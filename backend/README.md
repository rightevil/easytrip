# EasyTrip 后端

提供 SQLite 行程持久化、[业务 API](easytrip/api/README.md)与 P0 真实服务验证。React 工作区需先在 `frontend/` 构建，正式攻略理解与自动排程留待后续阶段。

## Windows 使用

Python 与依赖均由 uv 管理，环境位于 `backend/.venv`，锁定版本位于 `backend/uv.lock`。需已安装 uv；无需全局 pip 安装。根目录 `.env` 会自动读取，`backend/.env` 如存在则覆盖同名项，环境变量优先级最高。

从仓库根目录执行：

```powershell
uv --directory backend sync --locked
uv --directory backend run python -m easytrip.probe
uv --directory backend run uvicorn easytrip.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

打开 http://127.0.0.1:8000 查看草稿工作区，http://127.0.0.1:8000/p0/map 查看独立地图验证。如果 uv 不在 PATH，可以使用 `scripts/probes/p0.ps1`，它会查找用户目录 `.local/bin/uv.exe`：

```powershell
./scripts/probes/p0.ps1 probe
./scripts/probes/p0.ps1 serve
./scripts/probes/p0.ps1 test
```

已有真实笔记检查点时，用 `uv --directory backend run python -m easytrip.probe --reuse` 仅复用详情，再验证模型和高德；脚本支持 `./scripts/probes/p0.ps1 probe -Reuse`。重复执行会产生新的模型和地图服务调用，按账号实际计费。

P1 草稿应用的统一 Windows 入口为根目录 `scripts/dev.ps1`（setup/build/serve/test）；使用方法与验收证据见 [P1 验收记录](../docs/p1-acceptance.md)。SQLite 默认保存于根目录 `data/easytrip.sqlite3`。启动检测未完成任务并标为可恢复，用户确认继续后再执行；本地“准备工作区”任务不调用外部服务。仅运行一个后端进程，不使用多 worker 或自动 reload。

## 配置

参照 [.env.example](../.env.example)，真实密钥只放被 Git 忽略的 `.env`。

- 小红书：`XHS_MCP`，完整 HTTP MCP URL；服务由用户维护，登录/验证码由用户处理，本应用不启动 MCP 子进程。
- 高德：`AMAP_WEB_KEY`、`AMAP_JS_KEY`、`AMAP_JS_SECURE_KEY`。远程官方 MCP 默认复用 Web Key，可用 `AMAP_MCP_URL` 单独配置。
- Agnes：支持现有 `AGNES_URL`、`AGNES_KEY`、`AGNES_MODEL`，大小写兼容（包括 `AGNES_MODEl`）；也支持通用 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_ID`。URL 为兼容 chat/completions 的 base URL 或完整端点。

Agnes 调用通过 `trust_env=False` 忽略 Windows 系统代理和环境代理，不修改全局代理，也不在直连失败后切换到代理。按 [Agnes 3.0 Flash 官方文档](https://wiki.agnes-ai.com/en/docs/agnes-30-flash)，每次请求（包括结构修复）都发送 `chat_template_kwargs.enable_thinking=true`。当前任务的 `max_tokens` 上限为：

| 任务 | 输出 token 上限 |
|---|---:|
| 文本抽取 | 16,384 |
| 图文抽取 | 32,768 |
| 结构/证据修复 | 32,768 |
| P2 需求理解 | 16,384 |
| P2 评论证据（文本或图文） | 32,768 |

上限在 `easytrip/integrations/model.py` 的 `TOKEN_BUDGETS` 中统一定义，不代表每次都会消耗满额。读取响应最长等待 300 秒；结构校验失败只修复一次，修复不重复提交图片。网络/HTTP 错误停止调用，报告保留先前已返回用量并将缺失用量标为未知。`requests` 记录任务、预算、结束原因和是否返回 reasoning 字段，不保存思考正文；服务未返回该字段时，不能据此证明服务端实际推理过程。

P2 `discover` 任务与 P1 本地准备任务分开，冻结当轮条件、工作区和最近最多 16,000 字的用户对话。保存消息并开始发现为同一事务；模型推测不会修改已保存目的地，采用有来源方向是显式操作并递增条件版本。外部请求发出前写入私有调用台账，成功的校验结果在重启恢复时复用；失败或结果未知的模型调用要求显式新开一轮，避免在同轮恢复时重复发送图片。调用句柄和思考正文不会出现在业务 API。

每轮预算默认 8 次搜索（预留 2 次补查）、30 篇目标、每篇 20 条一级评论、600 秒，二级回复默认关闭；开启回复时另受本轮 20 条总额限制。模型额度检查使用已知累计 token 加未结清的输出 token 预留；服务无法预报输入/图片 token，因此这不是实际总 token 的绝对上限，实际用量超额后停止后续调用。未知用量保持预留和未知费用，恢复不重置计数；显式新轮有新预算，行程累计用量仍包含所有旧轮。

高德 JS 安全密钥仅由本机后端 `_AMapService` 代理追加，不出现在页面配置。JS Key 按 SDK 要求传给浏览器，生产使用前仍需配置合法域名。服务只绑定回环地址，拒绝非本机 Host 和跨来源请求。

## 探测范围

默认明确目的地查询为“杭州一日游 攻略”，探索查询为“周末 安静 山水 高铁 旅行”；属于人工接口测试条件，不代表用户实际出行需求。可用 `--query`、`--explore`、`--city` 替换。全轮最多读取 5 篇不同笔记，最多选取每篇 10 条一级评论，关闭二级回复；最多提交 3 张可信 CDN 图片，视频未分析。工具白名单仅包含登录状态、搜索、详情。

`--poi 名称` 可重复至 3 个，用于人工选择样本中的同城线索。地点歧义仍保留，首搜索结果只用于接口验证；没有同城模型结果时使用明确标注的杭州公共地点验证高德，不能把它们称为攻略推荐。

各 HTTPX 客户端显式关闭环境代理继承，并注入 MCP SDK 实际 transport。小红书服务端浏览器出口由用户独立核验，本应用不会修改代理、登录或服务状态。网络异常、未登录与空业务结果都在报告中分别记录；异常不输出带凭证 URL。

小红书服务端的代理配置由用户自行管理。本仓库不包含本地直连命令或登录凭证；P0 脚本不会修改 MCP 服务的代理、登录或会话状态。

## 产物与检查

- `reports/p0/capability-report.md`：本次真实结果和受阻项。
- `reports/p0/capabilities.json`：协议、实际 schema、字段、数量、耗时、模型用量、API 调用数。
- `reports/p0/samples.json`：匿名来源 ID 的笔记与一级评论，移除账号身份、调用句柄及外链。
- `reports/p0/map-data.json`：真实 GCJ-02 地点和实际返回路线几何。
- `reports/p0/field-differences.md`、`manual-review.md`：适配差异及人工复核清单。
- `data/`：被 Git 忽略的本地检查点和运行数据；保留匿名来源内容和图片地址以便复用，不包含登录 Cookies。

```powershell
uv --directory backend run python -m pytest -q
uv --directory backend run ruff check .
```

自动回归使用脱敏夹具/模拟传输和本机 HTTPServer，不再次访问真实账号。报告只声明已验证能力；少量样本不提供统计准确率或固定人民币成本。
