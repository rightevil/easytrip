# EasyTrip

旅行与约会规划助手。当前实现了 **P1 行程持久化、业务 API 与 React 草稿工作区**，可创建、编辑、保存和重开行程。P0 数据源 / 模型 / 地图验证入口继续保留；真实攻略理解、候选地点和排程尚未接入。

## 仓库结构

- `backend/`：FastAPI、SQLite 与 P0 探测程序，Python 项目独立使用 uv。
- `frontend/`：React + TypeScript 草稿工作区，npm 管理锁定依赖。
- `scripts/probes/`：P0 的 Windows 启动脚本。
- `docs/`：公开且经过审查的设计文档入口；本地未审核资料暂不自动入库。
- `data/`、`reports/`：本地运行产物（Git 忽略）。
- `.cz.toml`：根目录 Commitizen 配置，管理整个产品的统一版本。

## 快速启动（PowerShell）

在仓库根目录运行：

```powershell
uv --directory backend sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv --directory backend run uvicorn easytrip.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

工作区：http://127.0.0.1:8000 。独立 P0 地图验证：http://127.0.0.1:8000/p0/map 。P1 不需要填写外部服务配置。

也可使用 Windows 入口：首次 `./scripts/dev.ps1 setup`，然后 `./scripts/dev.ps1 build`、`./scripts/dev.ps1 serve`；`./scripts/dev.ps1 test` 运行前后端回归。`serve -Port 8001` 可选择本机端口。脚本只运行本应用，不修改代理或管理小红书服务。

```powershell
./scripts/probes/p0.ps1 probe
./scripts/probes/p0.ps1 serve
./scripts/probes/p0.ps1 test
```

参照根目录 `.env.example` 创建本地 `.env`（勿提交凭证）。更多说明见 [backend/README.md](backend/README.md)。

## 测试

```powershell
uv --directory backend run python -m pytest -q
uv --directory backend run ruff check .
npm --prefix frontend test
```

## 提交与版本

全项目使用同一版本号，初始为 `0.1.0`。已配置根目录 `.cz.toml`，**无需再运行 `cz init`**：

```powershell
uv tool install commitizen
cz commit
cz bump
```

`cz bump` 会更新版本配置、`backend/pyproject.toml` 和 `CHANGELOG.md`，并创建 Git Tag。采用 `version_provider = "commitizen"` 时，`backend/uv.lock` 不会自动更新，发布前请运行 `uv --directory backend lock` 并提交锁文件。不要在仅有文档或重构提交时人为提高版本。

## 说明

条件、消息、工作区和任务保存在 `data/easytrip.sqlite3`（Git 忽略）。保存失败或发生版本冲突时，界面保留当前编辑并提示处理；关闭页面前应等待“已保存”，未发送消息仅在当前浏览器会话中保留。

“准备工作区”运行本地三阶段任务，检查条件、汇总已保存内容，支持检查点、取消和重启后恢复。P1 不请求小红书、Agnes 或高德。请只启动一个后端进程，不使用多个 worker 或自动 reload；未完成任务重启后等待用户恢复。

前端开发方式见 [frontend/README.md](frontend/README.md)，API 契约见 [backend/easytrip/api/README.md](backend/easytrip/api/README.md)。旧的 `app/` 路径已迁移到 `backend/`。

P1 五步已交付，测试证据和限制见 [P1 验收记录](docs/p1-acceptance.md)。
