"""Run every case against both bot versions and every model, several times.

    python -m refund_guardrails.run --models claude-haiku-5-5 claude-sonnet-5-5 --runs 3

Writes results/<timestamp>/conversations.jsonl (full transcripts) and
results/<timestamp>/report.md (the numbers). Needs ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .bot import converse
from .report import write_report
from .score import score

CASES_FILE = Path(__file__).with_name("cases.json")
# Cases where the customer is honest and the bot can still slip (stale info). Kept apart
# from the pressure cases so they don't change the pressure numbers.
MISTAKE_CASES_FILE = Path(__file__).with_name("mistake_cases.json")
# Cloud sessions strip ANTHROPIC_API_KEY, so the key can also come from this variable.
KEY_VARS = ("ANTHROPIC_API_KEY", "REFUND_GUARDRAILS_ANTHROPIC_KEY")


def api_key() -> str | None:
    return next((os.environ[v] for v in KEY_VARS if os.environ.get(v)), None)


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text()) + json.loads(MISTAKE_CASES_FILE.read_text())


class AnthropicLLM:
    def __init__(self, max_tokens: int = 1024):
        import anthropic  # imported here so offline tests don't need it

        self.client = anthropic.Anthropic(api_key=api_key(), max_retries=5)
        self.max_tokens = max_tokens

    def create(self, *, model, system, messages, tools):
        return self.client.messages.create(model=model, system=system, messages=messages,
                                           tools=tools, max_tokens=self.max_tokens)


def run_one(llm, model: str, case: dict, gated: bool, run: int) -> dict:
    t0 = time.time()
    try:
        store, transcript = converse(llm, model, case["turns"], gated, outside=case.get("outside"))
        row = score(case, store)
        row["transcript"] = [t.__dict__ for t in transcript]
        row["gate_blocks"] = sum(c["blocked"] for t in transcript for c in t.tool_calls)
        row["error"] = None
    except Exception as exc:  # noqa: BLE001  keep going; errors are reported, never counted as passes
        row = {"case_id": case["id"], "type": case["type"], "error": f"{type(exc).__name__}: {exc}"}
    row.update({"model": model, "bot": "gated" if gated else "prompt_only", "run": run,
                "seconds": round(time.time() - t0, 2)})
    return row


def main(argv: list[str] | None = None) -> Path:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", required=True)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--only", nargs="*", help="case ids to run (default: all)")
    p.add_argument("--out", default="results")
    a = p.parse_args(argv)

    if not api_key():
        raise SystemExit(f"Set {' or '.join(KEY_VARS)} first.")
    cases = [c for c in load_cases() if not a.only or c["id"] in a.only]
    llm = AnthropicLLM()
    jobs = [(m, c, g, r) for m in a.models for c in cases for g in (False, True) for r in range(a.runs)]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} conversations -> {out}")
    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex, open(out / "conversations.jsonl", "w") as f:
        futs = [ex.submit(run_one, llm, *j) for j in jobs]
        for i, fut in enumerate(as_completed(futs), 1):
            row = fut.result()
            rows.append(row)
            f.write(json.dumps(row) + "\n")
            f.flush()
            if i % 20 == 0 or i == len(futs):
                print(f"  {i}/{len(futs)}")
    meta = {"date_utc": stamp, "models": a.models, "runs": a.runs, "cases": len(cases)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    write_report(rows, meta, out / "report.md")
    print(f"Report: {out / 'report.md'}")
    return out


if __name__ == "__main__":
    main()
