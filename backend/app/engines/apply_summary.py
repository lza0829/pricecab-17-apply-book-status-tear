"""总览计数：与 apply_state.audit 同一口径，不再直接数批次行或历史行。"""
from __future__ import annotations

import sqlite3

from app.engines import apply_state


def applied_ticket_count(conn: sqlite3.Connection) -> int:
    """已应用 = 价签本/历史末行仍有凭证的改价单数（而非批次行数）。"""
    return apply_state.audit(conn)["applied_ticket_count"]


def book_source_count(conn: sqlite3.Connection) -> int:
    """可按来源单溯源的价签数：来源单必须能与价签 + 历史互证的 SKU 数。"""
    return apply_state.audit(conn)["book_source_sku_count"]


def audit(conn: sqlite3.Connection) -> dict:
    return apply_state.audit(conn)
