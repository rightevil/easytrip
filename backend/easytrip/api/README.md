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
