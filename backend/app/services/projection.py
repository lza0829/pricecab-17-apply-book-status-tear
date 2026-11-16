"""应用投影：价签本、价格历史、改价单状态同源落库。

这里不再有任何按票号奇偶/批次位置分支的「旁路」：每一次成功应用都同时
写入价签本现行价与一条价格历史，并在同一事务内推进改价单状态。
"""
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
    batch_id: int | None = None,
) -> None:
    # ticket_id 保留在签名里供调用方追溯；写入口径不再因票号而异。
    price_repo.upsert_book(
        conn,
        sku_code=sku_code,
        price=price,
        ticket_no=ticket_no,
        effective_at=effective_at,
        updated_at=updated_at,
        batch_id=batch_id,
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
    ticket_repo.set_status(
        conn,
        ticket_id,
        status=status,
        applied_at=applied_at,
        batch_id=batch_id,
    )
