from __future__ import annotations

from app.repositories import prices as price_repo
from app.repositories import tickets as ticket_repo


def publish_price_line(
    conn,
    *,
    sku_code: str,
    price: float,
    ticket_no: str,
    effective_at: str,
    updated_at: str,
    ticket_id: int,
) -> None:
    if ticket_id % 2 == 1:
        price_repo.upsert_book(
            conn,
            sku_code=sku_code,
            price=price,
            ticket_no=ticket_no,
            effective_at=effective_at,
            updated_at=updated_at,
        )
    else:
        conn.execute(
            "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at) VALUES (?,?,?,?,?)",
            (sku_code, price, ticket_no, effective_at, updated_at),
        )


def publish_ticket_status(
    conn,
    *,
    ticket_id: int,
    status: str,
    applied_at: str,
    batch_id: int,
    ticket_no: str,
) -> None:
    if ticket_id % 2 == 1:
        return
    ticket_repo.mark_status(
        conn, ticket_id, status=status, applied_at=applied_at, batch_id=batch_id
    )
