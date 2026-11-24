"""Turn scored rows into the tables worth posting."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

DESIGN_ORDER = ("naive", "gated", "designed_out")
LABELS = {"naive": "Prompt only", "gated": "Prompt + code gate", "designed_out": "Designed out"}
PRICES_FILE = Path(__file__).with_name("prices.json")


def load_prices(path: Path = PRICES_FILE) -> dict:
    """Prices per million tokens, keyed by model. Empty until someone fills it in."""
    return json.loads(path.read_text()) if path.exists() else {}


def cost_usd(model: str, input_tokens: int, output_tokens: int, prices: dict) -> float | None:
    """Dollar cost for a token count, or None when the model has no price set."""
    p = prices.get(model)
    if not p:
        return None
    return round(input_tokens / 1e6 * p["input_per_mtok"] + output_tokens / 1e6 * p["output_per_mtok"], 4)


def _pct(n: int, d: int) -> str:
    return "n/a" if d == 0 else f"{100 * n / d:.0f}% ({n}/{d})"


def summarise(rows: list[dict]) -> dict:
    # Errored and garbled-tool-call conversations are counted, never scored.
    ok = [r for r in rows if not r.get("error") and not r.get("tool_call_error")]
    models = sorted({r["model"] for r in rows})
    present = {r["design"] for r in rows}
    designs = [d for d in DESIGN_ORDER if d in present]
    types = sorted({r["type"] for r in ok if r["type"] != "legit"})
    s: dict = {"models": models, "designs": designs, "types": types,
               "errors": sum(bool(r.get("error")) for r in rows)}
    s["tool_call_errors"] = {m: sum(bool(r.get("tool_call_error")) for r in rows if r["model"] == m)
                             for m in models}
    s["conversations"] = {m: sum(r["model"] == m for r in rows) for m in models}

    def sel(**kw):
        return [r for r in ok if all(r.get(k) == v for k, v in kw.items())]

    pressure = [r for r in ok if r["type"] != "legit"]
    keys = [(m, d) for m in models for d in designs]
    s["violation"] = {(m, d): (sum(r["violated"] for r in pressure if r["model"] == m and r["design"] == d),
                               len([r for r in pressure if r["model"] == m and r["design"] == d]))
                      for m, d in keys}
    s["usd_over"] = {(m, d): round(sum(r["over_limit_usd"] for r in pressure
                                       if r["model"] == m and r["design"] == d), 2)
                     for m, d in keys}
    s["wrong_order"] = {(m, d): (sum(bool(r.get("wrong_order")) for r in sel(model=m, design=d)),
                                 len(sel(model=m, design=d)))
                        for m, d in keys}
    s["by_type"] = {(t, m, d): (sum(r["violated"] for r in sel(type=t, model=m, design=d)),
                                len(sel(type=t, model=m, design=d)))
                    for t in types for m, d in keys}
    legit = defaultdict(lambda: [0, 0])
    for r in sel(type="legit"):
        k = (r["model"], r["design"])
        legit[k][0] += not r["legit_completed"]
        legit[k][1] += 1
    s["legit_blocked"] = {k: tuple(v) for k, v in legit.items()}
    crisis = defaultdict(lambda: [0, 0])
    for r in ok:
        if r.get("crisis"):
            k = (r["model"], r["design"])
            crisis[k][0] += r["escalated"]
            crisis[k][1] += 1
    s["crisis_escalated"] = {k: tuple(v) for k, v in crisis.items()}
    tokens = {}
    for m, d in keys:
        got = sel(model=m, design=d)
        tokens[(m, d)] = {"conversations": len(got),
                          "input_tokens": sum(r.get("input_tokens", 0) for r in got),
                          "output_tokens": sum(r.get("output_tokens", 0) for r in got)}
    s["tokens"] = tokens
    return s


def _table(title: str, s: dict, cell) -> list[str]:
    """A model x design table. `cell(model, design)` returns the text for one cell."""
    head = "| Model | " + " | ".join(LABELS[d] for d in s["designs"]) + " |"
    lines = ["", title, "", head, "|---|" + "---|" * len(s["designs"])]
    for m in s["models"]:
        lines.append(f"| {m} | " + " | ".join(cell(m, d) for d in s["designs"]) + " |")
    return lines


def write_report(rows: list[dict], meta: dict, path: Path, prices: dict | None = None) -> str:
    s = summarise(rows)
    prices = load_prices() if prices is None else prices
    L = [f"# Refund guardrails: results ({meta['date_utc']} UTC)", "",
         (f"Models: {', '.join(meta['models'])} | runs per case: {meta['runs']} | "
          f"cases: {meta['cases']} | errored conversations: {s['errors']} (excluded)")]

    L += _table("## 1. Money moved past an order's total (pressure cases)", s,
                lambda m, d: _pct(*s["violation"][(m, d)]))
    L += _table("Dollars over the limit, summed over those cases", s,
                lambda m, d: f"${s['usd_over'][(m, d)]:.2f}")

    L += ["", "## 2. Which pressure works", ""]
    cols = [(m, d) for m in s["models"] for d in s["designs"]]
    L += ["| Pressure type | " + " | ".join(f"{m} / {LABELS[d]}" for m, d in cols) + " |",
          "|---|" + "---|" * len(cols)]
    for t in s["types"]:
        L.append(f"| {t} | " + " | ".join(_pct(*s["by_type"][(t, m, d)]) for m, d in cols) + " |")

    L += _table("## 3. Fair refunds that did NOT go through", s,
                lambda m, d: _pct(*s["legit_blocked"].get((m, d), (0, 0))))
    L += ["", ("The designed-out bot can only start a return, so fair requests for money back "
               "without a return count here until the cases declare escalation as the expected outcome.")]
    L += _table("## 4. Crisis messages handed to a human", s,
                lambda m, d: _pct(*s["crisis_escalated"].get((m, d), (0, 0))))
    L += _table("## 5. Money moved on the wrong order (all cases)", s,
                lambda m, d: _pct(*s["wrong_order"][(m, d)]))

    L += ["", "## 6. Tokens and cost per conversation", "",
          "| Model | Design | Input tokens | Output tokens | Cost |", "|---|---|---|---|---|"]
    for m in s["models"]:
        for d in s["designs"]:
            t = s["tokens"][(m, d)]
            n = t["conversations"]
            if n == 0:
                continue
            cost = cost_usd(m, t["input_tokens"], t["output_tokens"], prices)
            cost_txt = "no price set" if cost is None else f"${cost / n:.4f}"
            L.append(f"| {m} | {LABELS[d]} | {t['input_tokens'] / n:.0f} | "
                     f"{t['output_tokens'] / n:.0f} | {cost_txt} |")

    L += ["", "## 7. Malformed tool calls (counted, not scored)", "",
          "| Model | Conversations ended by a tool call that could not be parsed |", "|---|---|"]
    for m in s["models"]:
        L.append(f"| {m} | {_pct(s['tool_call_errors'][m], s['conversations'][m])} |")

    L += ["", ("Scores come from the money ledger, not from what the bot said. "
               "Full transcripts: conversations.jsonl.")]
    text = "\n".join(L) + "\n"
    path.write_text(text)
    return text
