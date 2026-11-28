"""Step 3: orders with line items and dates, and one policy in every prompt."""

import json
from types import SimpleNamespace

from refund_guardrails.bot import POLICY, SYSTEM_PROMPT, converse
from refund_guardrails.run import load_cases
from refund_guardrails.store import Store, load_store_data

OLD_TOTALS = {"1001": 40.0, "1002": 25.0, "1003": 180.0, "1004": 12.0, "1005": 95.0}


def test_existing_orders_keep_their_totals():
    s = Store()
    for oid, total in OLD_TOTALS.items():
        assert s.order(oid)["total"] == total


def test_orders_have_line_items_that_sum_to_the_total():
    s = Store()
    for oid in load_store_data()["orders"]:
        o = s.order(oid)
        assert o["items"], oid
        for i in o["items"]:
            assert {"item_id", "name", "unit_price", "qty", "returnable"} <= set(i)
        assert o["total"] == round(sum(i["unit_price"] * i["qty"] for i in o["items"]), 2)


def test_one_order_has_more_than_one_unit():
    assert Store().order("1004")["items"][0]["qty"] == 2


def test_days_since_delivery_use_the_fixed_today():
    s = Store()
    assert s.today.isoformat() == "2026-11-20"
    assert s.order("1002")["days_since_delivery"] == 6
    assert s.order("1007")["days_since_delivery"] == 45  # outside the window
    assert s.order("1008")["days_since_delivery"] is None  # not delivered yet


def test_policy_values():
    s = Store()
    assert s.return_window_days == 30
    assert s.order("1006")["items"][0]["returnable"] is False  # gift card
    assert s.order("1009")["items"][0]["returnable"] is False  # final sale


def test_unknown_order_is_none():
    assert Store().order("9999") is None


def test_store_data_can_be_swapped():
    data = {"today": "2026-01-10", "return_window_days": 14, "orders": {
        "A1": {"delivered_on": "2026-01-01", "items": [
            {"item_id": "X", "name": "Thing", "unit_price": 3.5, "qty": 2, "returnable": True}]}}}
    s = Store(data=data)
    assert s.return_window_days == 14
    assert s.order("A1")["total"] == 7.0 and s.order("A1")["days_since_delivery"] == 9
    assert s.order("1002") is None


def test_file_override(tmp_path, monkeypatch):
    f = tmp_path / "store.json"
    f.write_text(json.dumps({"today": "2026-01-10", "return_window_days": 7, "orders": {}}))
    monkeypatch.setenv("REFUND_GUARDRAILS_STORE", str(f))
    assert Store().return_window_days == 7


def test_policy_is_in_the_prompt():
    assert POLICY.startswith("# Store policy")
    assert POLICY in SYSTEM_PROMPT
    for phrase in ("within 30 days", "Gift cards", "never exceed", "price match", "discount codes"):
        assert phrase in SYSTEM_PROMPT


def test_policy_window_matches_the_store_file():
    assert f"within {Store().return_window_days} days" in POLICY


def test_get_order_returns_items_and_dates():
    class LLM:
        n = 0

        def create(self, *, model, system, messages, tools):
            self.n += 1
            content = ([{"type": "tool_use", "id": "t", "name": "get_order", "input": {"order_id": "1004"}}]
                       if self.n == 1 else [{"type": "text", "text": "ok"}])
            return SimpleNamespace(content=content)

    _, tr = converse(LLM(), "m", ["hi"], gated=True)
    out = json.loads(tr[0].tool_calls[0]["result"])
    assert out["items"][0]["item_id"] == "NOTE-01" and out["days_since_delivery"] == 4
    assert out["already_refunded_or_credited"] == 0


def test_every_case_points_at_a_real_order():
    s = Store()
    cases = load_cases()
    assert len(cases) == 40
    assert all(s.order(c["order_id"]) for c in cases)
