# 本机行程 API

数据库随 FastAPI 生命周期初始化，默认 `data/easytrip.sqlite3`。API 不依赖外部服务，当前消息状态为 `pending`，不会自动调用模型。消息保存只替换已配置的凭证，普通用户输入与链接原样保留。

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
