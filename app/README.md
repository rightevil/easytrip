# EasyTrip P0

本阶段提供真实服务的小样本验证、Agnes 多模态抽取、脱敏报告和一页地图。行程数据库、React 工作区与自动排程在 P1–P6 实现。

## Windows 使用

Python 与依赖均由 uv 管理，环境位于 `app/.venv`，锁定版本位于 `app/uv.lock`。需已安装 uv；无需全局 pip 安装。根目录 `.env` 会自动读取，`app/.env` 如存在则覆盖同名项，环境变量优先级最高。

从仓库根目录执行：

```powershell
uv --directory app sync --locked
uv --directory app run python -m backend.app.probe
uv --directory app run uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

打开 http://127.0.0.1:8000 查看地图。如果 uv 不在 PATH，可以使用 `app/scripts/p0.ps1`，它会查找用户目录 `.local/bin/uv.exe`：

```powershell
./app/scripts/p0.ps1 probe
./app/scripts/p0.ps1 serve
./app/scripts/p0.ps1 test
```

已有真实笔记检查点时，用 `uv --directory app run python -m backend.app.probe --reuse` 仅复用详情，再验证模型和高德；脚本支持 `./app/scripts/p0.ps1 probe -Reuse`。重复执行会产生新的模型和地图服务调用，按账号实际计费。

## 配置

参照 [.env.example](.env.example)，真实密钥只放被 Git 忽略的 `.env`。

- 小红书：`XHS_MCP`，完整 HTTP MCP URL；服务由用户维护，登录/验证码由用户处理，本应用不启动 MCP 子进程。
- 高德：`AMAP_WEB_KEY`、`AMAP_JS_KEY`、`AMAP_JS_SECURE_KEY`。远程官方 MCP 默认复用 Web Key，可用 `AMAP_MCP_URL` 单独配置。
- Agnes：支持现有 `AGNES_URL`、`AGNES_KEY`、`AGNES_MODEL`，大小写兼容（包括 `AGNES_MODEl`）；也支持通用 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_ID`。URL 为兼容 chat/completions 的 base URL 或完整端点。

高德 JS 安全密钥仅由本机后端 `_AMapService` 代理追加，不出现在页面配置。JS Key 按 SDK 要求传给浏览器，生产使用前仍需配置合法域名。服务只绑定回环地址，拒绝非本机 Host 和跨来源请求。

## 探测范围

默认明确目的地查询为“杭州一日游 攻略”，探索查询为“周末 安静 山水 高铁 旅行”；属于人工接口测试条件，不代表用户实际出行需求。可用 `--query`、`--explore`、`--city` 替换。全轮最多读取 5 篇不同笔记，最多选取每篇 10 条一级评论，关闭二级回复；最多提交 3 张可信 CDN 图片，视频未分析。工具白名单仅包含登录状态、搜索、详情。

`--poi 名称` 可重复至 3 个，用于人工选择样本中的同城线索。地点歧义仍保留，首搜索结果只用于接口验证；没有同城模型结果时使用明确标注的杭州公共地点验证高德，不能把它们称为攻略推荐。

各 HTTPX 客户端显式关闭环境代理继承，并注入 MCP SDK 实际 transport。小红书服务端浏览器出口由用户独立核验，本应用不会修改代理、登录或服务状态。网络异常、未登录与空业务结果都在报告中分别记录；异常不输出带凭证 URL。

两个现有小红书 exe 强制忽略系统代理的命令见 [小红书直连说明](docs/xhs-direct.md)。该设置只影响用户随后启动的子进程，不会被 P0 脚本自动应用。

## 产物与检查

- `reports/p0/capability-report.md`：本次真实结果和受阻项。
- `reports/p0/capabilities.json`：协议、实际 schema、字段、数量、耗时、模型用量、API 调用数。
- `reports/p0/samples.json`：匿名来源 ID 的笔记与一级评论，移除账号身份、调用句柄及外链。
- `reports/p0/map-data.json`：真实 GCJ-02 地点和实际返回路线几何。
- `reports/p0/field-differences.md`、`manual-review.md`：适配差异及人工复核清单。
- `data/`：被 Git 忽略的本地检查点和运行数据；保留匿名来源内容和图片地址以便复用，不包含登录 Cookies。

```powershell
uv --directory app run python -m pytest -q
uv --directory app run ruff check .
```

自动回归使用脱敏夹具/模拟传输和本机 HTTPServer，不再次访问真实账号。报告只声明已验证能力；少量样本不提供统计准确率或固定人民币成本。
