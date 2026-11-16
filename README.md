# 06-pricecab（价签柜）

Pricecab — 改价单按预约时刻生效，现行价签可查，并与销售夹具对账。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）、Proofpack（采样放行）主链路不同。  
不是电商订单/购物车，不是仓库进出库 CRUD，不是考勤 OA。夹具全本地。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5600 |
| API | http://localhost:10600 |

## 主链（0-1）

选商品建改价单（写明生效时刻）→ 拨业务钟或点「应用到期」→ 现行价签更新 → 销售对账看一致/不一致

## 应用到期的批次语义

- **队序**：到期单先按生效时刻、再按单号排序，逐票逐行展开成应用队列。
- **同商品冲突策略（全局唯一，不可切换）**：后写覆盖（`last_write_wins`）——同一商品只保留队序最后一张单的新价写入价签本与价格历史；前列同商品行记为 `skipped_covered`（被覆盖跳过），批次仍成功。整单被覆盖的改价单记终态 `covered`，不再参与后续应用。
- **可核证**：批次行（`apply_batches`）带 `ticket_count / line_count / skipped_count / strategy`；每行明细落 `apply_batch_lines`（来源单号、行状态、覆盖者单号），经 `GET /api/apply/batches/{id}` 可查；响应、批次行、价签本现行价与来源单、价格历史末行同源于同一份队列计划，单事务写出。
- **回执不撒谎**：`ticket_count / line_count` 由真实写入集合回填（写入票数 = 至少一行落到价签的单数，写入行 = 落到价签的明细行数），而非队列展开数；响应直接读取回填后的批次行。
- **互斥**：应用进行中，第二次「应用到期」与「拨业务钟」均被拒绝（409）；批次异常整体回滚，不留半截价签。

## 分叉收口（全局唯一口径：以价签本与价格历史为准）

应用入口每次先收口、再扫到期，收口与应用互斥（发现分叉的这一轮只修复、不写新价）：

- **唯一凭证**：一次成功写入 = 价签本改现值 + 一条价格历史（含 `batch_id`），同事务、同来源单、同价。价格历史是价签本的审计链。
- **单状态回补/回滚**：`applied` 当且仅当该单某行仍是价签本现行来源、且历史末行同样指向该单该价 → 回补/保持 `applied`；曾写入后被更晚写入顶掉 → `covered`；毫无写入痕迹却声称 applied → 回滚 `scheduled` 并清批号，可再次真正应用。
- **批次成功标记**：`applied` 明细必须能由本批次写入的价格历史证明；证明不了的明细降级 `divergent`，整批次记 `divergent` 并按真实写入回填计数。分叉批次永不再当生效凭证，但作为已隔离历史残迹保留在报告中。
- **幽灵价签**：价签来源单不存在或其价格对不上明细时，按价格历史链回滚价签本，并撤销锚点之后未落到价签的旁路历史行；无法自洽的分叉会拒绝应用（409）。
- **到期扫描**：带 `apply_batch_id` 的单不会再被扫入队列，因此「价签已新、单仍 scheduled」的幽灵单一旦被收口就不会被二次应用。

只读互证视图：`GET /api/apply/state`（同时内嵌在 `GET /api/dashboard` 的 `apply_state` 中），对照 `applied_ticket_count`（可溯源票数）、`book_source_ticket_count`、`claimed_applied_count`、`divergent_batch_count` 等是否同源。

## 测试

```bash
cd backend && pip install -r requirements.txt
python -m pytest tests/
```

覆盖：无冲突成功、同商品冲突（后写覆盖 + 同刻按单号决胜 + 整单被覆盖）、应用中改钟/二次应用被拒、中途失败整体回滚。分叉夹具（`tests/test_divergence.py`）：回执成功但价签旧、价签新但单仍 scheduled 可再扫、票数 ≠ 写入集合、分叉批次不再当生效凭证、新单顶掉旧单、老库奇偶旁路遗留撕裂——均对照批次、批明细、单状态、价签本、历史与总览互证。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。
