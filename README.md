# 氨氮批次质控与发布 API

环境实验室数据员录入同批氨氮测量，水质分析核对空白与加标空白质控后发布逐样品浓度。
FastAPI + SQLite（标准库 `sqlite3`，无 ORM），可一键以 Docker Compose 启动，自带 Swagger。

## 启动

```bash
docker compose up --build -d
```

* Swagger UI: <http://localhost:8000/docs>
* ReDoc: <http://localhost:8000/redoc>
* OpenAPI JSON: <http://localhost:8000/openapi.json>
* 健康检查: <http://localhost:8000/healthz>

数据持久化在命名卷 `ammonia-data`（容器内 `/data/ammonia.db`），`docker compose down`
不会删除；要清空可 `docker compose down -v`。

不使用 Docker 时：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --port 8000
```

## 接口一览

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/batches` | 录入批次：1～100 个样品 + 可选空白、加标空白 |
| GET | `/batches` | 批次列表及当前质控/发布状态 |
| GET | `/batches/{id}` | 批次当前修订、质控结论、当前报告 |
| GET | `/batches/{id}/qc` | 按当前修订计算质控（只读） |
| GET | `/batches/{id}/measurements` | 全部测量修订历史 |
| POST | `/batches/{id}/measurements` | 新增测量或更正（幂等 + 拒收规则） |
| POST | `/batches/{id}/publications` | 发布合格批次 |
| GET | `/batches/{id}/publications` | 发布历史（current + expired） |
| GET | `/publications/{id}` | 按 ID 查报告，过期报告仍可查询 |

## 业务规则

**录入**

* 每批样品 1～100 个；另有 `blank`（空白）与 `spike`（加标空白，已知加入 10 mg/L）。
* `value_mg_l` 必须是非负有限数值。`measurement_no` 批内唯一，`revision` 为递增修订号。
* 每个测量点的首次记录必须从 `revision = 1` 开始（创建批次时同样要求），之后每次更正必须 `+1`
  （跳号返回 `REVISION_GAP`）。

**修订 / 幂等（`POST /batches/{id}/measurements`）**

| 情况 | 结果 |
| --- | --- |
| 同测量号、同修订、同读数 | `200`，`duplicate=true`，不新增记录，不影响当前报告 |
| 同测量号、同修订、异读数 | `409 REVISION_VALUE_CONFLICT` |
| 测量号已存在、修订低于当前 | `409 REVISION_STALE`（旧版拒收） |
| 修订跳号 / 首版非 1 | `409 REVISION_GAP` |
| 测量号改挂到另一测量点 | `409 MEASUREMENT_NO_CONFLICT` |
| 同一测量点换用新测量号 | `409 POINT_ALREADY_RECORDED` |
| 有效的新增/更正 | `201`，并立即过期当前报告 |

**质控（发布前计算）**

* 空白 `> 0.2 mg/L` → 不合格（恰好 `0.2` 合格；用 `Decimal` 精确比较边界）。
* 扣空白后加标回收率 `= (加标读数 − 空白) / 10 × 100%`，须在 **80%～120% 闭区间**内。
* 缺空白/缺加标 → 质控不完整，不能发布。

**发布（`POST /batches/{id}/publications`）**

* 质控不完整 → `409 QC_NOT_COMPLETE`；不合格 → `422 QC_FAILED`，响应体带完整质控明细。
* 合格 → `201`，逐样品写入 `concentration_mg_l = 样品读数 − 空白`，并记录所依据的
  `measurement_no` 与 `source_revision`。
* 样品读数 **低于** 空白（扣空白为负）→ `concentration_mg_l = null`、`reason = "SAMPLE_BELOW_BLANK"`，
  `note` 说明原因。恰好等于空白 → 浓度为 `0.0`，正常报告。
* 当前修订集合已发布且一致 → `200 already_published=true`，不重复建报告。

**过期与历史**

* 任何有效的新增/更正（含空白、加标）在**同一事务**内把当前报告置为 `expired`；
  随后重新发布生成新报告。
* 历史报告内容不可变，`GET /publications/{id}` 随时可查，`status=expired` 且带 `expired_at`。

## 一致性设计

* 每个写请求使用 `BEGIN IMMEDIATE`（SQLite 写锁），因此**发布**读取的修订集合与**更正**提交
  的修订集合不会交错：更正先提交 → 报告引用新修订；发布先提交 → 更正立即使其过期。
* 报告头与全部明细在**同一个事务**内写入，失败整体回滚，不会出现半份结果。
* 失效的当前报告由独立事务先行过期，即使随后因质控失败无法重新发布，旧报告也不会“复活”。
* 读到不一致的 `current` 报告时（防御分支）会重试，最多 `MAX_PUBLISH_ATTEMPTS` 次。
* 读请求运行在延迟事务中，固定一个 WAL 快照，报告头与明细不会跨提交混合。
* 数据库层另有约束兜底：每批最多一条 `current` 报告（部分唯一索引）、浓度与原因二选一、
  非负读数、测量号+修订唯一。

## 测试

```bash
.venv/bin/python -m pytest -q
```

覆盖：空白/回收率边界、扣空白、低于空白留空、修订幂等与拒收、过期与历史不可变、
写入失败回滚、质控失败后旧报告不复活、并发更正与发布不引用旧修订、单报告完整性。

## 限制

* 无鉴权、无限流，适合内网/LIMS 前的服务；生产部署请置于网关之后。
* SQLite 单写者模型适合单实例；横向扩展需换成 Postgres（事务语义可平移）。
* `measurement_no` 只要求批内唯一，未做跨批全局唯一约束。
