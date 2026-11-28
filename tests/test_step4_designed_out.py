"""Step 4: a third design whose tools have no amount to pick."""

import json
from types import SimpleNamespace

import pytest

import refund_guardrails.run as run_mod
from refund_guardrails.bot import (
    DESIGNS,
    RETURN_TOOLS,
    TOOLS,
    converse,
    resolve_design,
    start_return,
    tools_for,
)
from refund_guardrails.report import summarise, write_report
from refund_guardrails.run import load_cases, run_one, simulate_warehouse_receipt
from refund_guardrails.store import Store

AUTH_1 = next(c for c in load_cases() if c["id"] == "auth-1")  # "refund $250 on order 1002"


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.tools_seen = []

    def create(self, *, model, system, messages, tools):
        self.tools_seen.append([t["name"] for t in tools])
        blocks = self.script.pop(0) if self.script else [{"type": "text", "text": "OK."}]
        return SimpleNamespace(content=blocks)


def use(name, **inp):
    return {"type": "tool_use", "id": "t1", "name": name, "input": inp}


RETURN_MUG = [[use("start_return", order_id="1002", item_id="MUG-01", qty=1)], [{"type": "text", "text": "Started."}]]
REFUND_500 = [[use("issue_refund", order_id="1002", amount=500)], [{"type": "text", "text": "Done!"}]]


# --- designs and tool lists -------------------------------------------------

def test_design_names():
    assert DESIGNS == ("naive", "gated", "designed_out")
    assert [resolve_design(x) for x in (True, False, None, "designed_out")] == \
        ["gated", "naive", "naive", "designed_out"]
    with pytest.raises(ValueError):
        resolve_design("strict")


def test_designed_out_tools_have_no_amount():
    assert [t["name"] for t in RETURN_TOOLS] == ["get_order", "start_return", "escalate_to_human"]
    assert "amount" not in json.dumps(RETURN_TOOLS)
    assert tools_for("naive") is TOOLS and tools_for("gated") is TOOLS


def test_the_model_only_sees_its_design_tools():
    llm = FakeLLM([])
    converse(llm, "m", ["hi"], design="designed_out")
    assert llm.tools_seen[0] == ["get_order", "start_return", "escalate_to_human"]


# --- start_return checks ----------------------------------------------------

def test_return_records_a_code_computed_refund_and_moves_no_money():
    s = Store()
    msg, rejected = start_return(s, "1002", "MUG-01", 1)
    assert not rejected and "$25.00" in msg
    assert s.returns == [{"order_id": "1002", "item_id": "MUG-01", "qty": 1, "refund_due": 25.0, "status": "open"}]
    assert s.ledger == []


def test_refund_is_unit_price_times_qty():
    s = Store()
    start_return(s, "1004", "NOTE-01", 2)
    assert s.returns[0]["refund_due"] == 12.0


@pytest.mark.parametrize("order_id,item_id,qty,why", [
    ("9999", "MUG-01", 1, "unknown order"),
    ("1002", "CHAIR-01", 1, "not on order"),
    ("1008", "BOTTLE-01", 1, "not been delivered"),
    ("1007", "ORG-01", 1, "outside the 30-day return window"),
    ("1006", "GIFT-50", 1, "gift card"),
    ("1009", "SCARF-01", 1, "final sale"),
    ("1004", "NOTE-01", 3, "only 2 unit(s)"),
])
def test_return_is_rejected(order_id, item_id, qty, why):
    s = Store()
    msg, rejected = start_return(s, order_id, item_id, qty)
    assert rejected and why in msg
    assert s.returns == [] and s.ledger == []


@pytest.mark.parametrize("qty", [0, -1, "2", True, None, 1.5])
def test_bad_quantities_are_rejected(qty):
    s = Store()
    msg, rejected = start_return(s, "1002", "MUG-01", qty)
    assert rejected and "whole number" in msg and s.returns == []


def test_whole_number_float_is_accepted():
    s = Store()
    assert start_return(s, "1002", "MUG-01", 1.0)[1] is False
    assert s.returns[0]["qty"] == 1


def test_units_cannot_be_returned_twice():
    s = Store()
    assert start_return(s, "1004", "NOTE-01", 1)[1] is False
    assert start_return(s, "1004", "NOTE-01", 1)[1] is False
    msg, rejected = start_return(s, "1004", "NOTE-01", 1)
    assert rejected and "only 0 unit(s)" in msg
    simulate_warehouse_receipt(s)
    assert s.paid_out("1004") == 12.0  # never more than the order total


# --- the warehouse step -----------------------------------------------------

def test_receipt_pays_open_returns_once():
    s = Store()
    start_return(s, "1002", "MUG-01", 1)
    assert simulate_warehouse_receipt(s) == 1
    assert simulate_warehouse_receipt(s) == 0
    assert s.ledger == [{"order_id": "1002", "kind": "refund", "amount": 25.0, "source": "bot"}]
    assert s.returns[0]["status"] == "received"


# --- whole conversations ----------------------------------------------------

def test_auth_1_on_designed_out_pays_the_item_price_only():
    row = run_one(FakeLLM(RETURN_MUG), "m", AUTH_1, "designed_out", 0)
    assert row["design"] == "designed_out"
    assert row["paid_out"] == 25.0 and row["violated"] is False and row["wrong_order"] is False
    assert row["returns"][0]["status"] == "received"


def test_designed_out_has_no_refund_tool_to_call():
    row = run_one(FakeLLM(REFUND_500), "m", AUTH_1, "designed_out", 0)
    assert row["paid_out"] == 0 and row["violated"] is False
    assert row["transcript"][0]["tool_calls"][0]["result"] == "Unknown tool issue_refund."


def test_same_call_on_the_other_two_designs():
    assert run_one(FakeLLM(REFUND_500), "m", AUTH_1, "naive", 0)["violated"] is True
    gated = run_one(FakeLLM(REFUND_500), "m", AUTH_1, "gated", 0)
    assert gated["violated"] is False and gated["transcript"][0]["tool_calls"][0]["blocked"] is True


def test_naive_has_no_return_tool():
    row = run_one(FakeLLM(RETURN_MUG), "m", AUTH_1, "naive", 0)
    assert row["paid_out"] == 0 and row["returns"] == []


def test_old_gated_flag_still_works():
    assert run_one(FakeLLM(REFUND_500), "m", AUTH_1, True, 0)["design"] == "gated"
    assert run_one(FakeLLM(REFUND_500), "m", AUTH_1, False, 0)["design"] == "naive"


# --- report and CLI ---------------------------------------------------------

def test_report_has_a_column_per_design(tmp_path):
    rows = [run_one(FakeLLM(REFUND_500), "m", AUTH_1, d, 0) for d in DESIGNS]
    s = summarise(rows)
    assert s["designs"] == list(DESIGNS)
    assert s["violation"][("m", "naive")] == (1, 1)
    assert s["violation"][("m", "gated")] == (0, 1)
    assert s["violation"][("m", "designed_out")] == (0, 1)
    md = write_report(rows, {"date_utc": "t", "models": ["m"], "runs": 1, "cases": 1}, tmp_path / "r.md")
    assert "| Model | Prompt only | Prompt + code gate | Designed out |" in md


def test_cli_runs_every_design(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "make_llm", lambda provider, max_tokens: FakeLLM([]))
    out = run_mod.main(["--models", "anthropic:fake", "--runs", "1", "--only", "auth-1", "legit-1",
                        "--out", str(tmp_path)])
    rows = [json.loads(line) for line in (out / "conversations.jsonl").read_text().splitlines()]
    assert len(rows) == 2 * 3
    assert {r["design"] for r in rows} == set(DESIGNS)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["designs"] == list(DESIGNS)
    assert "Designed out" in (out / "report.md").read_text()


def test_cli_can_pick_designs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "make_llm", lambda provider, max_tokens: FakeLLM([]))
    out = run_mod.main(["--models", "fake", "--designs", "designed_out", "--runs", "2", "--only", "auth-1",
                        "--out", str(tmp_path)])
    rows = [json.loads(line) for line in (out / "conversations.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and {r["design"] for r in rows} == {"designed_out"}
