# 微孔板样本谱系追踪 API

基于 **Python · FastAPI · SQLite** 的微孔板谱系台账服务：

- 登记孔位中的初始样本与体积；
- **可选孔位容量上限**：每个孔位可单独设置最大容量，未设置（含全部既有孔位）默认不限；可随时收紧、放宽或以 `null` 取消。设置/取消经 API 完成，新上限低于当前体积时返回 `409` 且不改配置；
- 一批移液操作**按提交顺序在单个事务中原子执行**：每次吸取按来源孔的组成占比带走各初始样本，目标孔按现有液体与新增液体重新混合；**任一步体积不足或使目标孔超过当前容量，整批回滚**（同一批内允许先移出再移入，每一步按当时目标体积判定）；
- 按孔查询当前体积、各初始样本占比，以及涉及该孔的完整批次记录；
- **误操作批次撤销**：成功提交的批次在同一事务保存其首次触及各孔的批前快照，可原子恢复；仅当批次已提交、尚未撤销且之后没有补液或后续批次触及这些孔时允许撤销，原批次保留并追加审计记录；撤销前还会按各孔**当前容量**核对快照恢复后的体积，任一孔超限即拒绝整次撤销，液体与审计记录不变；
- 所有体积运算使用 `Decimal`（统一量化到小数点后 9 位），不使用 float；
- 通过 `request_key` 幂等去重：相同键重复提交不会重复执行，直接返回首个结果（失败批次同样被记录并消耗该键）；批次撤销同样以 `request_key` 幂等：同键同目标返回首次结果，同键换目标返回 `409`。

## 快速启动（Docker，唯一需要的命令）

```bash
docker compose up --build
```

启动后在本机访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/health | 健康检查 |
| http://localhost:8000/docs | Swagger 交互式文档（可直接试调） |
| http://localhost:8000/redoc | ReDoc 文档 |

SQLite 数据落在命名卷 `lineage-data` 中，容器重启不丢失；`docker compose down -v` 可清空。

## 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000   # 同样监听本机 8000 端口
pytest                                       # 运行测试
```

数据库文件默认在 `./data/lineage.db`，可用环境变量 `LINEAGE_DB` 指定其他路径。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/wells` | 登记孔位初始样本与体积（同孔重复登记视为补液混匀，受目标孔当前容量约束） |
| `PUT` | `/wells/{well}/capacity` | 设置容量上限（`{"capacity": "100"}`）或取消（`{"capacity": null}`，恢复不限） |
| `GET` | `/wells/{well}` | 孔位当前体积、容量上限（`null` 表示不限）+ 各初始样本的量与占比 |
| `GET` | `/wells/{well}/batches` | 涉及该孔的完整批次记录 |
| `POST` | `/batches` | 原子执行一批移液（`request_key` 幂等；任一步使目标孔超容量即整批回滚） |
| `GET` | `/batches` / `/batches/{id}` | 全部 / 单个批次记录（含失败批次） |
| `POST` | `/batches/{id}/reversals` | 撤销已提交批次：原子恢复批前快照（`request_key` 幂等；恢复后超当前容量则拒绝） |

孔位标签为「字母 + 数字」（如 `A1`、`B12`），不区分大小写；本服务按单块板建模。容量只能为已登记孔位设置（未登记返回 `404`）；容量与体积均为十进制字符串，容量必须大于 0。

## 调用示例

```bash
# 1. 登记初始样本：A1 加入 100 µL 样本 S1，A2 加入 60 µL 样本 S2
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A1", "sample": "S1", "volume": "100"}'
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "A2", "sample": "S2", "volume": "60"}'

# 2. 原子批量移液：A1 -> B1 40，A2 -> B1 30（B1 混合为 S1:S2 = 4:3）
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-001",
        "operations": [
          {"source": "A1", "destination": "B1", "volume": "40"},
          {"source": "A2", "destination": "B1", "volume": "30"}
        ]
      }'

# 3. 查询 B1 的体积与谱系占比
curl -s http://localhost:8000/wells/B1
# => {"well":"B1","volume":"70","capacity":null,"composition":[
#      {"sample":"S1","amount":"40","fraction":"0.571428571"},
#      {"sample":"S2","amount":"30","fraction":"0.428571429"}]}

# 4. 体积不足 -> 整批回滚，返回 409，状态不变
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-002",
        "operations": [
          {"source": "B1", "destination": "C1", "volume": "50"},
          {"source": "B1", "destination": "D1", "volume": "50"}
        ]
      }'
# => 409 {"detail":{"message":"孔位 B1 体积不足：可用 20，请求吸取 50", ...}}

# 5. 重复提交同一 request_key -> 200 且 replayed=true，不会重复执行
# 6. 查询涉及 B1 的全部批次
curl -s http://localhost:8000/wells/B1/batches

# 7. 撤销误操作批次（假设批次 id 为 1）：原子恢复批前快照，原批次保留
curl -s -X POST http://localhost:8000/batches/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "undo-run-2026-09-22-001"}'
# 之后再有补液或后续批次触及该批孔位 -> 409 且 blocking_wells 列出后续操作

# 8. 容量上限：为 B1 设置 100 µL 上限（GET /wells/B1 的 capacity 字段随之返回 "100"）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"capacity": "100"}'
# 取消上限（恢复不限容）：
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"capacity": null}'
```

### 容量冲突（409）示例

```bash
# 8a. 新上限低于当前体积 -> 409，容量配置不变（capacity 仍为旧值）
curl -s -X PUT http://localhost:8000/wells/B1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"capacity": "50"}'
# => 409 {"detail":{
#      "message":"孔位 B1 新上限 50 低于当前体积 70，容量未变更",
#      "well":"B1","current":"70","capacity":"50"}}

# 8b. 补液超过上限 -> 409，液体不变
curl -s -X POST http://localhost:8000/wells \
  -H 'Content-Type: application/json' \
  -d '{"well": "B1", "sample": "S3", "volume": "40"}'
# => 409 {"detail":{
#      "message":"孔位 B1 超过容量上限：当前体积 70，拟加入 40，上限 100",
#      "well":"B1","current":"70","incoming":"40","capacity":"100"}}

# 8c. 批次任一步使目标孔超容量 -> 整批回滚 409，返回超限孔位、当时体积、
#     拟加入量和上限；失败批次同样落库并消耗 request_key
curl -s -X POST http://localhost:8000/batches \
  -H 'Content-Type: application/json' \
  -d '{
        "request_key": "run-2026-09-22-003",
        "operations": [
          {"source": "B1", "destination": "C1", "volume": "20"},
          {"source": "A1", "destination": "B1", "volume": "40"}
        ]
      }'
# B1 先移出 20（70 -> 50），再移入 40（50 -> 90，未超 100）=> 201
# 若第二步移入 60（50 + 60 > 100）=> 409：
# => {"detail":{"message":"孔位 B1 超过容量上限：当前体积 50，拟加入 60，上限 100",
#      "batch_id":3,"well":"B1","current":"50","incoming":"60","capacity":"100"}}

# 8d. 撤销会把孔位恢复到批前体积；若恢复值超过该孔当前上限 -> 拒绝整次撤销，
#     液体和审计记录都不变
curl -s -X POST http://localhost:8000/batches/1/reversals \
  -H 'Content-Type: application/json' \
  -d '{"request_key": "undo-again"}'
# => 409 {"detail":{
#      "reason":"capacity_exceeded","batch_id":1,
#      "over_capacity_wells":[
#        {"well":"A1","restored_volume":"100","capacity":"80"}]}}
```

## 设计要点

- **Decimal 精度**：体积与组分量在库中以 TEXT 存 Decimal 字符串，运算全部走 `decimal.Decimal`，结果统一量化到 9 位小数（`ROUND_HALF_UP`），接口返回值中的数字均为字符串以避免 JSON 精度损失。
- **按比例吸取（整数单位最大余量法）**：从孔中吸取体积 `v`（孔总量 `V`，均以 9 位小数的最小计量单位 QUANTUM 计）时，每个初始样本先分得比例份额 `amount × v/V` 的整数单位，剩余单位按小数余量从大到小分配；余量相同按样本 ID 升序。任何样本分得的单位数不超过其原有量，且各样本带走量之和恰好等于 `v`——保证每一步之后来源孔与目标孔都满足「组分量之和 = 体积」；目标孔将各组分量累加，即视为立即完美混合。
- **原子性**：一个批次的所有操作在单个 SQLite 事务中按序执行，任一步 `InsufficientVolume`（来源不足）或 `CapacityExceeded`（目标超容）即回滚；失败批次以 `status="failed"` 落库（操作明细随回滚清空），方便审计。
- **孔位容量（可选，默认不限）**：`wells.capacity` 以 TEXT 存 Decimal 字符串，`NULL` 表示不限；服务启动时对旧库幂等 `ALTER TABLE` 加列，既有孔位一律为 `NULL`，不改变现有不限容数据。容量经 `PUT /wells/{well}/capacity` 设置/取消：新上限低于该孔当前体积时返回 `409` 且不写库；补液在写入前、批次在每一步实际写入前按**当时**目标体积与当时上限校验（同批先移出腾出的空间立即生效；恰好等于上限合法），超限 409 明细含 `well` / `current`（当时体积）/ `incoming`（拟加入量）/ `capacity`（上限）；容量变更不写入 `well_events`，不构成撤销阻塞，撤销中被整体删除的孔其容量配置随之删除。
- **并发串行（`BEGIN IMMEDIATE`）**：补液、批次提交、撤销、容量变更四类变更端点从事务起点（任何校验读之前）即取 SQLite 保留写锁，使「校验 → 写入 → 提交」全程互斥：后到的变更在取锁处等待，先到者提交后才读库，因此必然看到已提交的最新状态——**先提交的容量决定后续补液/批次/撤销校验看到的上限**。这消除了撤销的丢失更新——撤销完成阻塞检查后、恢复快照前若有补液先提交，撤销在锁内重检到该补液事件并返回 `409 blocked`（列出阻塞孔位与补液操作），不会以旧快照覆盖；反之撤销先提交时，补液在锁上等待，随后叠加在恢复后的状态上（含撤销中被删除、随后补液重建的孔）。两种顺序均不丢失组分。
- **幂等**：`batches.request_key` 有唯一约束。重复提交返回首个批次记录（`replayed=true`，HTTP 200）；并发下同键请求先在锁内复核、再由唯一约束兜底，不会执行两次。
- **批次撤销**：每个成功提交的批次在**同一事务**内对其首次触及的每个孔（来源与目标去重）保存批前快照——孔体积与各初始样本量，孔不存在则记 `NULL`。撤销时（`POST /batches/{id}/reversals`，请求体携带独立的 `request_key`）：
  - 仅当批次 `committed`、尚未撤销、存在快照，该批之后没有补液或已提交批次触及任一快照孔，**且快照恢复后各孔体积均不超过其当前容量**时才允许；条件不符返回 `409`，`reason` 取 `not_committed` / `already_reversed` / `no_snapshot` / `blocked` / `capacity_exceeded` / `request_key_conflict`；`blocked` 时在 `blocking_wells` 中逐孔列出后续操作（`top_up` 含样本与体积，`batch` 含后续批次 id 与 request_key），`capacity_exceeded` 时在 `over_capacity_wells` 中逐孔列出恢复体积与当前上限（`well` / `restored_volume` / `capacity`），**液体状态与审计记录保持不变**；
  - 撤销在单事务内把所有快照孔恢复到批前状态（批次中新建的孔整体删除，先删组成再删孔以满足外键），并向 `reversals` 追加一条关联原批次的审计记录；**原批次与其操作明细不删除**，批次记录新增 `reversed` / `reversed_at` 字段；
  - 撤销 `request_key` 幂等：同键同目标返回首次撤销结果（`replayed=true`，HTTP 200），同键换目标返回 `409`；
  - 上线前已提交、没有快照的批次明确返回 `409 no_snapshot`，不可撤销。
  - 「后续操作」的判定基于孔位事件表 `well_events`（补液与批次提交各记一条，自增 id 即发生顺序）：以**本批次在该孔上的事件 id** 为基准，只统计之后的事件。
- **状态码**：登记/批次成功/撤销成功 `201`；幂等重放、容量设置/取消成功 `200`；体积不足、超容量、容量设置冲突或撤销冲突 `409`；孔位/批次不存在 `404`；参数非法 `422`。

## 项目结构

```
app/
  main.py     # FastAPI 路由
  ledger.py   # 谱系台账核心逻辑（Decimal 运算、事务内批量执行）
  db.py       # SQLite 连接与建表
  schemas.py  # 请求体验证
tests/        # pytest 端到端测试
Dockerfile / compose.yaml
```
