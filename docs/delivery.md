# Windows 源码交付与发布判定

当前产品版本 `0.1.0`，交付性质为**未发布的验收候选**。源码及真实小样本链路已有工程证据；T05语义质量失败、T07认证范围不足仍见 [P6报告](p6-acceptance.md)。没有提前bump或创建发布Tag。

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

## 可复核源码包

仓库维护者在已提交且工作树无跟踪变更时运行：

```powershell
New-Item -ItemType Directory -Force ./reports | Out-Null
uv --directory backend run python ../scripts/package.py ../reports/EasyTrip-source.zip
```

打包只读取固定HEAD的Git blob，包含应用、迁移、Windows脚本、锁文件、脱敏测试集、配置模板、能力报告和验收说明。`manifest.json` 记录源码revision、产品版本和各文件SHA256。目标必须不存在；不自动覆写。

私有路径、真实配置、Cookie、数据库、原始来源/报告、依赖安装目录和Git历史不进入包；打包前扫描本机已配置凭证。它是源码包，首次使用仍需按锁文件安装依赖、构建前端；无需把开发者的 `.venv/node_modules` 搬过去。保留源码包校验值及对应验收记录，不把包内存在文件当作安装成功。

## 当前发布门槛

可交付代码、锁文件、启动/恢复入口和实际测试报告；推荐质量仍不能声称通过。需要在具体交付物与限制审阅后明确后续：继续质量整改/同口径复测，或由用户明确接受有限体验发布范围。不会重新强制逐条人工标注，也不会暗自改成只有手动地点功能的产品。

只有门槛或用户明确选择的范围通过后，才按根目录 `.cz.toml` 执行 `cz bump`，同步 `backend/pyproject.toml` 与 `backend/uv.lock`，复测并生成对应Changelog和Tag。当前普通切片与候选源码包不等于这一发布动作已经完成。
