from __future__ import annotations

from typing import Any

from app.db import connect
from app.engines import apply_summary
from app.repositories import skus as sku_repo
from app.repositories import settings as settings_repo
from app.repositories import batches as batch_repo
from app.repositories import tickets as ticket_repo
from app.repositories import prices as price_repo
from app.repositories import sales as sales_repo


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        clock = settings_repo.get(conn, "business_clock")
        state = apply_summary.audit(conn)
        return {
            "project": "pricecab",
            "name": "价签柜",
            "business_clock": clock,
            "sku_count": conn.execute("SELECT COUNT(*) c FROM skus").fetchone()["c"],
            "ticket_count": conn.execute("SELECT COUNT(*) c FROM price_tickets").fetchone()["c"],
            "scheduled_count": state["scheduled_ticket_count"],
            # 「已应用」只认价签本 + 历史可互证的凭证；与 book_source_count 同口径。
            "applied_count": state["applied_ticket_count"],
            "applied_claimed_count": state["claimed_applied_count"],
            "book_count": conn.execute("SELECT COUNT(*) c FROM price_book").fetchone()["c"],
            "book_source_count": state["book_source_sku_count"],
            "sale_count": conn.execute("SELECT COUNT(*) c FROM sales_fixture").fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM apply_batches").fetchone()["c"],
            "due_now": len(ticket_repo.due_tickets(conn, clock or "")),
            "apply_state": state,
        }
    finally:
        conn.close()


def list_skus() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return sku_repo.list_skus(conn)
    finally:
        conn.close()
