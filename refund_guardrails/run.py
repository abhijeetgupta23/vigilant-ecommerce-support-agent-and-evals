"""Run every case against each design and every model, several times.

    python -m refund_guardrails.run --models anthropic:<id> openai_compat:<id> \
        --designs naive gated designed_out --runs 3

A model is written as provider:model_id (see llm.py). A bare id with a "/" in it
(e.g. nvidia/nemotron-3.5-super-vl:free) runs on OpenRouter; any other bare id means anthropic.

Cases: cases.json (pressure and fair refunds), mistake_cases.json (stale information), and a
"-yes" copy of every pressure and stale case that ends with the customer saying yes.
Writes results/<timestamp>/conversations.jsonl (full transcripts),
results/<timestamp>/report.md (the numbers) and meta.json (the settings).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .bot import DESIGNS, SYSTEM_PROMPT, converse, resolve_design
from .llm import DEFAULT_MAX_TOKENS, AnthropicLLM, ToolCallError, make_llm, parse_spec  # noqa: F401
from .report import write_report
from .score import score
from .store import Store

CASES_FILE = Path(__file__).with_name("cases.json")
# Honest customer, bot slips because what it read went stale. Reported apart from pressure cases.
MISTAKE_CASES_FILE = Path(__file__).with_name("mistake_cases.json")
SCRIPTS = ("base", "yes")
YES_TURN = "Yes, go ahead."


def with_yes_turn(case: dict) -> dict:
    """A copy of the case whose customer accepts whatever the bot offered last."""
    return {**case, "id": f"{case['id']}-yes", "base_id": case["id"], "script": "yes",
            "turns": [*case["turns"], YES_TURN]}


def load_cases(scripts: tuple[str, ...] | list[str] = SCRIPTS) -> list[dict]:
    """Every case to run. "base" is the scripts as written (comparable with the 27 Nov runs);
    "yes" adds a final "Yes, go ahead." turn to each pressure and stale case, so a bot that
    offers something and waits for consent is scored on what it pays after the yes."""
    base = [{**c, "script": "base"} for c in
            json.loads(CASES_FILE.read_text()) + json.loads(MISTAKE_CASES_FILE.read_text())]
    out = base if "base" in scripts else []
    if "yes" in scripts:
        out = out + [with_yes_turn(c) for c in base if c["type"] != "legit"]
    return out


def simulate_warehouse_receipt(store: Store) -> int:
    """Stand in for the warehouse: mark every open return as received and pay its refund.

    This is the only way money reaches the ledger in the designed_out design.
    Returns the number of returns received. Safe to call more than once.
    The ledger is read again at payment time: if paying would take the order past its total
    (someone else paid money on it after the return started), the return is marked "held"
    and nothing is paid. Held returns are not counted as received.
    """
    received = 0
    for r in store.returns:
        if r["status"] != "open":
            continue
        order = store.order(r["order_id"])
        if store.paid_out(r["order_id"]) + r["refund_due"] > order["total"] + 1e-9:
            r["status"] = "held"
            continue
        store.record(r["order_id"], "refund", r["refund_due"])
        r["status"] = "received"
        received += 1
    return received


def run_one(llm, model: str, case: dict, design: str | bool, run: int, label: str | None = None) -> dict:
    """One conversation, scored. `model` goes to the API; `label` names it in the results.

    `design` is a design name; the old gated flag (True/False) is still accepted.
    """
    design = resolve_design(design)
    t0 = time.time()
    try:
        store, transcript = converse(llm, model, case["turns"], design=design, outside=case.get("outside"))
        simulate_warehouse_receipt(store)
        row = score(case, store)
        row.update(store.usage)
        row["returns"] = store.returns
        calls = [c for t in transcript for c in t.tool_calls]
        # A blocked money call (gated) and a rejected return (designed_out) are counted apart.
        row["gate_blocks"] = sum(c["blocked"] for c in calls if c["name"] != "start_return")
        row["returns_rejected"] = sum(c["blocked"] for c in calls if c["name"] == "start_return")
        row["returns_held"] = sum(r["status"] == "held" for r in store.returns)
        row["transcript"] = [t.__dict__ for t in transcript]
        row["error"] = None
    except ToolCallError as exc:
        # The model garbled a tool call. Its own outcome: not a pass, not a violation.
        row = {"case_id": case["id"], "type": case["type"], "error": None, "tool_call_error": str(exc)}
    except Exception as exc:  # noqa: BLE001  keep going; errors are reported, never counted as passes
        row = {"case_id": case["id"], "type": case["type"], "error": f"{type(exc).__name__}: {exc}"}
    row.update({"model": label or model, "design": design, "run": run,
                "script": case.get("script", "base"), "base_id": case.get("base_id", case["id"]),
                "seconds": round(time.time() - t0, 2)})
    return row


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=5, check=False)
    except OSError:
        return None
    return out.stdout.strip() or None


def build_meta(*, models: list[str], runs: int, n_cases: int, max_tokens: int, stamp: str,
               designs: list[str] | None = None, scripts: list[str] | None = None) -> dict:
    """Everything needed to tell two runs apart later."""
    return {
        "date_utc": stamp,
        "models": models,
        "providers": sorted({parse_spec(m)[0] for m in models}),
        "designs": list(designs or DESIGNS),
        "runs": runs,
        "cases": n_cases,
        "scripts": list(scripts or SCRIPTS),
        "temperature": "provider default",
        "max_tokens": max_tokens,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
        "git_commit": _git_commit(),
    }


def job_key(row: dict) -> tuple:
    return (row["model"], row["case_id"], row["design"], row["run"])


def latest_rows(rows: list[dict]) -> list[dict]:
    """One row per job: a scored row if any attempt was scored, else the last error.
    A resumed folder keeps every attempt in conversations.jsonl; the report uses these."""
    best: dict = {}
    for r in rows:
        k = job_key(r)
        if k not in best or best[k].get("error") or not r.get("error"):
            best[k] = r
    return list(best.values())


def order_jobs(models, cases, designs, runs) -> list[tuple]:
    """Stale cases first, then pressure, then fair refunds; one run of everything before the
    next run; designs interleaved. A run cut short by a daily limit still covers all designs."""
    rank = {"stale_info": 0, "legit": 2}
    cases = sorted(cases, key=lambda c: (rank.get(c["type"], 1), c.get("script") == "yes"))
    return [(m, c, d, r) for m in models for r in range(runs) for c in cases for d in designs]


def main(argv: list[str] | None = None) -> Path:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", required=True, help="provider:model_id, e.g. anthropic:<id>")
    p.add_argument("--designs", nargs="+", default=list(DESIGNS), choices=DESIGNS)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    p.add_argument("--scripts", nargs="+", default=list(SCRIPTS), choices=SCRIPTS,
                   help="base scripts, the '-yes' copies, or both (default: both)")
    p.add_argument("--out", default="results")
    p.add_argument("--resume", help="an earlier results/<timestamp> folder: skip jobs already scored "
                                    "there, append new attempts, rewrite the report over all of them")
    a = p.parse_args(argv)

    cases = [c for c in load_cases(a.scripts) if not a.only or c["id"] in a.only]
    specs = {m: parse_spec(m) for m in a.models}
    llms = {prov: make_llm(prov, a.max_tokens) for prov in {prov for prov, _ in specs.values()}}
    jobs = order_jobs(a.models, cases, a.designs, a.runs)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    old_rows: list[dict] = []
    old_meta: dict = {}
    if a.resume:
        out = Path(a.resume)
        old_rows = [json.loads(line) for line in (out / "conversations.jsonl").read_text().splitlines()
                    if line.strip()]
        old_meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
        done = {job_key(r) for r in old_rows if not r.get("error")}
        jobs = [j for j in jobs if (j[0], j[1]["id"], j[2], j[3]) not in done]
    else:
        out = Path(a.out) / stamp
        out.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} conversations -> {out}")

    stopped: set[str] = set()  # models that hit a daily limit: skip their remaining jobs

    def job(m, c, d, r):
        if m in stopped:
            return None
        row = run_one(llms[specs[m][0]], specs[m][1], c, d, r, label=m)
        if "daily limit" in (row.get("error") or ""):
            stopped.add(m)
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex, open(out / "conversations.jsonl", "a") as f:
        futs = [ex.submit(job, m, c, d, r) for m, c, d, r in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            if row is not None:
                rows.append(row)
                f.write(json.dumps(row) + "\n")
                f.flush()
            if i % 20 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)}" + (f"  (daily limit: {sorted(stopped)})" if stopped else ""))
    if stopped:
        print(f"Stopped at a daily limit: {sorted(stopped)}. Resume later with --resume {out}")
    all_rows = latest_rows(old_rows + rows)
    models = list(dict.fromkeys([*old_meta.get("models", []), *a.models]))
    meta = build_meta(models=models, runs=a.runs, n_cases=len(cases), max_tokens=a.max_tokens,
                      stamp=old_meta.get("date_utc", stamp), designs=a.designs, scripts=a.scripts)
    meta["sessions_utc"] = [*old_meta.get("sessions_utc", []), stamp]
    if old_meta.get("git_commit") and old_meta["git_commit"] != meta["git_commit"]:
        meta["git_commits"] = sorted({*old_meta.get("git_commits", [old_meta["git_commit"]]),
                                      meta["git_commit"]})
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_report(all_rows, meta, out / "report.md")
    print(f"Report: {out / 'report.md'}")
    return out


if __name__ == "__main__":
    main()
