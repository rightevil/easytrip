# EasyTrip

旅行与约会规划助手，目标是可交互的行程规划 Agent 产品。当前代码实现的是 **P0 数据源 / 模型 / 地图能力验证**，正式 Agent、React 工作区与持久化尚未实现。

## 仓库结构

- `backend/`：FastAPI 与现有 P0 探测程序，Python 项目独立使用 uv。
- `frontend/`：未来的 React 工作区（目前尚未实现）。
- `scripts/probes/`：P0 的 Windows 启动脚本。
- `docs/`：公开且经过审查的设计文档入口；本地未审核资料暂不自动入库。
- `data/`、`reports/`：本地运行产物（Git 忽略）。
- `.cz.toml`：根目录 Commitizen 配置，管理整个产品的统一版本。

## 快速启动（PowerShell）

在仓库根目录运行：

```powershell
uv --directory backend sync --locked
uv --directory backend run python -m easytrip.probe
uv --directory backend run uvicorn easytrip.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

地图验证页面：http://127.0.0.1:8000

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

当前仍是 P0 验证项目，目录设计为后续 Agent / 业务模块预留演进空间；尚未实现的模块不会用空代码冒充实现。旧的 `app/` 路径已迁移到 `backend/`，迁移后需要重新同步本地 uv 环境。
