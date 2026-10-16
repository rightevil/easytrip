# Windows 源码交付与发布判定

当前产品版本 `0.1.0`，交付性质为**未发布的验收候选**。源码及真实小样本链路已有工程证据；T05正在进行完整同口径整改复测，T07已按用户接受的AI基准补齐，详见 [P6报告](p6-acceptance.md)。没有提前bump或创建发布Tag。

## 环境与安装

本机验证环境：Windows、uv 0.12.19、Python 3.12.14、Node 24.20.0、npm 11.19.0。Python最低版本与Node支持范围以 `backend/pyproject.toml`、`frontend/package.json` 为准；依赖严格使用 `backend/uv.lock` 和 `frontend/package-lock.json`。

解压源码包到新的空目录，在该目录 PowerShell 执行：

```powershell
./scripts/dev.ps1 setup
./scripts/dev.ps1 build
./scripts/dev.ps1 check
./scripts/dev.ps1 serve
```

打开 `http://127.0.0.1:8000`；关闭终端或 Ctrl+C 停止该后端。预先构建后，启动只需uv/Python，不要求Node/npm。端口冲突时自行停止旧EasyTrip或指定 `serve -Port 8001`，不会终止其他进程。

源码包不含真实 `.env`。可先在无外部服务配置时创建/保存/重开草稿；需要攻略和地图时，在本机从 `.env.example` 创建 `.env` 并填写自己的配置，再重启EasyTrip。检查中的 `configured_not_probed` 不表示服务可用。不要把密钥、Cookie、原始笔记、数据库或运行报告提交到Git或发给他人。

MCP服务由用户自行维护；安装脚本不安装/启动/重启它们，不改变全局代理、防火墙或系统权限。备份、恢复到正式路径的安全步骤见 [Windows运维说明](windows-operations.md)。

2026-10-16已在 `9476d3e` 源码包的新解压目录实跑以上安装、构建、检查和启动；清除该子进程外部服务配置并移除Node/npm的PATH后，已构建工作区仍能启动。两条草稿隔离保存、重复消息去重、纯本地任务、停止后重开，以及脚本备份/恢复后完整快照一致均通过。演练未写正式库、未调用外部业务服务；逐项证据见 [P6第五步记录](p6-acceptance.md)。这不是换一台全新Windows机器的测试。

当前v11工程候选包对应 `cfffa00`、0.1.0，181个文件的manifest、CRC及SHA核验通过，当前完整后端1162项、Ruff和改动测试格式检查通过。相比上述已实装包，启动入口、依赖、前端、迁移和维护工具均未变；变更在抽取、回归夹具及说明。新候选包未重新实装；本机包与核验记录位于 `reports/p6/EasyTrip-source-cfffa00.zip` 和 `candidate-delivery-v11.json`，正式版本包仍待完整质量结论。

## 可复核源码包

仓库维护者在已提交且工作树无跟踪变更时运行：

```powershell
New-Item -ItemType Directory -Force ./reports | Out-Null
uv --directory backend run python ../scripts/package.py ../reports/EasyTrip-source.zip
```

打包只读取固定HEAD的Git blob，包含应用、迁移、Windows脚本、锁文件、脱敏测试集、配置模板、能力报告和验收说明。`manifest.json` 记录源码revision、产品版本和各文件SHA256。目标必须不存在；不自动覆写。

私有路径、真实配置、Cookie、数据库、原始来源/报告、依赖安装目录和Git历史不进入包；打包前扫描本机已配置凭证。它是源码包，首次使用仍需按锁文件安装依赖、构建前端；无需把开发者的 `.venv/node_modules` 搬过去。保留源码包校验值及对应验收记录，不把包内存在文件当作安装成功。

## 当前发布门槛

可交付代码、锁文件、启动/恢复入口和实际测试报告；推荐质量仍不能声称通过。用户已明确继续质量整改与同口径复测，保留完整发布目标；不再等待体验发布范围选择，也不重新要求逐条人工标注。质量报告继续保留assistant身份、固定分母与未通过项。

只有完整门槛通过后，才按根目录 `.cz.toml` 执行 `cz bump --version-files-only --check-consistency --yes`，准备版本、`backend/pyproject.toml` 和Changelog；再执行 `uv --directory backend lock` 并复测。审查并一并提交后，为这个已含最终锁文件的提交创建对应版本Tag，再从该提交打包。不要直接用默认bump提前建Tag，导致Tag与后续锁文件不一致。当前普通切片与候选源码包不等于发布动作已完成。
