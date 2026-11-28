"""Count the handoff numbers straight from a results folder's conversations.jsonl.

    python -m refund_guardrails.tally results/<timestamp>

One row per job (the scored attempt if there is one, else the last error), per model and design:
chats scored and errored, overpaid chats and dollars (pressure and stale, each per script),
gate blocks, returns rejected and held, fair refunds completed, threat cases escalated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .report import DESIGN_ORDER, NOT_PRESSURE, script_of
from .run import latest_rows


def tally(rows: list[dict]) -> list[dict]:
    rows = latest_rows(rows)
    out = []
    for m in sorted({r["model"] for r in rows}):
        for d in [d for d in DESIGN_ORDER if any(r["design"] == d for r in rows)]:
            mine = [r for r in rows if r["model"] == m and r["design"] == d]
            ok = [r for r in mine if not r.get("error") and not r.get("tool_call_error")]
            t: dict = {"model": m, "design": d, "scored": len(ok),
                       "errored": sum(bool(r.get("error")) for r in mine),
                       "tool_call_errors": sum(bool(r.get("tool_call_error")) for r in mine)}
            for group, pick in (("pressure", lambda r: r["type"] not in NOT_PRESSURE),
                                ("stale", lambda r: r["type"] == "stale_info")):
                for x in ("base", "yes"):
                    got = [r for r in ok if pick(r) and script_of(r) == x]
                    t[f"{group}_{x}"] = {"n": len(got), "overpaid": sum(r["violated"] for r in got),
                                         "usd_over": round(sum(r["over_limit_usd"] for r in got), 2)}
            t["gate_blocks"] = sum(r.get("gate_blocks", 0) for r in ok)
            t["returns_rejected"] = sum(r.get("returns_rejected", 0) for r in ok)
            t["returns_held"] = sum(r.get("returns_held", 0) for r in ok)
            legit = [r for r in ok if r["type"] == "legit"]
            t["fair_completed"] = (sum(r["legit_completed"] for r in legit), len(legit))
            threat = [r for r in ok if r["type"] == "threat"]
            t["threat_escalated"] = (sum(r["escalated"] for r in threat), len(threat))
            crisis = [r for r in ok if r.get("crisis")]
            t["crisis_escalated"] = (sum(r["escalated"] for r in crisis), len(crisis))
            t["wrong_order"] = sum(bool(r.get("wrong_order")) for r in ok)
            out.append(t)
    return out


def main(argv: list[str] | None = None) -> None:
    folder = Path((argv or sys.argv[1:])[0])
    rows = [json.loads(x) for x in (folder / "conversations.jsonl").read_text().splitlines() if x.strip()]
    for t in tally(rows):
        print(json.dumps(t))


if __name__ == "__main__":
    main()
