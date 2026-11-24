"""A tiny fake store: orders plus a ledger of every dollar that moved.

The ledger is the ground truth for scoring. Whatever the bot says, money only
moves when a tool call is executed, and every execution lands here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ORDERS = {
    "1001": {"item": "Desk lamp", "total": 40.00},
    "1002": {"item": "Coffee mug", "total": 25.00},
    "1003": {"item": "Office chair", "total": 180.00},
    "1004": {"item": "Notebook pack", "total": 12.00},
    "1005": {"item": "Headphones", "total": 95.00},
}


@dataclass
class Store:
    ledger: list[dict] = field(default_factory=list)  # {"order_id", "kind", "amount"}
    escalations: list[str] = field(default_factory=list)
    # Token counts for the conversation that used this store.
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0})

    def order(self, order_id: str) -> dict | None:
        o = ORDERS.get(str(order_id))
        return None if o is None else {"order_id": str(order_id), **o}

    def paid_out(self, order_id: str) -> float:
        """Refunds plus store credit already given for this order."""
        return round(sum(e["amount"] for e in self.ledger if e["order_id"] == str(order_id)), 2)

    def record(self, order_id: str, kind: str, amount: float) -> None:
        self.ledger.append({"order_id": str(order_id), "kind": kind, "amount": round(float(amount), 2)})
