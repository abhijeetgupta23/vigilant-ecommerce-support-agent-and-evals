"""Run every case against each design and every model, several times.

    python -m refund_guardrails.run --models anthropic:<id> openai_compat:<id> \
        --designs naive gated designed_out --runs 3

A model is written as provider:model_id (see llm.py); a bare id means anthropic.
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


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text())


def simulate_warehouse_receipt(store: Store) -> int:
    """Stand in for the warehouse: mark every open return as received and pay its refund.

    This is the only way money reaches the ledger in the designed_out design.
    Returns the number of returns received. Safe to call more than once.
    """
    received = 0
    for r in store.returns:
        if r["status"] == "open":
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
        store, transcript = converse(llm, model, case["turns"], design=design)
        simulate_warehouse_receipt(store)
        row = score(case, store)
        row.update(store.usage)
        row["returns"] = store.returns
        row["transcript"] = [t.__dict__ for t in transcript]
        row["error"] = None
    except ToolCallError as exc:
        # The model garbled a tool call. Its own outcome: not a pass, not a violation.
        row = {"case_id": case["id"], "type": case["type"], "error": None, "tool_call_error": str(exc)}
    except Exception as exc:  # noqa: BLE001  keep going; errors are reported, never counted as passes
        row = {"case_id": case["id"], "type": case["type"], "error": f"{type(exc).__name__}: {exc}"}
    row.update({"model": label or model, "design": design, "run": run,
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
               designs: list[str] | None = None) -> dict:
    """Everything needed to tell two runs apart later."""
    return {
        "date_utc": stamp,
        "models": models,
        "providers": sorted({parse_spec(m)[0] for m in models}),
        "designs": list(designs or DESIGNS),
        "runs": runs,
        "cases": n_cases,
        "temperature": "provider default",
        "max_tokens": max_tokens,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12],
        "git_commit": _git_commit(),
    }


def main(argv: list[str] | None = None) -> Path:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", required=True, help="provider:model_id, e.g. anthropic:<id>")
    p.add_argument("--designs", nargs="+", default=list(DESIGNS), choices=DESIGNS)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    p.add_argument("--out", default="results")
    a = p.parse_args(argv)

    cases = [c for c in load_cases() if not a.only or c["id"] in a.only]
    specs = {m: parse_spec(m) for m in a.models}
    llms = {prov: make_llm(prov, a.max_tokens) for prov in {prov for prov, _ in specs.values()}}
    jobs = [(m, c, d, r) for m in a.models for c in cases for d in a.designs for r in range(a.runs)]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} conversations -> {out}")
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex, open(out / "conversations.jsonl", "w") as f:
        futs = [ex.submit(run_one, llms[specs[m][0]], specs[m][1], c, d, r, label=m) for m, c, d, r in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            rows.append(row)
            f.write(json.dumps(row) + "\n")
            f.flush()
            if i % 20 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)}")
    meta = build_meta(models=a.models, runs=a.runs, n_cases=len(cases), max_tokens=a.max_tokens,
                      stamp=stamp, designs=a.designs)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_report(rows, meta, out / "report.md")
    print(f"Report: {out / 'report.md'}")
    return out


if __name__ == "__main__":
    main()
