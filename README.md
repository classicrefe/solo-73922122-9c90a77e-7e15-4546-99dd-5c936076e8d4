# pg-subset-exporter

从持续写入的 PostgreSQL 源库中抽取**引用完整的数据子集**，生成可直接恢复到空库的 SQL 包。
用于数仓迁移 / 在隔离库复现故障的场景。

## 快速开始

```bash
docker compose up --build
# API 文档:  http://localhost:8000/docs
# 源库:      postgres://exporter:exporter@localhost:5432/source
```

一条命令导出（种子：users 表主键 1）：

```bash
curl -OJ -X POST http://localhost:8000/api/export \
  -H 'Content-Type: application/json' \
  -d '{"table": "users", "pks": [1]}'
```

端到端演示（循环引用、可空复合外键、自引用、并发写入、失败语义、空库恢复校验）：

```bash
./scripts/demo.sh
```

## 组成

| 服务   | 说明 |
|--------|------|
| `db`     | PostgreSQL 16 示例源库（`db/init.sql`：6 张表，含循环引用 users↔teams、自引用 tasks、可空复合外键 attachments→task_links） |
| `app`    | FastAPI 导出服务（`/docs`、`/api/tables`、`/api/export`、`/api/compatibility/check`、`/healthz`） |
| `writer` | 持续写入器：每 0.1–0.3s 提交一笔 INSERT/UPDATE，用于演示导出期间源端并发改写不被阻断 |

## 导出语义

- **闭包方向**：从种子行出发，沿外键 *子→父* 方向递归补齐所有被引用行；仅引用种子行的下游行属于无关行，不复制。
- **可空外键**：遵循 MATCH SIMPLE——外键任一分量为 NULL 即视为无需补齐，NULL 原样保留。
- **一致性**：结构元数据与全部数据在同一个 `REPEATABLE READ READ ONLY` 事务中读取，
  即同一个已提交快照；MVCC 保证导出不阻断源端写入（SQL 包头含隔离级别与 `pg_current_snapshot()` 佐证）。
- **失败语义**（整次失败，不产出部分包；包在内存中完整构建后才返回）：
  - 闭包超过 1000 行 → `422`
  - 引用无法补齐（悬挂引用）→ `422`
  - 种子行不存在 / 种子表未知 → `404`
  - 源 schema 超出受管子集（>6 张普通表、非 int/text 列、列默认值、复合或非整数主键等）→ `422`
  - 读取失败（连接、权限等）→ `500`
- **SQL 包**：`CREATE TABLE`（主键 + 唯一约束）→ `INSERT`（原键、NULL 原样保留）→ 最后
  `ALTER TABLE … ADD FOREIGN KEY`（建表时不带外键，因此循环/自引用可直接恢复；添加外键时
  PostgreSQL 会校验全部数据，闭合完整即全部有效）。整包包裹在 `BEGIN/COMMIT` 中，恢复是原子的。

## 源库约束（受管子集）

- `public` schema 下至多 6 张普通表（非分区、非视图）；
- 列类型仅整数（`smallint`/`integer`/`bigint`）与 `text`，无默认值、非生成列、非 identity；
- 约束仅主键（单列整数）、唯一约束、外键（支持可空、复合、自引用、循环引用）。

超出子集时导出整体失败并说明原因（见 `GET /api/tables` 与导出错误信息）。

## API

- `GET /api/tables` — 查看可导出的表、列、主键、唯一与外键约束。
- `POST /api/export` — 请求体 `{"table": "<种子表>", "pks": [<主键>...]}`；
  成功返回 `application/sql` 附件下载；失败返回 JSON 错误且不产出任何 SQL。
- `POST /api/compatibility/check` — 目标库兼容性检查（见下节）。
- `GET /docs` — Swagger UI 交互文档。

## 目标库兼容性检查

迁移工程师在导出关联子集前，可先确认目标库能否承接当前 `public` schema：

```bash
# 目标 schema 与 GET /api/tables 响应同构——可直接取其输出改改后提交
curl -s http://localhost:8000/api/tables > target.json
# ... 按目标库实际情况编辑 target.json ...
curl -s -X POST http://localhost:8000/api/compatibility/check \
  -H 'Content-Type: application/json' --data-binary @target.json | jq
```

服务在与导出相同的 `REPEATABLE READ READ ONLY` 快照事务中读取源结构，
逐项比较表、列类型/可空性、主键、唯一约束和外键，返回
`compatible`（无 blocking 即为 true）、`summary` 与 `issues`
（每条含 `severity`/`category`/`reason`/`dependency_paths`）：

- **blocking**（目标无法承接）：缺表、缺列、列类型缩窄或不兼容
  （`bigint→integer→smallint` 缩窄、`text→integer`、参与外键的列
  `integer→text`，PostgreSQL 无法跨 integer/text 建外键）、可空性缩窄
  （源可空而目标 `NOT NULL`）、主键缺失或不一致、被外键引用的唯一约束缺失。
  影响沿源库外键依赖图传播，每条 blocking 附带 `dependency_paths`，如
  `users(team_id) -> teams(id) [users_team_fk]`。
- **warning**（数据可装载但保障减弱或可能拒数）：未被外键引用的唯一约束缺失、
  外键缺失、普通列 `integer→text`、目标额外定义的 NOT NULL 列/唯一约束/外键。

目标 JSON 格式错误（缺字段、未知列类型、重复表/列、主键或唯一约束引用
本表不存在的列、外键列数不匹配等）返回 `422`；目标文档中引用缺失的
表/列不算格式错误——那正是比较要报告的缺失项。

## 本地开发（不用 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt
cd app && python -m unittest test_exporter test_compatibility test_api -v   # 单元 + API 测试（无需数据库）
DATABASE_URL=postgresql://exporter:exporter@localhost:5432/source \
  uvicorn main:app --port 8000
```

## 设计说明与限制

- 闭包上限 1000 行、表数上限 6，均为导出前/导出中的硬性失败条件。
- 受管子集之外的 schema 特性（CHECK 约束、触发器、视图、序列默认值等）不在导出范围内；
  示例库中的 `writer_seq` 序列仅供写入器显式 `nextval()` 使用，不属于表结构。
- 外键的 `ON UPDATE/ON DELETE` 动作、`DEFERRABLE` 属性、`UNIQUE NULLS NOT DISTINCT`（PG15+）
  会按源库定义保留在 SQL 包中。
