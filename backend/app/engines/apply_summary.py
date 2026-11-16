from __future__ import annotations

import sqlite3


def applied_ticket_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM apply_batches WHERE status='done'"
    ).fetchone()
    return int(row["c"] if row else 0)


def book_source_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM price_book WHERE source_ticket_no IS NOT NULL"
    ).fetchone()
    return int(row["c"] if row else 0)
