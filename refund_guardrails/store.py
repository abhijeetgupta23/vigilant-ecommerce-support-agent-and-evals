"""A tiny fake store: orders plus a ledger of every dollar that moved.

Orders live in data/store.json (line items, a delivered date, and a fixed
"today" so date checks give the same answer on every run). Point
REFUND_GUARDRAILS_STORE at another file to swap the data.

The ledger is the ground truth for scoring. Whatever the bot says, money only
moves when a tool call is executed, and every execution lands here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

DEFAULT_STORE_FILE = Path(__file__).parent / "data" / "store.json"


@lru_cache(maxsize=8)
def _read(path: str) -> dict:
    return json.loads(Path(path).read_text())


def load_store_data(path: str | Path | None = None) -> dict:
    """The store file as a dict. Treat it as read-only: it is shared between stores."""
    return _read(str(path or os.environ.get("REFUND_GUARDRAILS_STORE") or DEFAULT_STORE_FILE))


@dataclass
class Store:
    ledger: list[dict] = field(default_factory=list)  # {"order_id", "kind", "amount"}
    escalations: list[str] = field(default_factory=list)
    # Token counts for the conversation that used this store.
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0})
    # Returns started by the designed_out bot: {"order_id", "item_id", "qty", "refund_due", "status"}.
    returns: list[dict] = field(default_factory=list)
    data: dict = field(default_factory=load_store_data, repr=False)

    @property
    def today(self) -> date:
        return date.fromisoformat(self.data["today"])

    @property
    def return_window_days(self) -> int:
        return int(self.data["return_window_days"])

    def order(self, order_id: str) -> dict | None:
        o = self.data["orders"].get(str(order_id))
        if o is None:
            return None
        items = [dict(i) for i in o["items"]]
        delivered = o.get("delivered_on")
        days = (self.today - date.fromisoformat(delivered)).days if delivered else None
        return {
            "order_id": str(order_id),
            "item": ", ".join(f"{i['qty']} x {i['name']}" for i in items),
            "total": round(sum(i["unit_price"] * i["qty"] for i in items), 2),
            "items": items,
            "delivered_on": delivered,
            "days_since_delivery": days,
        }

    def paid_out(self, order_id: str) -> float:
        """Refunds plus store credit already given for this order."""
        return round(sum(e["amount"] for e in self.ledger if e["order_id"] == str(order_id)), 2)

    def returned_qty(self, order_id: str, item_id: str) -> int:
        """Units of this item already in a return, open or received."""
        return sum(r["qty"] for r in self.returns
                   if r["order_id"] == str(order_id) and r["item_id"] == str(item_id))

    def record(self, order_id: str, kind: str, amount: float) -> None:
        self.ledger.append({"order_id": str(order_id), "kind": kind, "amount": round(float(amount), 2)})
