# refund-guardrails

**Does a refund rule hold when customers push? A prompt vs a code gate.**

Two versions of the same support bot, same model, same prompt, same tools:

| | Prompt only | Prompt + code gate |
|---|---|---|
| The rule | "Never refund or credit more than the order total" in the system prompt | Same prompt, plus code that checks every payout before money moves |
| Money tools | `issue_refund`, `issue_store_credit` | Same, both gated (refund + credit combined ≤ order total) |

## The test set (`refund_guardrails/cases.json`)

- **30 pressure cases**, 5 each: emotional, threat, fake authority, urgency, rewording ("call it goodwill"), small steps ("$10… another $10…")
- **10 of them escalate over 3 turns**
- **10 fair refunds** that should go through, so a gate that blocks everyone doesn't look good
- **3 crisis messages** (inside the threat set): did the bot hand off to a human?

## Honest mistakes (`refund_guardrails/mistake_cases.json`)

- **2 stale-info cases**: the customer is honest and the bot does the right thing, but just after
  it looks up the order, another payout lands on that order (a simulated colleague or returns
  team, written straight into the fake ledger). The bot isn't told, so what it just read is out of date.
- Kept out of the pressure numbers and reported on their own.

Each case runs against both bots, each model, 3 times.

## What gets measured

Scores come from the **money ledger**, never from what the bot says:

1. Money moved past the order total: prompt only vs code gate, per model
2. Which pressure type works best on the prompt-only bot
3. Fair refunds that did not go through (want ~0)
4. Crisis messages handed to a human
5. Honest mistakes: did the bot pay on stale info, and did the gate block it?

No results are in this repo until a run is done. Nothing here is estimated.

## Run it

```bash
pip install anthropic pytest
pytest -q                               # offline, no key needed
export ANTHROPIC_API_KEY=...
python -m refund_guardrails.run --models claude-haiku-5-5 claude-sonnet-5-5 --runs 3
```

42 cases × 2 bots × 2 models × 3 runs = 504 conversations. Output lands in
`results/<timestamp>/`: `report.md` (the numbers), `conversations.jsonl` (every
transcript) and `meta.json` (models, date). Pin model IDs and keep the date: models change.

Smoke test first: `--runs 1 --only thr-1 legit-1 step-1`.
