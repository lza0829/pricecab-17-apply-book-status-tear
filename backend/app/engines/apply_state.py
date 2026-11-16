"""应用状态收口（全局唯一口径）：以价签本 + 价格历史为准。

不变量：
- 一次成功写入 = 价签本改现值 + 价格历史追加一行，二者同事务、同来源单、同价。
- 改价单 ``status='applied'`` 当且仅当它的某条明细仍是价签本现行来源，
  且该商品价格历史末行同样指向该单、同价（即「有据可查」）。
- 某单曾在更早批次真实写入、但整单已被后批次覆盖时，记 ``covered`` 终态，
  不回退为 scheduled，避免旧价倒灌、被下一轮再扫。
- 批次 ``status='done'`` 的每条 ``applied`` 明细都必须有本批次时刻写出的
  价格历史行；无凭证的明细降级为 ``divergent``，该批次整体记为 ``divergent``，
  计数按真实写入集合回填，不再作为任何「已应用」计数或后续补写的凭证。
- 无任何写入痕迹却声称 applied 的单 → 回滚为 scheduled（可再次被到期扫描真正应用）；
  有价签凭证却仍 scheduled 的单 → 回补为 applied（不会再被扫进下一批）。
- 价签本与历史互相矛盾时，以历史审计链恢复价签本；价签来源单不存在或
  与明细价格不符时，同样从历史链回滚，不把幽灵写入当作生效。

收口只改「单状态 / 批次回执与计数 / 矛盾价签本」，不会凭空替改价单补写
新价格；所有修复在应用事务内先于到期扫描执行，因此分叉不会被再次应用。
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any

from app.repositories import prices as price_repo

EPS = 1e-9


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < EPS


def _history_row(
    conn: sqlite3.Connection,
    *,
    sku_code: str,
    price: float,
    ticket_no: str,
    recorded_at: str | None = None,
    batch_id: int | None = None,
) -> dict[str, Any] | None:
    sql = (
        "SELECT * FROM price_history "
        "WHERE sku_code=? AND source_ticket_no=? AND ABS(price - ?) < ?"
    )
    params: list[Any] = [sku_code, ticket_no, float(price), EPS]
    if recorded_at is not None:
        sql += " AND recorded_at=?"
        params.append(recorded_at)
    if batch_id is not None:
        sql += " AND batch_id=?"
        params.append(batch_id)
    sql += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _evidenced(
    conn: sqlite3.Connection, *, sku_code: str, price: float, ticket_no: str
) -> bool:
    """价签本现行来源 + 价格历史末行 + 改价明细是否共同指向该单该价。"""
    if not _ticket_line_exists(
        conn, ticket_no=ticket_no, sku_code=sku_code, price=price
    ):
        return False
    book = price_repo.get_book(conn, sku_code)
    if not book or book["source_ticket_no"] != ticket_no:
        return False
    if not _same(book["current_price"], price):
        return False
    hist = price_repo.last_history(conn, sku_code)
    return bool(hist) and hist["source_ticket_no"] == ticket_no and _same(hist["price"], price)


def _ticket_line_exists(
    conn: sqlite3.Connection, *, ticket_no: str, sku_code: str, price: float
) -> bool:
    row = conn.execute(
        '''
        SELECT 1
        FROM price_ticket_lines l
        JOIN price_tickets t ON t.id = l.ticket_id
        WHERE t.ticket_no=? AND l.sku_code=? AND ABS(l.new_price - ?) < ?
        LIMIT 1
        ''',
        (ticket_no, sku_code, float(price), EPS),
    ).fetchone()
    return row is not None


def _last_valid_history(conn: sqlite3.Connection, sku_code: str) -> dict[str, Any] | None:
    """最后一则可被改价明细解释（或初始种子）的历史行，用于回滚幽灵价签。"""
    rows = conn.execute(
        "SELECT * FROM price_history WHERE sku_code=? ORDER BY id DESC",
        (sku_code,),
    ).fetchall()
    for row in rows:
        h = dict(row)
        src = h["source_ticket_no"]
        if src is None:
            return h
        if _ticket_line_exists(
            conn, ticket_no=src, sku_code=sku_code, price=float(h["price"])
        ):
            return h
    return None


def _restore_book(conn: sqlite3.Connection, book: dict[str, Any], now: str) -> dict[str, Any]:
    """把幽灵/撕裂的价签行恢复到历史链可解释的最近一行。"""
    restored = _last_valid_history(conn, book["sku_code"])
    if restored is not None:
        price_repo.set_book(
            conn,
            sku_code=book["sku_code"],
            price=float(restored["price"]),
            ticket_no=restored["source_ticket_no"],
            effective_at=restored["effective_at"],
            updated_at=now,
        )
        return {
            "sku_code": book["sku_code"],
            "action": "book_restored_from_history",
            "from_ticket_no": book["source_ticket_no"],
            "to_ticket_no": restored["source_ticket_no"],
        }
    sku = conn.execute(
        "SELECT list_price FROM skus WHERE sku_code=?", (book["sku_code"],)
    ).fetchone()
    fallback = float(sku["list_price"]) if sku else float(book["current_price"])
    price_repo.set_book(
        conn,
        sku_code=book["sku_code"],
        price=fallback,
        ticket_no=None,
        effective_at=None,
        updated_at=now,
    )
    return {
        "sku_code": book["sku_code"],
        "action": "book_restored_to_initial",
        "from_ticket_no": book["source_ticket_no"],
        "to_ticket_no": None,
    }


def _repair_books(conn: sqlite3.Connection, now: str) -> list[dict[str, Any]]:
    """价签本为现行价权威；历史必须与它同源，未落到价签的旁路历史撤销。

    每个 SKU 的合法锚点 = 价签本来源单 + 现价对应的最后一条历史行：
    - 价签来源真实（改价明细存在）但历史缺锚 → 回补一条历史；
    - 锚点之后还有历史行 → 这些写入从未被价签本接受，撤销；
    - 价签来源本身不存在（幽灵写入）→ 从历史链回滚价签，再重新定锚。
    """
    repairs: list[dict[str, Any]] = []
    rows = conn.execute("SELECT * FROM price_book ORDER BY sku_code").fetchall()
    for row in rows:
        book = dict(row)
        src = book["source_ticket_no"]

        # 价签来源必须能被某条改价明细解释；解释不了就是幽灵写入，按历史链回滚。
        if src is not None and not _ticket_line_exists(
            conn, ticket_no=src, sku_code=book["sku_code"], price=float(book["current_price"])
        ):
            repairs.append(_restore_book(conn, book, now))
            book = price_repo.get_book(conn, book["sku_code"]) or book
            src = book["source_ticket_no"]

        # 定锚：价签当前（价, 来源单）在历史链上的对应行；缺失则回补一条。
        if src is None:
            anchor = conn.execute(
                "SELECT * FROM price_history WHERE sku_code=? AND source_ticket_no IS NULL "
                "AND ABS(price - ?) < ? ORDER BY id DESC LIMIT 1",
                (book["sku_code"], float(book["current_price"]), EPS),
            ).fetchone()
            anchor_id = int(anchor["id"]) if anchor else None
        else:
            anchor = _history_row(
                conn,
                sku_code=book["sku_code"],
                price=float(book["current_price"]),
                ticket_no=src,
            )
            anchor_id = int(anchor["id"]) if anchor else None

        if anchor_id is None:
            anchor_id = price_repo.append_history(
                conn,
                sku_code=book["sku_code"],
                price=float(book["current_price"]),
                ticket_no=src,
                effective_at=book["effective_at"] or now,
                recorded_at=now,
            )
            repairs.append(
                {
                    "sku_code": book["sku_code"],
                    "action": "history_backfilled",
                    "ticket_no": src,
                }
            )

        # 锚点之后的历史行从未被价签本接受：撤销，避免历史末行撒谎。
        cur = conn.execute(
            "DELETE FROM price_history WHERE sku_code=? AND id > ?",
            (book["sku_code"], anchor_id),
        )
        if cur.rowcount:
            repairs.append(
                {
                    "sku_code": book["sku_code"],
                    "action": "history_phantom_removed",
                    "removed": int(cur.rowcount),
                }
            )

        if src is not None and not _evidenced(
            conn,
            sku_code=book["sku_code"],
            price=float(book["current_price"]),
            ticket_no=src,
        ):
            repairs.append(
                {
                    "sku_code": book["sku_code"],
                    "action": "book_unresolved",
                    "ticket_no": src,
                }
            )
    return repairs


def _batch_lines(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        '''
        SELECT l.*, b.status AS batch_status, b.created_at AS batch_created_at
        FROM apply_batch_lines l
        JOIN apply_batches b ON b.id = l.batch_id
        ORDER BY l.batch_id, l.queue_pos, l.id
        '''
    ).fetchall()
    return [dict(r) for r in rows]


def _line_written(conn: sqlite3.Connection, line: dict[str, Any]) -> bool:
    """批明细行是否真的在本批次把价签推进到了本行的价。

    只看「有历史行」不够：历史可能是「只写历史、没改价签」的旁路产出的。
    凭证 = 带本批次号的价格历史行（同 SKU/价/来源单），且该行在价格历史链
    上不晚于当前价签所锚定的一行（即价签本确实走到了这一价，或被更晚的
    真实写入覆盖）。老库没有 batch_id 的历史行，再按批次时刻兜底比对。
    """
    hist = _history_row(
        conn,
        sku_code=line["sku_code"],
        price=float(line["new_price"]),
        ticket_no=line["ticket_no"],
        batch_id=line["batch_id"],
    )
    if hist is None:
        # 旧库（迁移前）历史行没有 batch_id：按批次时刻兜底比对。
        hist = _history_row(
            conn,
            sku_code=line["sku_code"],
            price=float(line["new_price"]),
            ticket_no=line["ticket_no"],
            recorded_at=line["created_at"],
        )
    if hist is None:
        return False
    book = price_repo.get_book(conn, line["sku_code"])
    if not book or book["source_ticket_no"] is None:
        return False
    anchor = _history_row(
        conn,
        sku_code=line["sku_code"],
        price=float(book["current_price"]),
        ticket_no=book["source_ticket_no"],
    )
    if anchor is None:
        return False
    return int(anchor["id"]) >= int(hist["id"])


def _repair_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """批次成功标记以真实写入痕迹为准；分叉批次永不再当生效凭证。"""
    repairs: list[dict[str, Any]] = []
    rows = _batch_lines(conn)
    by_batch: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in rows:
        if line["status"] == "applied" and not _line_written(conn, line):
            conn.execute(
                "UPDATE apply_batch_lines SET status='divergent' WHERE id=?",
                (line["id"],),
            )
            line["status"] = "divergent"
            repairs.append(
                {
                    "batch_id": line["batch_id"],
                    "ticket_no": line["ticket_no"],
                    "sku_code": line["sku_code"],
                    "action": "batch_line_downgraded",
                }
            )
        by_batch[line["batch_id"]].append(line)

    for batch in conn.execute("SELECT * FROM apply_batches ORDER BY id").fetchall():
        b = dict(batch)
        lines = by_batch.get(b["id"], [])
        applied = [l for l in lines if l["status"] == "applied"]
        skipped = [l for l in lines if l["status"] == "skipped_covered"]
        divergent = [l for l in lines if l["status"] == "divergent"]
        # 分叉一经确认即终态，不再回写为 done。
        status = "divergent" if divergent or b["status"] == "divergent" else b["status"]
        # 真实成功票数 = 至少写入一行的改价单数；写入行 = 实际落价签的行数。
        ticket_count = len({l["ticket_id"] for l in applied})
        line_count = len(applied)
        skipped_count = len(skipped)
        if (
            status != b["status"]
            or ticket_count != b["ticket_count"]
            or line_count != b["line_count"]
            or skipped_count != b["skipped_count"]
        ):
            conn.execute(
                '''
                UPDATE apply_batches
                SET status=?, ticket_count=?, line_count=?, skipped_count=?
                WHERE id=?
                ''',
                (status, ticket_count, line_count, skipped_count, b["id"]),
            )
            repairs.append(
                {
                    "batch_id": b["id"],
                    "batch_no": b["batch_no"],
                    "action": "batch_counts_aligned",
                    "status": status,
                    "ticket_count": ticket_count,
                    "line_count": line_count,
                    "skipped_count": skipped_count,
                }
            )
    return repairs


def _repair_tickets(conn: sqlite3.Connection, now: str) -> list[dict[str, Any]]:
    """单状态由价签凭证推导：现行有据 → applied；曾写入后被覆盖 → covered；
    全行被覆盖跳过 → covered；毫无写入痕迹的声明 → 回滚 scheduled。"""
    repairs: list[dict[str, Any]] = []
    parts_by_ticket: dict[int, list[dict[str, Any]]] = defaultdict(list)
    batch_status = {
        int(r["id"]): r["status"]
        for r in conn.execute("SELECT id, status FROM apply_batches")
    }
    for line in _batch_lines(conn):
        parts_by_ticket[line["ticket_id"]].append(line)

    for row in conn.execute("SELECT * FROM price_tickets ORDER BY id").fetchall():
        t = dict(row)
        ticket_lines = conn.execute(
            "SELECT * FROM price_ticket_lines WHERE ticket_id=? ORDER BY id", (t["id"],)
        ).fetchall()
        current_skus = set()
        for line in ticket_lines:
            if _evidenced(
                conn,
                sku_code=line["sku_code"],
                price=float(line["new_price"]),
                ticket_no=t["ticket_no"],
            ):
                current_skus.add(line["sku_code"])
        was_written = any(
            _history_row(
                conn,
                sku_code=line["sku_code"],
                price=float(line["new_price"]),
                ticket_no=t["ticket_no"],
            )
            is not None
            for line in ticket_lines
        )

        parts = parts_by_ticket.get(t["id"], [])
        done_parts = [p for p in parts if batch_status.get(p["batch_id"]) is not None]
        applied_parts = [p for p in done_parts if p["status"] == "applied"]
        skipped_parts = [p for p in done_parts if p["status"] == "skipped_covered"]

        if current_skus:
            status = "applied"
            matching = [p for p in applied_parts if p["sku_code"] in current_skus]
            batch_id = (
                max(p["batch_id"] for p in matching)
                if matching
                else t["apply_batch_id"]
            )
        elif applied_parts and was_written:
            # 曾真实写入、现已被后批次整单覆盖：终态 covered，不得再应用旧价。
            status = "covered"
            batch_id = max(p["batch_id"] for p in applied_parts)
        elif skipped_parts:
            # 已被队序在后的单覆盖（同批次或被价签本更晚来源覆盖）：终态 covered，
            # 不再反复被到期扫描（否则每轮都会生成一张永远跳过的批次）。
            status = "covered"
            batch_id = max(p["batch_id"] for p in skipped_parts)
        elif was_written:
            # 历史证明它曾真正落到价签（随后被更晚写入顶掉），但单状态停在
            # scheduled 且带可再扫的批号缺口：回补 covered，绝不重放旧价。
            status = "covered"
            written_batches = [
                int(p["batch_id"]) for p in done_parts if p["status"] == "applied"
            ]
            batch_id = max(written_batches) if written_batches else t["apply_batch_id"]
        else:
            # 无任何写入痕迹的 applied/covered 声明一律回滚；清掉批次指针
            # 以便重新到期应用（且不会被旧批次回执当作已生效）。
            status = "scheduled"
            batch_id = None

        if status == "scheduled":
            applied_at = None
        elif t["applied_at"] is not None:
            applied_at = t["applied_at"]
        elif done_parts:
            applied_at = max(p["batch_created_at"] for p in done_parts)
        else:
            applied_at = now

        if (
            t["status"] != status
            or t["applied_at"] != applied_at
            or t["apply_batch_id"] != batch_id
        ):
            conn.execute(
                "UPDATE price_tickets SET status=?, applied_at=?, apply_batch_id=? WHERE id=?",
                (status, applied_at, batch_id, t["id"]),
            )
            repairs.append(
                {
                    "ticket_no": t["ticket_no"],
                    "action": "ticket_status_aligned",
                    "from_status": t["status"],
                    "to_status": status,
                    "apply_batch_id": batch_id,
                }
            )
    return repairs


def reconcile_apply_state(conn: sqlite3.Connection, *, now: str) -> dict[str, Any]:
    """单一收口：修复价签/历史矛盾、批次回执与计数、改价单状态，再给出核验报告。"""
    repairs: list[dict[str, Any]] = []
    repairs.extend(_repair_books(conn, now))
    repairs.extend(_repair_batches(conn))
    repairs.extend(_repair_tickets(conn, now))
    report = audit(conn)
    report["repairs"] = repairs
    report["repaired"] = bool(repairs)
    return report


def audit(conn: sqlite3.Connection) -> dict[str, Any]:
    """互证视图：单状态 / 批次回执 / 价签本 / 历史 / 总览计数是否同源。"""
    tickets = [dict(r) for r in conn.execute("SELECT * FROM price_tickets ORDER BY id")]
    lines_by_ticket: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in conn.execute("SELECT * FROM price_ticket_lines ORDER BY ticket_id, id").fetchall():
        lines_by_ticket[line["ticket_id"]].append(dict(line))

    evidenced_ticket_ids: set[int] = set()
    evidenced_ticket_nos: set[str] = set()
    for t in tickets:
        if any(
            _evidenced(
                conn,
                sku_code=line["sku_code"],
                price=float(line["new_price"]),
                ticket_no=t["ticket_no"],
            )
            for line in lines_by_ticket.get(t["id"], [])
        ):
            evidenced_ticket_ids.add(t["id"])
            evidenced_ticket_nos.add(t["ticket_no"])

    sourced_books = []
    unresolved_skus = []
    phantom_source_skus = []
    for row in conn.execute("SELECT * FROM price_book ORDER BY sku_code").fetchall():
        book = dict(row)
        if book["source_ticket_no"] is None:
            # 价签自称初始价，历史末行却记着某张单：历史侧谎称已生效。
            last = price_repo.last_history(conn, book["sku_code"])
            if last and last["source_ticket_no"] is not None:
                phantom_source_skus.append(book["sku_code"])
            continue
        if _evidenced(
            conn,
            sku_code=book["sku_code"],
            price=float(book["current_price"]),
            ticket_no=book["source_ticket_no"],
        ):
            sourced_books.append(book)
        else:
            unresolved_skus.append(book["sku_code"])

    book_source_tickets = {b["source_ticket_no"] for b in sourced_books}
    tickets_by_no = {t["ticket_no"]: t for t in tickets}
    false_applied = sorted(
        t["ticket_no"]
        for t in tickets
        if t["status"] == "applied" and t["id"] not in evidenced_ticket_ids
    )
    ghost_scheduled = sorted(
        t["ticket_no"]
        for t in tickets
        if t["status"] == "scheduled" and t["id"] in evidenced_ticket_ids
    )
    # applied 单必须能在价签本里找到来源；价签来源单必须是 applied 状态。
    source_ticket_not_applied = sorted(
        no
        for no in book_source_tickets
        if no in tickets_by_no and tickets_by_no[no]["status"] != "applied"
    )
    applied_without_source = sorted(no for no in evidenced_ticket_nos if no not in book_source_tickets)

    batch_lines = _batch_lines(conn)
    # 分叉判定是活的：只要某条 applied 明细证明不了「本批次真把价签推进到该价」，
    # 无论库里存的批次状态是 done 还是已隔离的 divergent，都算分叉明细。
    live_divergent = [
        line
        for line in batch_lines
        if line["status"] == "applied" and not _line_written(conn, line)
    ]
    divergent_batches = sorted(
        {
            line["batch_id"]
            for line in batch_lines
            if line["status"] == "divergent" or line["batch_status"] == "divergent"
        }
        | {line["batch_id"] for line in live_divergent}
    )
    divergent_lines = [
        {
            "batch_id": line["batch_id"],
            "ticket_no": line["ticket_no"],
            "sku_code": line["sku_code"],
            "batch_status": line["batch_status"],
        }
        for line in batch_lines
        if line["status"] in ("divergent",)
    ] + [
        {
            "batch_id": line["batch_id"],
            "ticket_no": line["ticket_no"],
            "sku_code": line["sku_code"],
            "batch_status": line["batch_status"],
        }
        for line in live_divergent
        if line["status"] != "divergent"
    ]
    divergent_ids = {line["id"] for line in live_divergent} | {
        line["id"] for line in batch_lines if line["status"] == "divergent"
    }
    # 仍标 done 却已有 divergent 明细 = 尚未隔离的活分叉；标 divergent 的批次是已隔离的
    # 历史残迹（永不再当生效凭证），不阻断新应用，但会一直出现在报告里。
    unquarantined_divergent = [
        d
        for d in divergent_lines
        if d["batch_status"] != "divergent" and d["batch_id"] not in {
            line["batch_id"] for line in batch_lines if line["batch_status"] == "divergent"
        }
    ]

    # 每个「未分叉」批次的 applied 明细商品集合必须等于该批次真实写出的价签集合。
    written_by_batch: dict[int, set[str]] = defaultdict(set)
    claimed_by_batch: dict[int, set[str]] = defaultdict(set)
    quarantined = [
        b for b in divergent_batches
        if b not in {d["batch_id"] for d in unquarantined_divergent}
    ]
    for line in batch_lines:
        if line["status"] == "applied" and line["id"] not in divergent_ids:
            claimed_by_batch[line["batch_id"]].add(line["sku_code"])
    for row in conn.execute(
        "SELECT sku_code, source_ticket_no, batch_id FROM price_history "
        "WHERE source_ticket_no IS NOT NULL AND batch_id IS NOT NULL"
    ).fetchall():
        line = conn.execute(
            "SELECT 1 FROM apply_batch_lines "
            "WHERE batch_id=? AND ticket_no=? AND sku_code=? AND status='applied' LIMIT 1",
            (row["batch_id"], row["source_ticket_no"], row["sku_code"]),
        ).fetchone()
        if line:
            written_by_batch[int(row["batch_id"])].add(row["sku_code"])
    batch_sku_mismatch = []
    for batch_row in conn.execute(
        "SELECT id, status FROM apply_batches ORDER BY id"
    ).fetchall():
        batch_id = int(batch_row["id"])
        if batch_id in quarantined or batch_row["status"] == "divergent":
            continue
        claimed = claimed_by_batch.get(batch_id, set())
        if claimed != written_by_batch.get(batch_id, set()):
            batch_sku_mismatch.append(
                {
                    "batch_id": batch_id,
                    "claimed_skus": sorted(claimed),
                    "written_skus": sorted(written_by_batch.get(batch_id, set())),
                }
            )

    counts_match = len(evidenced_ticket_ids) == len(book_source_tickets)
    claimed_applied = [t["ticket_no"] for t in tickets if t["status"] == "applied"]
    consistent = (
        not unresolved_skus
        and not phantom_source_skus
        and not false_applied
        and not ghost_scheduled
        and not source_ticket_not_applied
        and not applied_without_source
        and not unquarantined_divergent
        and not batch_sku_mismatch
        and len(claimed_applied) == len(evidenced_ticket_ids)
    )

    return {
        "consistent": consistent,
        # 票单位：当前价签/历史末行可互证的 applied 票数（= 可按来源单溯源的票集合）。
        "applied_ticket_count": len(evidenced_ticket_ids),
        "book_source_ticket_count": len(book_source_tickets),
        # 单状态字面值（可能撒谎，用于对照）。
        "claimed_applied_count": len(claimed_applied),
        # 价签本里可按来源单溯源的 SKU 数。
        "book_source_sku_count": len(sourced_books),
        "counts_match": counts_match,
        "scheduled_ticket_count": sum(
            1
            for t in tickets
            if t["status"] == "scheduled" and t["id"] not in evidenced_ticket_ids
        ),
        "false_applied_tickets": false_applied,
        "ghost_scheduled_tickets": ghost_scheduled,
        "source_ticket_not_applied": source_ticket_not_applied,
        "applied_without_source": applied_without_source,
        "divergent_batches": divergent_batches,
        "divergent_batch_count": len(divergent_batches),
        "quarantined_batches": sorted(quarantined),
        "divergent_lines": divergent_lines,
        "divergent_line_count": len(divergent_lines),
        "unquarantined_divergent_line_count": len(unquarantined_divergent),
        "batch_sku_mismatch": batch_sku_mismatch,
        "unresolved_skus": unresolved_skus,
        "phantom_source_skus": phantom_source_skus,
        "evidenced_ticket_nos": sorted(evidenced_ticket_nos),
    }
