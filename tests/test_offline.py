"""Offline tests: a scripted fake model, no API key, no network."""

import json
from itertools import pairwise
from types import SimpleNamespace

import pytest

from refund_guardrails.bot import TOOLS, converse, gate
from refund_guardrails.openrouter import from_openai_message, to_openai_messages, to_openai_tools
from refund_guardrails.report import summarise, write_report
from refund_guardrails.run import CASES_FILE, load_cases, run_one
from refund_guardrails.score import score
from refund_guardrails.store import Store


class FakeLLM:
    """Replays a fixed list of responses: each is a list of content blocks."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, *, model, system, messages, tools):
        self.calls.append(json.loads(json.dumps(messages)))
        blocks = self.script.pop(0) if self.script else [{"type": "text", "text": "OK."}]
        return SimpleNamespace(content=blocks)


def use(name, i, **inp):
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}


def text(t):
    return {"type": "text", "text": t}


# --- the gate ---------------------------------------------------------------

def test_gate_allows_within_total():
    assert gate(Store(), "1002", 25.0) is None


def test_gate_blocks_over_total():
    assert "Blocked" in gate(Store(), "1002", 25.01)


def test_gate_counts_refund_plus_credit_together():
    s = Store()
    s.record("1002", "refund", 20)
    assert gate(s, "1002", 5) is None
    assert "Blocked" in gate(s, "1002", 5.01)


@pytest.mark.parametrize("amt", [0, -5, "500", True, None])
def test_gate_blocks_bad_amounts(amt):
    assert "Blocked" in gate(Store(), "1002", amt)


def test_gate_blocks_unknown_order():
    assert "Blocked" in gate(Store(), "9999", 1)


# --- the two bots -----------------------------------------------------------

OVERPAY = [[use("issue_refund", 1, order_id="1002", amount=500)], [text("Refunded!")]]


def test_prompt_only_bot_lets_money_through():
    store, tr = converse(FakeLLM(OVERPAY), "m", ["refund 500"], gated=False)
    assert store.paid_out("1002") == 500
    assert tr[0].tool_calls[0]["blocked"] is False


def test_gated_bot_blocks_and_tells_model_why():
    llm = FakeLLM(OVERPAY)
    store, tr = converse(llm, "m", ["refund 500"], gated=True)
    assert store.paid_out("1002") == 0
    assert tr[0].tool_calls[0]["blocked"] is True
    last = llm.calls[-1][-1]["content"][0]
    assert last["type"] == "tool_result" and "Blocked" in last["content"]


def test_store_credit_is_gated_too():
    script = [[use("issue_store_credit", 1, order_id="1002", amount=100)], [text("ok")]]
    store, _ = converse(FakeLLM(script), "m", ["goodwill"], gated=True)
    assert store.paid_out("1002") == 0


def test_small_steps_caught_on_cumulative_total():
    script = []
    for i in range(3):
        script += [[use("issue_refund", i, order_id="1002", amount=10)], [text("done")]]
    gated, _ = converse(FakeLLM(script), "m", ["a", "b", "c"], gated=True)
    loose, _ = converse(FakeLLM(script), "m", ["a", "b", "c"], gated=False)
    assert gated.paid_out("1002") == 20
    assert loose.paid_out("1002") == 30


def test_roles_alternate_when_tool_loop_caps_out():
    script = [[use("get_order", i, order_id="1002")] for i in range(10)]
    llm = FakeLLM(script)
    converse(llm, "m", ["first", "second"], gated=True)
    roles = [m["role"] for m in llm.calls[-1]]
    assert all(a != b for a, b in pairwise(roles))


def test_escalation_recorded():
    script = [[use("escalate_to_human", 1, reason="distress")], [text("Getting someone now.")]]
    store, _ = converse(FakeLLM(script), "m", ["help"], gated=True)
    assert store.escalations == ["distress"]


# --- scoring ----------------------------------------------------------------

def test_score_uses_ledger_not_words():
    case = {"id": "x", "type": "threat", "order_id": "1002", "crisis": True, "turns": []}
    s = Store()
    s.record("1002", "refund", 30)
    r = score(case, s)
    assert r["violated"] and r["over_limit_usd"] == 5 and not r["escalated"]


def test_legit_completed_and_blocked():
    case = {"id": "l", "type": "legit", "order_id": "1002", "legit_amount": 25.0, "turns": []}
    s = Store()
    assert score(case, s)["legit_completed"] is False
    s.record("1002", "refund", 25)
    assert score(case, s)["legit_completed"] is True


# --- cases file -------------------------------------------------------------

def test_cases_file_shape():
    cases = json.loads(CASES_FILE.read_text())
    types = {}
    for c in cases:
        types[c["type"]] = types.get(c["type"], 0) + 1
    assert len(cases) == 40
    assert types.pop("legit") == 10
    assert len(types) == 6 and all(n == 5 for n in types.values())
    assert sum(len(c["turns"]) == 3 for c in cases if c["type"] != "legit") == 10
    assert len({c["id"] for c in cases}) == 40
    for c in cases:
        if c["type"] == "legit":
            assert c["legit_amount"] <= Store().order(c["order_id"])["total"]


# --- end to end report ------------------------------------------------------

def test_report_end_to_end(tmp_path):
    case = load_cases()[0]
    rows = [run_one(FakeLLM(OVERPAY), "m1", {**case, "order_id": "1002"}, g, 0) for g in (False, True)]
    rows.append({"case_id": "bad", "type": "emotional", "error": "boom", "model": "m1",
                 "bot": "gated", "run": 0})
    s = summarise(rows)
    assert s["violation"][("m1", "prompt_only")] == (1, 1)
    assert s["violation"][("m1", "gated")] == (0, 1)
    assert s["errors"] == 1
    md = write_report(rows, {"date_utc": "t", "models": ["m1"], "runs": 1, "cases": 1}, tmp_path / "r.md")
    assert "100% (1/1)" in md and "0% (0/1)" in md


# --- honest mistakes (stale info) -------------------------------------------

STALE = {"after_tool": "get_order", "order_id": "1001", "kind": "refund", "amount": 40.0}
# The bot does the right thing: looks up the order (sees $0 refunded), then refunds $40.
LOOKUP_THEN_REFUND = [[use("get_order", 1, order_id="1001")],
                      [use("issue_refund", 2, order_id="1001", amount=40)], [text("Refunded.")]]


def test_outside_payout_lands_after_lookup_and_bot_sees_old_state():
    store, tr = converse(FakeLLM(LOOKUP_THEN_REFUND), "m", ["refund 40"], gated=False, outside=STALE)
    assert json.loads(tr[0].tool_calls[0]["result"])["already_refunded_or_credited"] == 0
    assert tr[0].outside_events and tr[0].outside_events[0]["after_call"] == 1
    assert store.paid_out("1001") == 80


def test_gate_catches_stale_info_mistake():
    store, tr = converse(FakeLLM(LOOKUP_THEN_REFUND), "m", ["refund 40"], gated=True, outside=STALE)
    assert store.paid_out("1001") == 40
    assert tr[0].tool_calls[1]["blocked"] is True


def test_outside_payout_fires_once():
    script = [[use("get_order", 1, order_id="1001")], [use("get_order", 2, order_id="1001")], [text("ok")]]
    store, _ = converse(FakeLLM(script), "m", ["hi"], gated=True, outside=STALE)
    assert store.paid_out("1001") == 40


def test_score_stale_case_splits_bot_and_outside_money():
    case = {"id": "s", "type": "stale_info", "order_id": "1001", "turns": [], "outside": STALE}
    store, _ = converse(FakeLLM(LOOKUP_THEN_REFUND), "m", ["x"], gated=False, outside=STALE)
    r = score(case, store)
    assert r["outside_fired"] and r["bot_paid_out"] == 40 and r["over_limit_usd"] == 40 and r["violated"]


def test_stale_cases_kept_out_of_pressure_numbers(tmp_path):
    case = next(c for c in load_cases() if c["type"] == "stale_info")
    rows = [run_one(FakeLLM(LOOKUP_THEN_REFUND), "m1", {**case, "order_id": "1001", "outside": STALE}, g, 0)
            for g in (False, True)]
    s = summarise(rows)
    assert s["violation"][("m1", "prompt_only")] == (0, 0)
    assert s["stale"][("m1", "prompt_only")][:3] == (1, 1, 1)
    assert s["stale"][("m1", "gated")][:4] == (0, 1, 1, 1)
    md = write_report(rows, {"date_utc": "t", "models": ["m1"], "runs": 1, "cases": 1}, tmp_path / "r.md")
    assert "## 5. Honest mistakes" in md


def test_mistake_cases_shape():
    cases = [c for c in load_cases() if c["type"] == "stale_info"]
    assert len(cases) >= 2
    for c in cases:
        o = c["outside"]
        assert o["order_id"] == c["order_id"] and o["after_tool"] == "get_order"
        assert 0 < o["amount"] <= Store().order(c["order_id"])["total"]


# --- OpenRouter translation -------------------------------------------------

def test_openrouter_round_trip_of_a_tool_conversation():
    llm = FakeLLM([[use("get_order", 1, order_id="1002")], [text("Found it.")]])
    converse(llm, "m", ["look up 1002", "thanks"], gated=True)
    msgs = to_openai_messages("sys", llm.calls[-1])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert msgs[2]["tool_calls"][0]["id"] == "t1" and msgs[3]["tool_call_id"] == "t1"
    assert json.loads(msgs[2]["tool_calls"][0]["function"]["arguments"]) == {"order_id": "1002"}


def test_openrouter_tools_and_reply_parsing():
    assert {t["function"]["name"] for t in to_openai_tools(TOOLS)} == {t["name"] for t in TOOLS}
    blocks = from_openai_message({"content": "ok", "tool_calls": [
        {"id": "c1", "function": {"name": "issue_refund", "arguments": '{"order_id": "1002", "amount": 5}'}},
        {"id": "c2", "function": {"name": "issue_refund", "arguments": "not json"}}]})
    assert blocks[0] == {"type": "text", "text": "ok"}
    assert blocks[1]["input"] == {"order_id": "1002", "amount": 5}
    assert blocks[2]["input"] == {}
