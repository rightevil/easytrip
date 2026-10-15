# Windows 运行与数据恢复

Python 使用 uv 和 `backend/uv.lock`；前端使用 Node/npm 和 `frontend/package-lock.json`。先按 `.env.example` 配置本机 `.env`，不要提交密钥或数据库。无需外部配置也可保存本地草稿。

## 安装与启动

在仓库根目录 PowerShell 执行：

```powershell
./scripts/dev.ps1 setup
./scripts/dev.ps1 build
./scripts/dev.ps1 check
./scripts/dev.ps1 serve
```

`setup` 锁定安装 Python 与前端依赖；`check` 报告前端构建、Python 包版本、数据库完整性和外部配置状态。`configured_not_probed` 只代表填写了配置，不代表服务可用。检查不打印地址、密钥、帖子或用户数据，也不会连接外部服务。缺少可选服务不影响离线草稿；构建缺失、损坏/不兼容数据库或核心依赖缺失时不能启动。

打开 `http://127.0.0.1:8000`；端口被占用时自行关闭旧 EasyTrip，或使用 `./scripts/dev.ps1 serve -Port 8001`。脚本不停止其他进程。终端按 Ctrl+C 结束当前后端；启动仅监听本机、单 worker、无 reload。已有前端构建时启动不需要 Node/npm。不要并发启动两个后端或同时升级同一数据库。

小红书和铁路 MCP 由用户维护；本脚本不安装、启动、重启或登录这些服务，也不修改全局代理。Agnes 和后端 MCP HTTP 客户端继续直连；MCP 自身对外的代理设置由其启动环境控制。

## 升级前备份

先等界面显示“已保存”，停止 EasyTrip，再在**更新代码之前**使用当前版本执行：

```powershell
./scripts/dev.ps1 backup
```

默认读取 `data/easytrip.sqlite3`，输出到 `data/backups/` 唯一文件，成功后显示目标路径、schema 版本和各表数量。备份使用 SQLite backup API，包含已经提交的 WAL 内容；不会靠复制主文件丢掉 WAL，也不会迁移源库。可指定新的目标文件：

```powershell
./scripts/dev.ps1 backup -Destination E:\easytrip-backups\before-upgrade.sqlite3
```

已有目标或同名 `-wal/-shm/-journal` 侧文件会被拒绝；失败不会覆盖原库。备份包含私人行程与来源数据，请存放在自己的受保护磁盘。应用只在检测到旧的受支持 schema 时，自动生成 `data/backups/*-before-v*.sqlite3`，成功后才迁移。自动备份是补充；不能代替更新代码前的手动备份。首次建库和同版本重开不生成迁移备份，未来版本直接拒绝。

## 恢复演练与正式恢复

先恢复到**新的文件**并校验，不覆盖当前库：

```powershell
./scripts/dev.ps1 restore -Source E:\easytrip-backups\before-upgrade.sqlite3 -Destination .\data\recovery-check.sqlite3
uv --directory backend run --locked python -m easytrip.storage.maintenance check --source ..\data\recovery-check.sqlite3
```

恢复会检查完整性、外键及该 schema 应有的表/列，再创建一致副本；不运行迁移。普通 SQLite 文件、损坏库和高于应用支持版本的库会被拒绝。可用相同版本应用在隔离目录检查行程、消息、来源、方案及活动版本；不要直接打开备份后让应用迁移唯一的备份。

正式应用固定读取 `data/easytrip.sqlite3`，恢复到别名文件不会自动生效。正式恢复步骤：

1. 等待保存后停止所有 EasyTrip 后端；确认没有进程再访问数据库。
2. 用 `backup` 保存当前库的一致副本；若当前库损坏，保全原主文件及所有 `-wal/-shm/-journal` 侧文件供排查。
3. 在服务已停止的前提下，把原主文件和存在的侧文件一起移至一个新的保留目录，确认 `data/easytrip.sqlite3` 及同名侧文件均不存在。不要只挪主文件留下旧 WAL。
4. 执行下列恢复命令，再 `check`、`serve`，核对原行程、来源和已采用方案。旧备份由启动时自动迁移，迁移前仍会生成一份备份。

```powershell
./scripts/dev.ps1 restore -Source E:\easytrip-backups\before-upgrade.sqlite3 -Destination .\data\easytrip.sqlite3
./scripts/dev.ps1 check
./scripts/dev.ps1 serve
```

脚本没有强制覆写或自动删除旧库的选项。恢复后未完成任务仍按既有恢复机制等待明确操作，结果未知的外部调用不会自动重放。
