# 本机行程 API

数据库随 FastAPI 生命周期初始化，默认 `data/easytrip.sqlite3`。行程保存和本地准备任务不依赖外部服务；P2 `discover` 请求由单执行器调用小红书与模型。普通消息保存不自动调用模型；消息保存只替换已配置的凭证，普通用户输入与链接原样保留。

当前连续数据库迁移为 8（`008_manual_places.sql`），保留既有候选、地图、采用指针与方案快照。P3 使用独立 POI 覆盖和缓存；P4 已接通真实路线、单日排程、有限餐饮补查与后缀排程，并支持不经过攻略发现的用户指定候选。

| 方法与路径 | 请求 / 返回 |
|---|---|
| `POST /api/trips` | `operation_id`（UUID）、可选 `name` / `request`；返回已持久化行程 |
| `GET /api/trips` | `include_archived`、`limit`（1–100）、`offset`；返回 `items/total/limit/offset` |
| `GET /api/trips/{id}` | 条件、工作区、消息和最近任务 |
| `PATCH /api/trips/{id}` | `expected_version`、`name` 和/或完整 `request` |
| `PATCH /api/trips/{id}/workspace` | `expected_version`（工作区版本）、完整 `workspace` |
| `POST /api/trips/{id}/messages` | `operation_id`（UUID）、`content` |
| `POST /api/trips/{id}/archive` | `expected_version`、`archived`；可恢复归档 |
| `DELETE /api/trips/{id}?expected_version=…` | 版本匹配后级联删除，成功返回 204 |

创建与消息的相同操作 ID、相同内容重试返回原对象；同 ID 不同内容返回 409。条件和工作区分别校验 `request_version` / `workspace_version`；旧版本不会覆盖保存内容。归档行程拒绝编辑，可以先恢复。

错误在 `detail` 中提供 `code/message`：404 不存在、409 操作或版本冲突（附 `current_version`）、422 入参校验、503 存储暂不可用。503 时保留编辑后重试；不要假定已经保存。调用示例（在应用启动后）：

```powershell
$body = @{ operation_id = [guid]::NewGuid().ToString(); name = '周末出游' } | ConvertTo-Json
$trip = Invoke-RestMethod 'http://127.0.0.1:8000/api/trips' -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
Invoke-RestMethod "http://127.0.0.1:8000/api/trips/$($trip.id)"
```

服务绑定回环地址、限制本机 Host，拒绝不匹配的 Origin。前端开发反代将 Origin 改为后端自身地址，无需开放任意 CORS。

## 本地准备任务

| 方法与路径 | 请求 / 返回 |
|---|---|
| `POST /api/trips/{id}/runs` | `operation_id`（UUID）、`expected_version`、`kind: "prepare_workspace"`；返回 Run |
| `GET /api/runs/{id}?trip_id=…` | 必须指定所属行程；跨行程查询返回 404 |
| `POST /api/runs/{id}/cancel` | `trip_id`、当前 `generation`；取消后拒绝旧执行代次写入 |
| `POST /api/runs/{id}/resume` | `trip_id`、当前 `generation`；重新检查版本与剩余预算 |

启动请求可传 `budget: {max_steps: 3, max_seconds: 30}`；步数范围 1–3、时间范围 1–300 秒，P1 不开放任意任务、脚本或外部工具。三阶段为检查条件、读取工作区、汇总；输出在校验后与检查点一起原子提交，结果仅保存在 Run，不自动覆盖行程。

状态为 `pending/running/recoverable/completed/partial/failed/cancelled/stale`。重启前未结束任务只标为 `recoverable`，用户继续后从检查点执行；条件已改变或行程已归档时不能恢复。达到预算保留部分检查点。旧 `generation` 的控制请求返回 409，应读取最新状态后再操作。

`params/budget/checkpoint/usage/result` 均落库，`usage` 记录步骤、已知执行耗时，外部调用、模型 token 与图片数量在 P1 均为零。中断阶段无法精确核算的耗时明确标为未知。生产入口仅启动一个进程和执行器，不使用多 worker 或自动 reload。

## P2 发现与证据

| 方法与路径 | 请求 / 返回 |
|---|---|
| `POST /api/trips/{id}/runs` | `kind: "discover"`、UUID `operation_id`、`expected_version`、可选 `budget` / 同条件 `parent_run_id`；返回 Run |
| `POST /api/trips/{id}/messages` | 另传 `start_discovery: true`、`expected_version` 和可选 `budget`，消息与发现任务原子保存；返回消息及 `discovery_run_id/discovery_run` |
| `GET /api/trips/{id}/candidates` | 可选 `run_id/category/limit/offset`；返回 `items/total/limit/offset`，未指定轮次时只读当前条件版本 |
| `GET /api/trips/{id}/candidates/{candidate_id}/evidence` | 返回评价 `items` 与名称来源 `sources`；名称来源不等于正面体验 |
| `GET /api/trips/{id}/sources/{source_id}?run_id=…` | 指定轮次的原文快照、读取时间和图片原序号 |
| `GET /api/trips/{id}/sources?run_id=…` | 返回该轮来源 `items` |
| `GET /api/trips/{id}/gaps` | 可选 `run_id`；返回餐饮、住宿缺口 `items` |
| `POST /api/trips/{id}/directions/{candidate_id}/adopt` | UUID `operation_id/run_id`、`expected_version`；显式采用有来源城市方向并递增条件版本 |

发现预算默认 600 秒、8 次搜索（预留 2 次补查）、30 篇目标、每篇 20 条一级评论；二级回复关闭，开启后另限全轮 20 条。六阶段按顺序落库；计数区分新取得来源、沿用来源、评论选取/分析/判断、图片提交次数及原图去重覆盖。字段缺失表示历史记录未知，不能推导为零。候选为 `unmatched` 文字草案，`semantic_quality=not_validated` 表示尚无人类完成语义验收。

任务增加 `paused` 状态。取消、恢复仍携带当前 `generation`，条件已变或行程已归档时拒绝执行。恢复复用成功结果与剩余预算；同轮已有失败或未知模型请求时不会再次提交输入，新轮必须显式创建并有独立预算。未知用量保留 `reserved_tokens/unknown_cost_calls/unknown_result_calls`，实际 token 不转换为人民币。来源及调用内容不会覆盖条件，旧轮可指定 `run_id` 查看但不可采用旧方向。

## P3 地点、地图与出发点

| 方法与路径 | 请求 / 返回 |
|---|---|
| `GET /api/trips/{id}/places` | 可选 UUID `run_id`；返回 `run_id/request_version/candidates/matches/points/trip/read_at`，默认当前条件下最新有结果轮次 |
| `POST /api/trips/{id}/places/match` | `expected_version`、`expected_workspace_version`、可选 `run_id/candidate_ids/refresh_map`；核对当前候选，返回地点快照及 `budget` |
| `POST /api/trips/{id}/places/candidates/{candidate_id}/confirm` | 两个预期版本及 `poi_id`；只确认该候选已存备选，经真实详情验证后返回地点快照 |
| `POST /api/trips/{id}/places/map` | 两个预期版本、`action`；单项操作另需 `candidate_id`，整组操作可传 `category/city`；返回地点快照及保存后的行程 |
| `GET /api/trips/{id}/places/origin/search` | 必填 `query`（1–80 字）、`city`（1–200 字）、`expected_version`；返回 `search_id/request_version/request_origin/items`，只搜索、不确认 |
| `POST /api/trips/{id}/places/origin/confirm` | 两个预期版本及 `source`；`amap` 需 `search_id/poi_id`，`map` 需 `name/location`；返回保存后的行程 |

两个预期版本分别指 `request_version` 和 `workspace_version`；地图/起点选择只更新工作区，不修改原行程条件。地图动作是 `initialize/refresh/add/remove/select/unselect`：`add/remove` 操作地图集合，`select/unselect` 操作行程选择，`initialize` 仅首次建立点集，`refresh` 显式重新取 Top 8。地图点按真实 POI ID 聚合，点内保留原候选、来源以及含 `run_id` 的活动和证据引用；`points` 是可用 POI 集合，客户端必须另按 `trip.workspace.map_visible_ids` 和图层筛选绘制。

已经初始化地图或本行程已有 POI 匹配覆盖时，普通 `PATCH /workspace` 无条件保留数据库内的 `map_visible_ids/selected_ids`，忽略提交的旧集合；集合修改必须走 `/places/map`。普通保存保留起点与初始化状态并校验工作区版本，视野、类别和标签仍可编辑。尚未开始 P3 的 P1 工作区继续兼容历史集合。

`matches` 含 `candidate_id/run_id/request_version/status/poi/options/reason/confirmed/updated_at`。状态为 `unique/ambiguous/unmatched`；只有唯一匹配或显式确认且有真实 ID、名称、有效坐标的候选可加入地图或行程选择。泛称和探索方向不直接转成具体 POI。标准 POI 为 `id/name/location/address/city/adcode/type/entrance_location/read_at/api_version/missing_fields`；`location` 是 GCJ-02 `[经度,纬度]`，未取得字段为 `null` 并列入 `missing_fields`，入口缺失不复用中心点。

每次匹配最多 20 个候选，`candidate_ids` 显式指定时最多 20 个；默认每个名称至多两页、每页 20 条。操作共限 40 次物理请求和 60 秒，`budget` 返回 `candidate_limit/call_limit/timeout_seconds/calls/processed/limited`，缓存命中不记新请求。应用内高德请求共享 1.1 秒间隔，不自动重试 QPS 错误。超时或预算结束保留已完成匹配与原候选。缓存以规范化名称、城市、v5 版本为键，24 小时到期，保留原 `read_at`；高德 `count` 仅指本页数量，页数未穷尽不自动唯一匹配。

人工确认和高德起点确认额外请求所选 ID 的详情，限制一次物理请求、30 秒，核对唯一返回的 ID、有效坐标、名称及城市。起点搜索最多两次请求、30 秒。详情错误返回 502，未配置高德服务返回 503，空白城市/关键词等入参错误为 422，行程归属、旧条件、已归档或版本冲突拒绝操作；确认失败不替换原备选。异步核对结果提交时复查条件与匹配会话，工作区已有更新时保留新点集并返回 `map_update_skipped`。

`candidates` 保留原 `evidence_ids` 并增加 POI 状态、来源 ID 与 `ranking_reasons`。排序仅对已核城市/类别冲突作资格过滤，再用明示偏好短语的关联原文字面命中、不同作者、亲历正负评论、原发布时间解释次序；负面或混合命中显示冲突。无法确认当前价格时不以预算硬排除，不将点赞、样本数量或采样比例称为全平台好评率。地点确认不表示 P2 语义质量通过，也不代表路线与时间表已可执行。

## P4 规划输入、单日排程与方案调整

手动候选使用以下接口，不需要发现 Run、MCP 或模型配置：

| 方法与路径 | 契约 |
|---|---|
| `GET /api/trips/{id}/places/manual/search` | `query`（1–80 字）、`city`、`expected_version`；查询真实高德并保存本行程搜索快照，返回 `search_id/request_version/query/city/items` |
| `POST /api/trips/{id}/places/manual/confirm` | `operation_id` UUID、`expected_version/expected_workspace_version`、`search_id/poi_id/category`（`eat/play/stay`）；只能确认已保存搜索选项，重新核对详情标识、城市与坐标，返回地点快照及 `candidate_id` |

`GET /places` 的独立 `manual_candidates` 数组始终包含本行程用户指定候选，字段包括 `source="user"`、`run_id=null`、分类及已核对 POI。P2 的 `candidates`、来源与评价契约不变。确认只保存候选；复用 `/places/map` 的 `add/remove/select/unselect` 分别控制地图与行程。无发现轮次、修改时间或查看历史发现结果时，用户候选仍可读取。地图按 POI 合并落点，手动候选与攻略别名不重复创建 Marker。

确认校验行程归属、两个版本及归档状态，详情晚到后再次检查；同 UUID 重试返回当前保存状态并跳过重复详情请求，同 UUID 不同内容拒绝。沿用单后端进程的串行确认机制。用户候选独立存储，不伪造攻略、评论或推荐依据；进入规划时冻结 `source="user"` 及真实 POI，停留时间和费用保持未知，需由用户填写。改变选择会改变规划签名，旧方案采用必须重新校验。

| 方法与路径 | 契约 |
|---|---|
| `GET /api/trips/{id}/planning-input` | 返回 `trip_id/request_version/request/planning/origin/selected_ids/selected/missing/input_hash/active_plan_id`；冻结本行程已确认的 POI、活动与来源 |
| `GET /api/trips/{id}/planning-replacements` | 本行程已唯一确认且同城的备选，不因仅修改时间而要求重新采集；返回行程和两个版本、`candidates` |
| `POST /api/trips/{id}/planning-replace` | UUID `operation_id`、条件和工作区两个版本、`input_hash/candidate_id/replacement_id`；原子替换选择，保护完成/必去/锁定/预约，保留地图与当前采用指针；返回 `{trip,operation_applied}` |
| `GET /api/trips/{id}/plans` | 返回不可变版本 `items` 及 `active_plan_id`，最多最近 50 个 |
| `GET /api/trips/{id}/plans/{plan_id}` | 只读所属行程方案；跨行程返回 404 |
| `POST /api/trips/{id}/plans/{plan_id}/adopt` | UUID `operation_id`、`expected_version`（条件版本）、`input_hash`；校验当前条件、实质输入及有效期，返回 `{trip,plan}` |

`TripRequest.planning` 保存返程终点/期限、步行上限、禁用模式、额外缓冲和 `activity_constraints`。每个活动的时长/费用必须同时标记 `user/source/estimated/unknown` 来源；空值为未知。必去/锁定不得同时排除或跳过，时间窗和预约使用有时区时间。普通条件 PATCH 会递增请求版本并沿用已有 CAS。

只有服务端执行器可以写方案快照，客户端没有提交 `validated` 或伪造路线的接口。快照含冻结输入、时序、未知、冲突、用量、差异及有效期限。采用拒绝空、缺输入、草稿、部分、冲突或过期方案；条件、起点、选择变化后须重新校验。地图视野和来源全局重读元数据不影响输入签名。餐饮补查与后缀重排已接入服务端；最终真实流程与浏览器验收见 P4 记录。

`POST /runs` 的 `kind="plan"` 使用单执行器、冻结输入、代次检查与调用账本，预算至多 3 组组合、40 次高德请求。公交耗时直接使用接口总时长，程序缓冲单独显示；路线缓存按方向、交通、城市、日期和时段区分。无交通、无权限、费用或开放时间未知时保留问题，不生成虚构时长。方案生成不会自动采用。响应中的 `operation_applied=true` 可确认此前采用操作已保存；重试返回服务端当前方案指针，不能重新覆盖后来采用的方案。

`planning.progress_at/progress_origin` 必须成对，当前位置只能由用户明确提供地图坐标；保留原起点用于返程与整日期限。`dietary_restrictions` 不构成商家安全保证。完成、跳过和锁定从不可变当前方案检查；已发生历史与受影响后缀分开，只有未来路线要求当前有效，历史本身不可改写。未知历史移动不计算成确定步行或费用。旧版重新规划必须再次核对当前时间和进度。

跨午晚餐的实际窗口触发同轮只读补查：最多 2 搜索、2 笔记、5 条评论、4 模型请求、0 图片，所有来源/模型请求沿用原账本，高德地点与路线共用 40 次额度。补充候选保存于 `payload.supplements`，不偷偷修改原选择或签名；排入时间表前重新校验路线和窗口。失败或额度用尽保留原路线和餐次缺口。Agnes 仍使用直接连接、thinking 与任务输出预算；未知结果不自动重放。

## P5 多日、住宿与铁路

复用原行程修改、规划 Run、版本查询和显式采用接口，不新增订房/售票接口。

- `request.planning.day_windows` 为最多 15 个连续本地日期的 `{date,start_at,end_at}`，所有时刻带时区；与原 14 天时长上限一致，可涉及 15 个日历日期。活动约束可有 `date`，多日必须明确所属日。旧单日空默认不写入序列化或签名。
- `overnight_stays` 最多 14 晚，逐项 `{date,candidate_id}`；日期只可为非末日窗口，候选需为本行程已唯一核对的具体住宿。可提供用户确认的 `check_in_not_before/check_out_by/nightly_cost_cny`，分别属于住宿日、次日和同行全组该晚费用；未提供的值保持未知，历史笔记价格不自动转入。
- `rail_segments` 最多 28 段，稳定 `id/date`、`departure_station_id/arrival_station_id`、可空的 `train_number/departure_at/arrival_at`、`confirmed`、`source:user`、`boarding_buffer_minutes/arrival_buffer_minutes`、可空 `cost_cny`。确认前必须补齐两站、车次及完整带时区时刻；真实火车站 POI 与市内路线分别核对。没有可靠时刻只给部分条件草案。
- `GET /api/trips/{id}/planning-input` 另提供只用于编辑的 `lodging_candidates/station_candidates`；不把整个未使用候选池冻结到 Run。实际引用的 `lodgings/stations` 进入签名，保持来源与真实 POI。
- `payload.days` 保存当日起终点、窗口、住宿、活动、交通与问题，平坦 `itinerary` 仍保持逐项一致。`kind:rail` 使用用户确认时刻及两站资料，不伪装成高德 `RouteLeg`。跨午夜用 `overnight_rail_id/continuing_rail_id` 连接在途和到达日。
- 住宿补查候选带 `lodging_window`（`date/anchor_candidate_id/anchor_name/anchor_location/city`），与餐饮共用 Run 预算，来源/POI 保存后仍须用户明确逐晚绑定并重排。房态、实时价格、办理时长和行李寄存未知时保留待核实项。
- 重新规划按日比较语义输入，保留未受影响的日期和有效前缀；已完成/锁定和过去酒店/铁路不允许静默改写。执行中进度只接受当前采用父方案，历史交通计划的实际执行与费用没有确认时仍未知。采用继续核验版本、签名、有效期、逐日连续性、真实站点、时刻与缓冲。
