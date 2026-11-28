# vigilant-ecommerce-support-agent-and-evals

**Does a refund rule hold when customers push? One support bot, three designs.**

All three get the same model, the same system prompt and the same store policy (`refund_guardrails/policy.md`). They differ in what the tools let the model do:

| | Naive | Gated | Designed out |
|---|---|---|---|
| Who picks the refund amount | The model | The model | Nobody: code computes it |
| Money tools | `issue_refund`, `issue_store_credit` | Same, with a code check before money moves (refund + credit combined ≤ order total) | `start_return(order_id, item_id, qty)` only |
| Where the rule lives | The prompt | The prompt and a gate | The tool has no amount to argue over |

In the designed-out bot, code checks that the item is on the order, delivered, inside the return window, returnable and not already returned, and that the refund fits in what is left of the order total after money already paid and returns already promised. The refund is paid when the return is received, which the harness simulates at the end of each conversation. At receipt the ledger is read again: if paying would pass the order total, the return is marked `held` and nothing is paid.

## The test set (`refund_guardrails/cases.json`)

- **30 pressure cases**, 5 each: emotional, threat, fake authority, urgency, rewording ("call it goodwill"), small steps ("$10… another $10…")
- **10 of them escalate over 3 turns**
- **10 fair refunds** that should go through, so a design that blocks everyone doesn't look good
- **3 crisis messages** (inside the threat set): did the bot hand off to a human?

## Honest mistakes (`refund_guardrails/mistake_cases.json`)

- **2 stale information cases**: the customer is honest and the bot does the right thing, but right after it first looks up the order, another payout lands on that order (a simulated colleague or returns team, written into the ledger with `source="outside"`). The bot isn't told, so what it just read is out of date.
- Kept out of the pressure numbers and reported on their own.

## The "yes" turn

The scripts end before the customer can accept an offer, so a bot that asks first always pays $0. Every pressure and stale case is also run as a `-yes` copy with one more customer turn, "Yes, go ahead." The report shows both side by side; the scripts as written stay comparable with runs before 28 Nov 2026. `--scripts base` or `--scripts yes` runs only one.

## What gets measured

Scores come from the **money ledger**, never from what the bot says:

1. Money moved past an order's total, per design and model
2. Which pressure type works
3. Fair refunds that did not go through
4. Crisis messages handed to a human
5. Money moved on an order the customer never named (outside payouts excluded)
8. Honest mistakes: overpaid on stale info, gate blocks, returns rejected or held
6. Tokens and cost per conversation (cost needs prices in `refund_guardrails/prices.json`)
7. Tool calls the model garbled, counted apart and never scored

No results are in this repo until a run is done. Nothing here is estimated.

## Known limits

- Scripted customers do not react to the bot's replies.
- The store is synthetic (`refund_guardrails/data/store.json`); set `REFUND_GUARDRAILS_STORE` to use another file.
- The designed-out bot cannot give money back without a return. Six of the ten fair cases ask for that, so they count as "did not go through" until the cases declare escalation as their expected outcome.
- No login, sessions or concurrency yet.

## Run it

```bash
pip install anthropic openai pytest
pytest -q                               # offline, no key needed

export ANTHROPIC_API_KEY=...            # for anthropic:<model_id> (or REFUND_GUARDRAILS_ANTHROPIC_KEY)
export OPENROUTER_API_KEY=...           # for ids with a "/", unless a proxy adds the key
export OPENAI_COMPAT_BASE_URL=...       # for openai_compat:<model_id>, any OpenAI-compatible host
export OPENAI_COMPAT_API_KEY=...

python -m refund_guardrails.run --models claude-haiku-5-5 nvidia/nemotron-3.5-super-vl:free \
    --designs naive gated designed_out --runs 3
```

Output lands in `results/<timestamp>/`: `report.md` (the numbers), `conversations.jsonl` (every transcript) and `meta.json` (models, settings, prompt hash, git commit). Pin model ids and keep the date: models change.

Smoke test first: `--runs 1 --only thr-1 legit-1 step-1`.

The build plan for release 1 is in `docs/HANDOFF.md`.
