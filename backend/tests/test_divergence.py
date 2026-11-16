"""分叉（宣告与价签分叉）的夹具测试。

用夹具直接在库里打出三类真实分叉，再经「应用到期 / 内核验」收口，
对照批次、批明细、单状态、价签本、价格历史、总览互证：

1. 回执成功但价签旧：批量成功、明细 applied、单 applied，价签本却还是旧价
   （历史里甚至多了一条来源对不上的旁路行）；另含「宣称成功但一个字都没写」。
2. 价签新但单仍 scheduled 可再扫：价签本与历史末行都已指向该单，单状态却
   停在 scheduled、无批次号，下一轮「应用到期」会再次把它扫进去。
3. 票数 ≠ 写入集合：批次回执写 2 票 / 3 行，实际只有 1 票 1 行真正落到价签。

另覆盖：分叉批次永不再当生效凭证、新单顶掉旧单、老库奇偶旁路遗留撕裂、
迟到旧单不得倒退已生效新价。所有收口都对照批次、批明细、单状态、价签本、
价格历史与总览计数互证。

收口口径全局唯一：以价签本 + 价格历史为准（见 app/engines/apply_state.py）。
"""
from __future__ import annotations

SEED_CLOCK = "2026-11-16T09:00:00Z"


def make_ticket(client, title, effective_at, lines):
    r = client.post(
        "/api/tickets",
        json={"title": title, "effective_at": effective_at, "lines": lines},
    )
    assert r.status_code == 200, r.text
    return r.json()


def book_by_sku(client):
    return {b["sku_code"]: b for b in client.get("/api/prices/book").json()}


def tickets_by_no(client):
    return {t["ticket_no"]: t for t in client.get("/api/tickets").json()}


def state(client):
    r = client.get("/api/apply/state")
    assert r.status_code == 200, r.text
    return r.json()


def apply_due(client):
    return client.post("/api/apply/due")


# ---------------------------------------------------------------- 场景 1
def test_receipt_success_but_book_still_old(client):
    """回执成功、单 applied，价签本却仍是旧价（来源单与明细价对不上）。"""
    mk = make_ticket(client, "茶调价", "2026-11-16T08:00:00Z",
                     [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])

    # 正常应用一次，形成真实凭证。
    ok = apply_due(client).json()
    assert ok["applied"] is True
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 25.0

    import app.db as db

    conn = db.connect()
    try:
        # 撕裂：价签本退回旧价 28.0，却仍挂着该单号（来源单与明细 25.0 对不上）；
        # 历史末行还多了一条来源单不存在的 19.9 旁路行。
        conn.execute(
            "UPDATE price_book SET current_price=28.0, effective_at=?, updated_at=? "
            "WHERE sku_code='SKU-TEA-001'",
            (SEED_CLOCK, SEED_CLOCK),
        )
        conn.execute(
            "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at) "
            "VALUES ('SKU-TEA-001', 19.9, 'PT-9999', ?, ?)",
            (SEED_CLOCK, SEED_CLOCK),
        )
        conn.commit()
    finally:
        conn.close()

    # 分叉可见：单 applied 但价签凭证不在，总览与来源可溯源数对不上。
    before = state(client)
    assert before["consistent"] is False
    assert before["false_applied_tickets"] == [mk["ticket_no"]]
    assert "SKU-TEA-001" in before["unresolved_skus"]
    dash = client.get("/api/dashboard").json()
    assert dash["applied_count"] == 0
    assert dash["book_source_count"] == 0
    assert dash["applied_claimed_count"] == 1

    # 收口轮：只收口、不应用（互斥），以历史里的真实写入恢复价签。
    r = apply_due(client).json()
    assert r["applied"] is False
    assert r["reason"] == "divergence_reconciled"
    actions = {(x["action"], x.get("sku_code")) for x in r["reconciled"]["repairs"]}
    assert ("book_restored_from_history", "SKU-TEA-001") in actions
    assert ("history_phantom_removed", "SKU-TEA-001") in actions

    # 互证：价签回到 25.0 且来源单 = 该单；19.9 旁路历史被撤销。
    book = book_by_sku(client)["SKU-TEA-001"]
    assert book["current_price"] == 25.0
    assert book["source_ticket_no"] == mk["ticket_no"]
    hist = client.get("/api/prices/history", params={"sku_code": "SKU-TEA-001"}).json()
    assert hist[0]["price"] == 25.0 and hist[0]["source_ticket_no"] == mk["ticket_no"]
    assert all(h["price"] != 19.9 for h in hist)
    assert tickets_by_no(client)[mk["ticket_no"]]["status"] == "applied"
    after = state(client)
    assert after["consistent"] is True
    dash = client.get("/api/dashboard").json()
    assert dash["applied_count"] == dash["book_source_count"] == 1
    assert dash["applied_claimed_count"] == 1

    # 再应用不会重放旧价：无到期单。
    again = apply_due(client).json()
    assert again["applied"] is False
    assert again["message"] == "当前业务钟下没有到期改价单"
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 25.0


def test_success_receipt_with_no_write_at_all_rolls_ticket_back(client):
    """回执说成功、单 applied，但价签与历史都没有任何写入 → 回滚单状态为可再应用。"""
    mk = make_ticket(client, "零食调价", "2026-11-16T08:30:00Z",
                     [{"sku_code": "SKU-SNK-011", "new_price": 10.0}])

    import app.db as db

    conn = db.connect()
    try:
        now = SEED_CLOCK
        conn.execute(
            "INSERT INTO apply_batches(batch_no, clock_at, ticket_count, line_count, "
            "skipped_count, strategy, status, created_at) "
            "VALUES ('AB-9001', ?, 1, 1, 0, 'last_write_wins', 'done', ?)",
            (SEED_CLOCK, now),
        )
        batch_id = conn.execute("SELECT id FROM apply_batches").fetchone()["id"]
        tid = conn.execute(
            "SELECT id FROM price_tickets WHERE ticket_no=?", (mk["ticket_no"],)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO apply_batch_lines(batch_id, queue_pos, ticket_id, ticket_no, "
            "sku_code, new_price, status, covered_by_ticket_no, created_at) "
            "VALUES (?, 1, ?, ?, 'SKU-SNK-011', 10.0, 'applied', NULL, ?)",
            (batch_id, tid, mk["ticket_no"], now),
        )
        conn.execute(
            "UPDATE price_tickets SET status='applied', applied_at=?, apply_batch_id=? WHERE id=?",
            (now, batch_id, tid),
        )
        conn.commit()
    finally:
        conn.close()

    before = state(client)
    assert before["consistent"] is False
    assert before["false_applied_tickets"] == [mk["ticket_no"]]
    assert before["divergent_batch_count"] == 1
    assert client.get("/api/dashboard").json()["applied_count"] == 0

    r = apply_due(client).json()
    assert r["applied"] is False and r["reason"] == "divergence_reconciled"

    # 单回滚为 scheduled 且清掉批号；分叉批次被隔离，回执按 0 写入回填。
    ticket = tickets_by_no(client)[mk["ticket_no"]]
    assert ticket["status"] == "scheduled"
    assert ticket["apply_batch_id"] is None
    batch = client.get("/api/apply/batches").json()[0]
    assert batch["status"] == "divergent"
    assert batch["ticket_count"] == 0 and batch["line_count"] == 0
    assert book_by_sku(client)["SKU-SNK-011"]["current_price"] == 12.5

    # 下一轮才真正写入，且本轮才是唯一生效凭证。
    applied = apply_due(client).json()
    assert applied["applied"] is True
    assert applied["written_skus"] == ["SKU-SNK-011"]
    assert book_by_sku(client)["SKU-SNK-011"]["current_price"] == 10.0
    assert tickets_by_no(client)[mk["ticket_no"]]["status"] == "applied"
    assert state(client)["consistent"] is True


# ---------------------------------------------------------------- 场景 2
def test_book_new_but_ticket_still_scheduled_and_rescannable(client):
    """价签已是新价、历史有新行，单却 scheduled 无批号 → 回补 applied，禁止再扫。"""
    t1 = make_ticket(client, "茶调价", "2026-11-16T08:00:00Z",
                     [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])
    t2 = make_ticket(client, "零食调价", "2026-11-16T08:30:00Z",
                     [{"sku_code": "SKU-SNK-011", "new_price": 10.0}])
    assert apply_due(client).json()["applied"] is True

    import app.db as db

    conn = db.connect()
    try:
        # 人为把 t1 的单状态退回 scheduled 并抹掉批号：价签与历史仍是它写的。
        conn.execute(
            "UPDATE price_tickets SET status='scheduled', applied_at=NULL, apply_batch_id=NULL "
            "WHERE ticket_no=?",
            (t1["ticket_no"],),
        )
        conn.commit()
    finally:
        conn.close()

    before = state(client)
    assert before["consistent"] is False
    assert before["ghost_scheduled_tickets"] == [t1["ticket_no"]]

    # 到期待应用数把幽灵单算进去了——这正是「可再扫」的风险面。
    assert client.get("/api/dashboard").json()["due_now"] == 1

    r = apply_due(client).json()
    assert r["applied"] is False
    assert r["reason"] == "divergence_reconciled"
    assert any(
        x["action"] == "ticket_status_aligned"
        and x["ticket_no"] == t1["ticket_no"]
        and x["to_status"] == "applied"
        for x in r["reconciled"]["repairs"]
    )

    # 回补后：单 applied 带批号，不再是待应用；价签未被旧价二次写入。
    ticket = tickets_by_no(client)[t1["ticket_no"]]
    assert ticket["status"] == "applied"
    assert ticket["apply_batch_id"] is not None
    assert client.get("/api/dashboard").json()["due_now"] == 0
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 25.0
    hist = client.get("/api/prices/history", params={"sku_code": "SKU-TEA-001"}).json()
    assert [h["source_ticket_no"] for h in hist[:1]] == [t1["ticket_no"]]
    assert state(client)["consistent"] is True

    # 再应用：无到期单，不会把 t1 的价重放一遍。
    again = apply_due(client).json()
    assert again["applied"] is False
    assert again["message"] == "当前业务钟下没有到期改价单"
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 25.0
    assert tickets_by_no(client)[t2["ticket_no"]]["status"] == "applied"


# ---------------------------------------------------------------- 场景 3
def test_batch_counts_lie_about_written_set(client):
    """回执 2 票 3 行，实际只落 1 票 1 行 → 回执按真实写入集合回填，分叉批次隔离。"""
    make_ticket(client, "茶与零食", "2026-11-16T08:00:00Z",
                [{"sku_code": "SKU-TEA-001", "new_price": 25.0},
                 {"sku_code": "SKU-SNK-011", "new_price": 10.0}])
    make_ticket(client, "日用", "2026-11-16T08:30:00Z",
                [{"sku_code": "SKU-DAY-021", "new_price": 40.0}])

    ok = apply_due(client).json()
    assert ok["applied"] is True
    assert ok["ticket_count"] == 2 and ok["line_count"] == 3

    import app.db as db

    conn = db.connect()
    # 只把「茶与零食」这一票的 SKU-TEA-001 真正写进价签与历史；
    # 其余两行（零食、日用）的写入痕迹全部抹掉，再把回执吹成 2 票 3 行。
    conn.execute(
        "DELETE FROM price_history WHERE sku_code IN ('SKU-SNK-011','SKU-DAY-021') "
        "AND source_ticket_no IS NOT NULL"
    )
    conn.execute(
        "UPDATE price_book SET current_price=12.5, source_ticket_no=NULL, updated_at=? "
        "WHERE sku_code='SKU-SNK-011'",
        (SEED_CLOCK,),
    )
    conn.execute(
        "UPDATE price_book SET current_price=45.0, source_ticket_no=NULL, updated_at=? "
        "WHERE sku_code='SKU-DAY-021'",
        (SEED_CLOCK,),
    )
    conn.execute(
        "UPDATE apply_batches SET ticket_count=2, line_count=3, skipped_count=0, status='done'"
    )
    conn.execute("UPDATE price_tickets SET status='applied'")
    conn.commit()
    conn.close()

    before = state(client)
    assert before["consistent"] is False
    assert before["divergent_batch_count"] == 1
    assert before["divergent_line_count"] == 2

    r = apply_due(client).json()
    assert r["applied"] is False
    assert r["reason"] == "divergence_reconciled"

    # 批次回执按真实写入集合回填：1 票 1 行，状态 divergent（不再当生效凭证）。
    batches = client.get("/api/apply/batches").json()
    b = batches[0]
    assert b["status"] == "divergent"
    assert b["ticket_count"] == 1
    assert b["line_count"] == 1
    detail = client.get(f"/api/apply/batches/{b['id']}").json()
    divergent = [l for l in detail["lines"] if l["status"] == "divergent"]
    assert {l["sku_code"] for l in divergent} == {"SKU-SNK-011", "SKU-DAY-021"}
    applied = [l for l in detail["lines"] if l["status"] == "applied"]
    assert {l["sku_code"] for l in applied} == {"SKU-TEA-001"}

    # 单状态与真实价签凭证一致：只有茶这一单 applied，其余可重新到期应用。
    tickets = tickets_by_no(client)
    by_title = {t["title"]: t for t in tickets.values()}
    assert by_title["茶与零食"]["status"] == "applied"
    assert by_title["日用"]["status"] == "scheduled"

    # 已隔离的分叉批次带 divergent 明细但不再阻断新应用（consistent 视其为历史残迹）。
    after = state(client)
    assert after["unquarantined_divergent_line_count"] == 0
    assert after["divergent_batch_count"] == 1
    assert after["consistent"] is True

    # 下一轮把未真正落价签的日用单真正应用，回执与写入集合一致。
    applied_run = apply_due(client).json()
    assert applied_run["applied"] is True
    assert applied_run["ticket_count"] == 1
    assert applied_run["line_count"] == 1
    assert applied_run["written_skus"] == ["SKU-DAY-021"]
    assert book_by_sku(client)["SKU-DAY-021"]["current_price"] == 40.0
    assert book_by_sku(client)["SKU-SNK-011"]["current_price"] == 12.5
    assert state(client)["consistent"] is True


# ---------------------------------------------------------------- 场景 4
def test_divergent_batch_never_counts_as_applied_proof(client):
    """分叉批次不得再当生效凭证：即便批明细仍标 applied，总览也不认它。"""
    make_ticket(client, "茶调价", "2026-11-16T08:00:00Z",
                [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])
    assert apply_due(client).json()["applied"] is True

    import app.db as db

    conn = db.connect()
    conn.execute("UPDATE apply_batches SET status='divergent'")
    conn.execute(
        "UPDATE price_book SET current_price=28.0, source_ticket_no=NULL WHERE sku_code='SKU-TEA-001'"
    )
    conn.execute(
        "DELETE FROM price_history WHERE sku_code='SKU-TEA-001' AND source_ticket_no IS NOT NULL"
    )
    conn.commit()
    conn.close()

    # 收口前：单仍号称 applied，但已无可溯来源，总览不认它。
    before = client.get("/api/dashboard").json()
    assert before["applied_count"] == 0
    assert before["book_source_count"] == 0
    assert before["applied_claimed_count"] == 1
    r = apply_due(client).json()
    assert r["reason"] == "divergence_reconciled"
    dash = client.get("/api/dashboard").json()
    assert dash["applied_count"] == 0
    assert dash["book_source_count"] == 0
    assert dash["applied_claimed_count"] == 0
    assert state(client)["consistent"] is True


# ---------------------------------------------------------------- 场景 5
def test_newer_ticket_supersedes_applied_ticket_consistently(client):
    """同商品新单生效后：旧单转 covered，价签/历史只指向新单，回执与写入集合一致。"""
    old = make_ticket(client, "旧价", "2026-11-16T08:00:00Z",
                      [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])
    new = make_ticket(client, "新价", "2026-11-16T10:00:00Z",
                      [{"sku_code": "SKU-TEA-001", "new_price": 22.0}])

    assert client.put("/api/apply/clock",
                      json={"business_clock": "2026-11-16T08:30:00Z"}).status_code == 200
    first = apply_due(client).json()
    assert first["applied"] is True
    assert tickets_by_no(client)[old["ticket_no"]]["status"] == "applied"

    assert client.put("/api/apply/clock",
                      json={"business_clock": "2026-11-16T11:00:00Z"}).status_code == 200
    second = apply_due(client).json()
    assert second["applied"] is True
    assert second["ticket_count"] == 1
    assert second["line_count"] == 1
    assert second["written_skus"] == ["SKU-TEA-001"]

    # 旧单不再号称 applied（转 covered），新单是价签与历史的唯一来源。
    tickets = tickets_by_no(client)
    assert tickets[old["ticket_no"]]["status"] == "covered"
    assert tickets[new["ticket_no"]]["status"] == "applied"
    book = book_by_sku(client)["SKU-TEA-001"]
    assert book["current_price"] == 22.0
    assert book["source_ticket_no"] == new["ticket_no"]
    hist = client.get("/api/prices/history", params={"sku_code": "SKU-TEA-001"}).json()
    assert hist[0]["source_ticket_no"] == new["ticket_no"]
    assert hist[1]["source_ticket_no"] == old["ticket_no"]
    assert hist[1]["price"] == 25.0

    # 再应用既不会复活旧单，也不会重放新单。
    again = apply_due(client).json()
    assert again["applied"] is False
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 22.0
    dash = client.get("/api/dashboard").json()
    assert dash["applied_count"] == dash["book_source_count"] == 1
    assert state(client)["consistent"] is True


# ---------------------------------------------------------------- 场景 6
def test_legacy_parity_tears_closed_against_book(client):
    """老库遗留的奇偶旁路撕裂：没写价签的单回滚、写了价签的单回补，均由价签定夺。"""
    odd = make_ticket(client, "只写价签不写单", "2026-11-16T08:00:00Z",
                      [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])
    even = make_ticket(client, "只写单不写价签", "2026-11-16T08:30:00Z",
                       [{"sku_code": "SKU-SNK-011", "new_price": 10.0}])

    import app.db as db

    conn = db.connect()
    try:
        now = "2026-11-16T08:05:00Z"
        odd_id = conn.execute("SELECT id FROM price_tickets WHERE ticket_no=?",
                              (odd["ticket_no"],)).fetchone()["id"]
        even_id = conn.execute("SELECT id FROM price_tickets WHERE ticket_no=?",
                               (even["ticket_no"],)).fetchone()["id"]
        # odd 单：价签与历史已写成 25.0，但单仍 scheduled、无批号（老投影只写价签）。
        conn.execute(
            "UPDATE price_book SET current_price=25.0, source_ticket_no=?, effective_at=?, updated_at=? "
            "WHERE sku_code='SKU-TEA-001'",
            (odd["ticket_no"], "2026-11-16T08:00:00Z", now),
        )
        conn.execute(
            "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, recorded_at) "
            "VALUES ('SKU-TEA-001', 25.0, ?, '2026-11-16T08:00:00Z', ?)",
            (odd["ticket_no"], now),
        )
        # even 单：批次与单状态都 applied，价签与历史没有任何写入（老投影只改单状态）。
        conn.execute(
            "INSERT INTO apply_batches(batch_no, clock_at, ticket_count, line_count, "
            "skipped_count, strategy, status, created_at) "
            "VALUES ('AB-9100', '2026-11-16T08:30:00Z', 1, 1, 0, 'last_write_wins', 'done', ?)",
            (now,),
        )
        batch_id = conn.execute(
            "SELECT id FROM apply_batches WHERE batch_no='AB-9100'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO apply_batch_lines(batch_id, queue_pos, ticket_id, ticket_no, "
            "sku_code, new_price, status, covered_by_ticket_no, created_at) "
            "VALUES (?, 1, ?, ?, 'SKU-SNK-011', 10.0, 'applied', NULL, ?)",
            (batch_id, even_id, even["ticket_no"], now),
        )
        conn.execute(
            "UPDATE price_tickets SET status='applied', applied_at=?, apply_batch_id=? WHERE id=?",
            (now, batch_id, even_id),
        )
        conn.commit()
    finally:
        conn.close()

    before = state(client)
    assert before["consistent"] is False
    assert before["ghost_scheduled_tickets"] == [odd["ticket_no"]]
    assert before["false_applied_tickets"] == [even["ticket_no"]]

    r = apply_due(client).json()
    assert r["applied"] is False and r["reason"] == "divergence_reconciled"

    tickets = tickets_by_no(client)
    assert tickets[odd["ticket_no"]]["status"] == "applied"
    assert tickets[even["ticket_no"]]["status"] == "scheduled"
    assert tickets[even["ticket_no"]]["apply_batch_id"] is None
    # odd 单的既有价签不再被重写；受影响的批次被隔离，回执回填 0 写入。
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 25.0
    assert book_by_sku(client)["SKU-SNK-011"]["current_price"] == 12.5
    bad_batch = next(b for b in client.get("/api/apply/batches").json()
                     if b["batch_no"] == "AB-9100")
    assert bad_batch["status"] == "divergent"
    assert bad_batch["ticket_count"] == 0 and bad_batch["line_count"] == 0
    assert state(client)["consistent"] is True

    # 下一轮把 even 单真正落价签；回执与写入集合同源。
    applied = apply_due(client).json()
    assert applied["applied"] is True
    assert applied["written_skus"] == ["SKU-SNK-011"]
    assert book_by_sku(client)["SKU-SNK-011"]["current_price"] == 10.0
    assert state(client)["consistent"] is True


# ---------------------------------------------------------------- 场景 7
def test_late_old_ticket_cannot_regress_newer_applied_price(client):
    """早生效的旧单回滚后再到期：不得把更晚已生效的新价倒灌回价签本。"""
    old = make_ticket(client, "旧价", "2026-11-16T08:00:00Z",
                      [{"sku_code": "SKU-TEA-001", "new_price": 25.0}])
    new = make_ticket(client, "新价", "2026-11-16T10:00:00Z",
                      [{"sku_code": "SKU-TEA-001", "new_price": 22.0}])

    import app.db as db

    conn = db.connect()
    try:
        # 先让新价单生效（模拟新单已是价签本来源），旧单因分叉回滚为 scheduled。
        now = "2026-11-16T10:05:00Z"
        new_id = conn.execute("SELECT id FROM price_tickets WHERE ticket_no=?",
                              (new["ticket_no"],)).fetchone()["id"]
        old_id = conn.execute("SELECT id FROM price_tickets WHERE ticket_no=?",
                              (old["ticket_no"],)).fetchone()["id"]
        conn.execute(
            "INSERT INTO apply_batches(batch_no, clock_at, ticket_count, line_count, "
            "skipped_count, strategy, status, created_at) "
            "VALUES ('AB-9200', '2026-11-16T10:00:00Z', 1, 1, 0, 'last_write_wins', 'done', ?)",
            (now,),
        )
        batch_id = conn.execute(
            "SELECT id FROM apply_batches WHERE batch_no='AB-9200'").fetchone()["id"]
        conn.execute(
            "INSERT INTO apply_batch_lines(batch_id, queue_pos, ticket_id, ticket_no, "
            "sku_code, new_price, status, covered_by_ticket_no, created_at) "
            "VALUES (?, 1, ?, ?, 'SKU-TEA-001', 22.0, 'applied', NULL, ?)",
            (batch_id, new_id, new["ticket_no"], now),
        )
        conn.execute(
            "UPDATE price_book SET current_price=22.0, source_ticket_no=?, "
            "effective_at='2026-11-16T10:00:00Z', updated_at=? WHERE sku_code='SKU-TEA-001'",
            (new["ticket_no"], now),
        )
        conn.execute(
            "INSERT INTO price_history(sku_code, price, source_ticket_no, effective_at, "
            "recorded_at, batch_id) VALUES ('SKU-TEA-001', 22.0, ?, '2026-11-16T10:00:00Z', ?, ?)",
            (new["ticket_no"], now, batch_id),
        )
        conn.execute(
            "UPDATE price_tickets SET status='applied', applied_at=?, apply_batch_id=? WHERE id=?",
            (now, batch_id, new_id),
        )
        conn.execute(
            "UPDATE price_tickets SET status='scheduled', applied_at=NULL, apply_batch_id=NULL "
            "WHERE id=?",
            (old_id,),
        )
        conn.commit()
    finally:
        conn.close()

    assert state(client)["consistent"] is True

    # 业务钟推过旧单生效时刻：旧单到期，但价签已由更晚的单写成。
    assert client.put("/api/apply/clock",
                      json={"business_clock": "2026-11-16T11:00:00Z"}).status_code == 200
    r = apply_due(client).json()
    assert r["applied"] is True
    assert r["ticket_count"] == 0 and r["line_count"] == 0
    assert r["skipped_count"] == 1

    # 旧价不倒退：价签/历史仍指向新单；旧单终态 covered，不再每轮反复被扫。
    book = book_by_sku(client)["SKU-TEA-001"]
    assert book["current_price"] == 22.0
    assert book["source_ticket_no"] == new["ticket_no"]
    tickets = tickets_by_no(client)
    assert tickets[old["ticket_no"]]["status"] == "covered"
    assert tickets[new["ticket_no"]]["status"] == "applied"

    again = apply_due(client).json()
    assert again["applied"] is False
    assert book_by_sku(client)["SKU-TEA-001"]["current_price"] == 22.0
    assert client.get("/api/dashboard").json()["due_now"] == 0
    assert state(client)["consistent"] is True
