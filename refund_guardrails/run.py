"""Run every case against both bot versions and every model, several times.

    python -m refund_guardrails.run --models anthropic:<id> openai_compat:<id> --runs 3

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

from .bot import SYSTEM_PROMPT, converse
from .llm import DEFAULT_MAX_TOKENS, AnthropicLLM, ToolCallError, make_llm, parse_spec  # noqa: F401
from .report import write_report
from .score import score

CASES_FILE = Path(__file__).with_name("cases.json")


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text())


def run_one(llm, model: str, case: dict, gated: bool, run: int, label: str | None = None) -> dict:
    """One conversation, scored. `model` goes to the API; `label` names it in the results."""
    t0 = time.time()
    try:
        store, transcript = converse(llm, model, case["turns"], gated)
        row = score(case, store)
        row.update(store.usage)
        row["transcript"] = [t.__dict__ for t in transcript]
        row["error"] = None
    except ToolCallError as exc:
        # The model garbled a tool call. Its own outcome: not a pass, not a violation.
        row = {"case_id": case["id"], "type": case["type"], "error": None, "tool_call_error": str(exc)}
    except Exception as exc:  # noqa: BLE001  keep going; errors are reported, never counted as passes
        row = {"case_id": case["id"], "type": case["type"], "error": f"{type(exc).__name__}: {exc}"}
    row.update({"model": label or model, "bot": "gated" if gated else "prompt_only", "run": run,
                "seconds": round(time.time() - t0, 2)})
    return row


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).parent,
                             capture_output=True, text=True, timeout=5, check=False)
    except OSError:
        return None
    return out.stdout.strip() or None


def build_meta(*, models: list[str], runs: int, n_cases: int, max_tokens: int, stamp: str) -> dict:
    """Everything needed to tell two runs apart later."""
    return {
        "date_utc": stamp,
        "models": models,
        "providers": sorted({parse_spec(m)[0] for m in models}),
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
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    p.add_argument("--out", default="results")
    a = p.parse_args(argv)

    cases = [c for c in load_cases() if not a.only or c["id"] in a.only]
    specs = {m: parse_spec(m) for m in a.models}
    llms = {prov: make_llm(prov, a.max_tokens) for prov in {prov for prov, _ in specs.values()}}
    jobs = [(m, c, g, r) for m in a.models for c in cases for g in (False, True) for r in range(a.runs)]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} conversations -> {out}")
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex, open(out / "conversations.jsonl", "w") as f:
        futs = [ex.submit(run_one, llms[specs[m][0]], specs[m][1], c, g, r, label=m) for m, c, g, r in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            rows.append(row)
            f.write(json.dumps(row) + "\n")
            f.flush()
            if i % 20 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)}")
    meta = build_meta(models=a.models, runs=a.runs, n_cases=len(cases), max_tokens=a.max_tokens, stamp=stamp)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_report(rows, meta, out / "report.md")
    print(f"Report: {out / 'report.md'}")
    return out


if __name__ == "__main__":
    main()
