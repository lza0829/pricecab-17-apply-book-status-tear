from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines import apply_state
from app.repositories import settings as settings_repo
from app.repositories import tickets as ticket_repo
from app.repositories import prices as price_repo
from app.repositories import batches as batch_repo

# 同商品冲突策略（全局唯一，刻意不做成可切换开关）：
# 后写覆盖 —— 队序（先生效时刻、再单号）最后一张到期单的新价写入价签本与历史；
# 前列同商品行记为 skipped_covered（被覆盖跳过），批次仍然成功，
# 跳过数与来源单号落在 apply_batch_lines 中可核。
CONFLICT_STRATEGY = "last_write_wins"

# 「应用到期」与「拨业务钟 / 第二次应用到期」互斥：非阻塞取锁，取不到即拒绝（409），
# 不排队、不写半截价签。
APPLY_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_clock() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "business_clock": settings_repo.get(conn, "business_clock"),
            "clock_label": settings_repo.get(conn, "clock_label"),
        }
    finally:
        conn.close()


def set_clock(clock_at: str) -> dict[str, Any]:
    clock_at = (clock_at or "").strip()
    if not clock_at:
        raise HTTPException(400, "业务钟不能为空")
    if not APPLY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "应用到期进行中，禁止拨动业务钟")
    try:
        conn = connect(immediate=True)
        try:
            settings_repo.set_value(conn, "business_clock", clock_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        APPLY_LOCK.release()
    return get_clock()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_batches(conn)
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = batch_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(404, "批次不存在")
        batch["lines"] = batch_repo.list_batch_lines(conn, batch_id)
        return batch
    finally:
        conn.close()


def get_apply_state() -> dict[str, Any]:
    """只读核验视图：单状态、批次回执、价签本、历史是否同源。"""
    conn = connect()
    try:
        return apply_state.audit(conn)
    finally:
        conn.close()


def _build_queue(conn, due: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten due tickets (already in queue order) into per-line queue entries."""
    queue: list[dict[str, Any]] = []
    for pos, t in enumerate(due, start=1):
        for line in ticket_repo.list_lines(conn, t["id"]):
            queue.append(
                {
                    "queue_pos": pos,
                    "ticket_id": t["id"],
                    "ticket_no": t["ticket_no"],
                    "effective_at": t["effective_at"],
                    "sku_code": line["sku_code"],
                    "new_price": float(line["new_price"]),
                }
            )
    return queue


def _due_ticket_ids(due: list[dict[str, Any]]) -> set[int]:
    return {t["id"] for t in due}


def _prior_book_source(conn, sku_code: str) -> dict[str, Any] | None:
    """价签本当前来源（已生效写入），用于与到期队列一起排序决胜。"""
    book = price_repo.get_book(conn, sku_code)
    if not book or book["source_ticket_no"] is None:
        return None
    return book


def _mark_overwrites(queue: list[dict[str, Any]], conn, due_ticket_ids: set[int]) -> None:
    """后写覆盖：同一商品只保留队序最后一行（且不倒退已生效的更新价）。

    队序（effective_at、ticket_no）不仅覆盖本批次内的前列行，也覆盖「价签本
    已由更晚生效的单写成」的情形：早于已生效来源的到期行记 skipped_covered，
    绝不把旧价倒灌回价签本。
    """
    last_idx_by_sku: dict[str, int] = {}
    for idx, item in enumerate(queue):
        last_idx_by_sku[item["sku_code"]] = idx
    prior: dict[str, dict[str, Any] | None] = {}
    for item in queue:
        sku = item["sku_code"]
        if sku in prior:
            continue
        prior[sku] = None
        book = _prior_book_source(conn, sku)
        # 已生效来源若也在本队列中，由队列自身决胜，避免自我覆盖。
        if not book or not book["source_ticket_no"]:
            continue
        source_ticket = conn.execute(
            "SELECT id, ticket_no, effective_at FROM price_tickets WHERE ticket_no=?",
            (book["source_ticket_no"],),
        ).fetchone()
        if source_ticket is None or int(source_ticket["id"]) in due_ticket_ids:
            continue
        prior[sku] = {
            "ticket_no": source_ticket["ticket_no"],
            "effective_at": source_ticket["effective_at"],
        }

    for idx, item in enumerate(queue):
        sku = item["sku_code"]
        last = queue[last_idx_by_sku[sku]]
        if last is not item:
            item["status"] = "skipped_covered"
            item["covered_by_ticket_no"] = last["ticket_no"]
            continue
        prior_source = prior.get(sku)
        if prior_source and (
            prior_source["effective_at"] > item["effective_at"]
            or (
                prior_source["effective_at"] == item["effective_at"]
                and prior_source["ticket_no"] > item["ticket_no"]
            )
        ):
            # 价签本已由更晚生效的单写成：本行是迟到/回滚产生的旧价，不得倒灌。
            item["status"] = "skipped_covered"
            item["covered_by_ticket_no"] = prior_source["ticket_no"]
        else:
            item["status"] = "applied"
            item["covered_by_ticket_no"] = None


def _assert_no_divergence(conn) -> None:
    """收口后仍有分叉（例如价签指向未知来源且历史也无从解释）则拒绝应用。

    收口本身在事务内已修复单状态/批次回执/可恢复的价签；剩余 unresolved
    意味着价签本与历史都无法自洽，此时再应用只会叠加更多谎言。
    """
    report = apply_state.audit(conn)
    if report["unresolved_skus"]:
        raise HTTPException(
            409,
            "价签本与价格历史存在未收口分叉，禁止应用到期："
            + ", ".join(report["unresolved_skus"]),
        )


def apply_due() -> dict[str, Any]:
    """Apply all scheduled tickets whose effective_at <= business clock.

    单事务、单队列快照：批次行、批明细、价签本、价格历史、票据状态同源写出；
    写前先按「价签本 + 历史为准」收口既有分叉，写后按真实写入集合回填回执。
    任何异常整体回滚，不留半截价签。
    """
    if not APPLY_LOCK.acquire(blocking=False):
        raise HTTPException(409, "已有应用到期批次进行中，本次应用被拒绝")
    try:
        conn = connect(immediate=True)
        try:
            clock = settings_repo.get(conn, "business_clock")
            if not clock:
                raise HTTPException(400, "业务钟未设置")

            # 1) 先收口：价签/历史为准回补或回滚单状态、批次成功标记与计数。
            now = _now()
            reconciled = apply_state.reconcile_apply_state(conn, now=now)
            _assert_no_divergence(conn)
            if reconciled["repaired"]:
                # 互斥收口：发现分叉的这一轮只收口、不应用。分叉未清前绝不允许
                # 同商品再次应用到期，避免把修复和写入混在同一拍里。
                conn.commit()
                return {
                    "applied": False,
                    "reason": "divergence_reconciled",
                    "message": "发现并收口了分叉（以价签本与价格历史为准），本轮不应用到期；请复核后再次应用",
                    "business_clock": clock,
                    "strategy": CONFLICT_STRATEGY,
                    "ticket_count": 0,
                    "line_count": 0,
                    "skipped_count": 0,
                    "reconciled": reconciled,
                }

            # 2) 再扫到期：已被收口回补为 applied 的单带批号，不会再进队列。
            due = ticket_repo.due_tickets(conn, clock)
            if not due:
                conn.commit()
                return {
                    "applied": False,
                    "message": "当前业务钟下没有到期改价单",
                    "business_clock": clock,
                    "strategy": CONFLICT_STRATEGY,
                    "ticket_count": 0,
                    "line_count": 0,
                    "skipped_count": 0,
                    "reconciled": reconciled,
                }

            queue = _build_queue(conn, due)
            _mark_overwrites(queue, conn, _due_ticket_ids(due))
            applied_lines = [i for i in queue if i["status"] == "applied"]
            skipped_lines = [i for i in queue if i["status"] == "skipped_covered"]
            applied_ticket_ids = {i["ticket_id"] for i in applied_lines}

            created = now
            batch_no = batch_repo.next_batch_no(conn)
            batch_id = batch_repo.create_batch(
                conn,
                batch_no=batch_no,
                clock_at=clock,
                ticket_count=0,
                line_count=0,
                skipped_count=0,
                strategy=CONFLICT_STRATEGY,
                status="pending",
                created_at=created,
            )

            written: list[dict[str, Any]] = []
            from app.services import projection as projection_svc
            for item in queue:
                if item["status"] == "applied":
                    projection_svc.publish_price_line(
                        conn,
                        sku_code=item["sku_code"],
                        price=item["new_price"],
                        ticket_no=item["ticket_no"],
                        effective_at=item["effective_at"],
                        updated_at=created,
                        ticket_id=item["ticket_id"],
                        batch_id=batch_id,
                    )
                    written.append(item)
                batch_repo.add_batch_line(
                    conn,
                    batch_id=batch_id,
                    queue_pos=item["queue_pos"],
                    ticket_id=item["ticket_id"],
                    ticket_no=item["ticket_no"],
                    sku_code=item["sku_code"],
                    new_price=item["new_price"],
                    status=item["status"],
                    covered_by_ticket_no=item["covered_by_ticket_no"],
                    created_at=created,
                )
            for t in due:
                # 整单全部行都被覆盖（本批次内或价签已有更晚来源）才记 covered；
                # 有行真正落价签的单才是 applied。
                status = "applied" if t["id"] in applied_ticket_ids else "covered"
                projection_svc.publish_ticket_status(
                    conn,
                    ticket_id=t["id"],
                    status=status,
                    applied_at=created if status == "applied" else None,
                    batch_id=batch_id,
                    ticket_no=t["ticket_no"],
                )

            # 3) 回执以真实写入集合为准（票数=有行落价签的单数，写入行=落价签行数）。
            actual_ticket_count = len({i["ticket_id"] for i in written})
            batch_repo.update_batch_receipt(
                conn,
                batch_id,
                ticket_count=actual_ticket_count,
                line_count=len(written),
                skipped_count=len(skipped_lines),
                status="done",
            )

            # 4) 写后按同一口径收口：被本批次顶掉的旧单从 applied 转 covered，
            #    批次回执与计数以真实写入集合为准；随后自检，任何谎言整批回滚。
            post_reconcile = apply_state.reconcile_apply_state(conn, now=created)
            post = apply_state.audit(conn)
            if not post["consistent"]:
                raise HTTPException(
                    409,
                    "应用后核验不一致，已整体回滚："
                    f"false_applied={post['false_applied_tickets']} "
                    f"ghost_scheduled={post['ghost_scheduled_tickets']} "
                    f"divergent_batches={post['divergent_batches']}",
                )

            conn.commit()
            receipt = batch_repo.get_batch(conn, batch_id) or {}
            return {
                "applied": True,
                "batch_id": batch_id,
                "batch_no": batch_no,
                "strategy": CONFLICT_STRATEGY,
                "business_clock": clock,
                # 回执直接取库内已按真实写入回填的批次行，响应与批次/明细同源。
                "ticket_count": int(receipt["ticket_count"]),
                "line_count": int(receipt["line_count"]),
                "skipped_count": int(receipt["skipped_count"]),
                "tickets": [t["ticket_no"] for t in due if t["id"] in applied_ticket_ids],
                "covered_tickets": [t["ticket_no"] for t in due if t["id"] not in applied_ticket_ids],
                "written_skus": sorted({i["sku_code"] for i in written}),
                "reconciled": reconciled,
                "post_reconcile": post_reconcile,
                "verification": post,
                "lines": [
                    {
                        "ticket_no": i["ticket_no"],
                        "sku_code": i["sku_code"],
                        "new_price": i["new_price"],
                        "status": i["status"],
                        "covered_by_ticket_no": i["covered_by_ticket_no"],
                    }
                    for i in queue
                ],
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    finally:
        APPLY_LOCK.release()
