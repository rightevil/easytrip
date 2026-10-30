# EasyTrip

EasyTrip `0.2.0` 完成[P6工程交付与质量验收](docs/p6-acceptance.md)：20篇/100评论主正确率90.7%、可判覆盖82.4%，地点固定20例符合用户接受的AI基准。已支持单日/多日规划、逐晚住宿、用户确认铁路及接驳、完成/锁定与历史版本保护。安装、打包和恢复见 [Windows交付说明](docs/delivery.md)，真实服务及质量限制分别记录；[P4既有验收](docs/p4-acceptance.md)与[P5记录](docs/p5-acceptance.md)继续保留。

多日在“时间安排”设置每日窗口、活动日期和每晚具体酒店，可填写用户已确认的入住/退房时间与该晚总费用；空值保持未知。铁路需先核对具体火车站，再填入并明确确认班次与完整日期时刻。缺酒店或车次保留草案；生成后明确采用，修改时重新校验并保留旧版，不提供订房、售票或库存承诺。

也可直接在“地点与地图”输入城市和候选地名，搜索高德并确认具体地址，保存为“用户指定”候选。分别点击“加入地图”“加入行程”，再到“时间安排”填写停留时间、生成并选择采用方案；修改后点击重新校验。此入口无需先运行攻略发现，手动地点不附加攻略或评价依据，营业和费用保持待核实。

旅行与约会规划助手。已实现行程保存、**P2 只读攻略发现与来源证据**、**P3 高德地点核对与候选地图**、**P4 单日路线与时间表**和 **P5 多日住宿与铁路衔接**。固定文本样本的对象/主要维度已按AI参考验收；每轮推荐仍需结合来源核对，地点确认不代表营业、价格或预约已核实。P0 验证入口继续保留。

## 仓库结构

- `backend/`：FastAPI、SQLite 与 P0 探测程序，Python 项目独立使用 uv。
- `frontend/`：React + TypeScript 草稿工作区，npm 管理锁定依赖。
- `scripts/probes/`：P0 的 Windows 启动脚本。
- `docs/`：公开且经过审查的设计文档入口；本地未审核资料暂不自动入库。
- `data/`、`reports/`：本地运行产物（Git 忽略）。
- `.cz.toml`：根目录 Commitizen 配置，管理整个产品的统一版本。

## Windows 桌面版

发现工作区采用紧凑条件摘要、可展开的条件编辑和候选/地图同屏布局；小窗口改为纵向展示，原地图页状态继续兼容。已保存方案优先展示，采集统计与任务用量按需展开。模型 HTTP 错误会显示台账中的状态码；这不会解除供应商限流，也不会自动重发未知费用请求。

运行 `./scripts/build-desktop.ps1` 生成 `dist/EasyTrip.exe`。使用 pywebview + PyInstaller 单文件打包，双击打开独立窗口，使用者不需要安装 Python 或 Node。Windows 需安装 [Microsoft Edge WebView2 运行时](https://developer.microsoft.com/microsoft-edge/webview2/)；本机服务只监听随机回环端口，关闭窗口时一并退出。

左侧「设置」可填写小红书 MCP、12306 MCP、高德 Web/JS Key、安全密钥、可选高德 MCP 地址及模型服务地址、Key、模型名。保存后关闭并重新打开应用生效，运行中的任务不会在保存时切换凭证。已保存的地址和密钥不回显；空白输入保留原值，显式「清除」才会移除。

首次启动且没有本机配置时自动进入使用引导，可以选择「稍后设置，先建行程」，跳过状态会保存在本机。设置页支持选择已有 UTF-8 `.env` 文件（最多 64 KB）：先预览将更新的字段，再点「确认导入并保存」。兼容大小写不同的 `AGNES_*` / `12306_MCP` 别名，非空标准字段优先；空值和无关字段跳过，不执行环境变量展开。冲突的重复项、格式错误、无效地址不会写入；导入只更新配置，不迁移或覆盖行程。

「本机配置检查」列出缺失项、格式问题与配置齐全的服务，可检查尚未保存的编辑。检查不发起外部请求，也不修改配置；「配置齐全，未测试连接」不代表凭证有效、MCP 已启动或小红书已登录。

桌面版配置与行程分别保存在 `%LOCALAPPDATA%\EasyTrip\.env`、`%LOCALAPPDATA%\EasyTrip\easytrip.sqlite3`，更换 exe 不会删除数据。配置文件含明文凭证，请勿分享。构建只包含程序与网页资源，不包含仓库 `.env`、行程数据库或私人报告。小红书与 12306 MCP 仍需单独启动；小红书需先完成登录。外部服务需联网并可能计费。

桌面版初次运行使用独立数据目录，不会自动修改仓库数据。迁移已有配置时，在关闭桌面版后将原 `.env` 复制到上述配置位置（先备份已存在的文件；兼容 `AGNES_*` / `12306_MCP`）。迁移行程请先关闭网页版与桌面版，再运行 `./scripts/dev.ps1 backup -Destination "$env:LOCALAPPDATA\EasyTrip\easytrip.sqlite3"`，目标文件必须尚不存在，勿直接覆盖已使用的桌面数据库。也可以用 `EasyTrip.exe --data-dir "D:\EasyTripData"` 指定独立数据目录。

源码运行时，设置页保存到 `data/.env`，其显式值（含清空）优先于原根目录 `.env`、`backend/.env` 和进程环境；此文件同样不进入 Git。

## 快速启动（PowerShell）

在仓库根目录运行：

```powershell
uv --directory backend sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv --directory backend run uvicorn easytrip.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log
```

工作区：http://127.0.0.1:8000 。独立 P0 地图验证：http://127.0.0.1:8000/p0/map 。P1 不需要填写外部服务配置。

也可使用 Windows 入口：首次 `./scripts/dev.ps1 setup`，然后 `./scripts/dev.ps1 build`、`./scripts/dev.ps1 check`、`./scripts/dev.ps1 serve`；`./scripts/dev.ps1 test` 运行前后端回归。`serve -Port 8001` 可选择本机端口。检查仅报告依赖、构建、数据库及配置完整性，不请求外部服务。已有前端构建时，启动不需要 Node/npm。脚本只运行本应用，不修改代理或管理小红书服务。

升级前先退出 EasyTrip，在旧版本执行 `./scripts/dev.ps1 backup`，保留备份后再更新代码。新版本启动遇到旧库也会先自动一致性备份，备份失败即停止迁移。手动备份、恢复及正式路径替换说明见 [Windows 运行与数据恢复](docs/windows-operations.md)。恢复仅接受不存在的目标文件，不覆盖已有数据。

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

GitHub Actions 在 `main`、PR 和手动触发时运行 Windows 回归与单文件打包；推送 `v*` Tag 时，仅在同一流水线的测试、构建、版本和产物检查全部通过后发布 GitHub Release。Release 附带版本化 x64 exe 和 `SHA256SUMS.txt`；不上传本机配置或数据库。

发布仍由 Commitizen 管理，先提交功能改动，再运行 `cz bump --version-files-only --check-consistency --yes`；随后执行 `uv --directory backend lock`，把版本、锁文件与 Changelog 一起提交。为该最终提交创建对应 annotated `vX.Y.Z` Tag，推送提交和 Tag 后等待 Actions 完成；不要提前给缺少锁文件变更的提交打 Tag。CI 只构建和发布，不自行更改版本或反复 bump。内部前端 npm 包版本不作为桌面产品发布版本。

当前产品版本为 `0.3.0`，由根目录 `.cz.toml` 与 `backend/pyproject.toml`统一管理，**无需再运行 `cz init`**：

T05/T07已按用户接受的AI基准达到发布门槛，不声明人工认证；最终版本与交付记录见 [P6验收报告](docs/p6-acceptance.md)。普通切片提交不 bump。

```powershell
uv tool install commitizen
cz commit
# 仅在发布门槛通过后准备版本文件与 Changelog：
cz bump --version-files-only --check-consistency --yes
uv --directory backend lock
./scripts/dev.ps1 test
```

根目录配置会更新版本、`backend/pyproject.toml` 和 `CHANGELOG.md`，`--version-files-only`避免提前创建提交和Tag。`backend/uv.lock`需单独同步；检查版本、锁文件、Changelog和测试后一起提交，再为该最终提交创建对应版本Tag并生成源码包。不要让Tag停在尚未更新锁文件的提交，也不要在普通文档或重构切片提高版本。

## 说明

条件、消息、工作区和任务保存在 `data/easytrip.sqlite3`（Git 忽略）。保存失败或发生版本冲突时，界面保留当前编辑并提示处理；关闭页面前应等待“已保存”，未发送消息仅在当前浏览器会话中保留。

“准备工作区”运行本地三阶段任务，检查条件、汇总已保存内容，支持检查点、取消和重启后恢复。P1 不请求小红书、Agnes 或高德。请只启动一个后端进程，不使用多个 worker 或自动 reload；未完成任务重启后等待用户恢复。

前端开发方式见 [frontend/README.md](frontend/README.md)，API 契约见 [backend/easytrip/api/README.md](backend/easytrip/api/README.md)。旧的 `app/` 路径已迁移到 `backend/`。

P1 五步已交付，测试证据和限制见 [P1 验收记录](docs/p1-acceptance.md)。

在行程中填写条件或想法，点击“保存并发现”保存原文并创建发现任务，也可单独点击“发现攻略”。目的地可空；有来源的探索方向需要明确确认后才写入目的地。“保存想法”和“准备工作区”不请求外部服务。P2 需要后端读取本机 `.env` 中的小红书与模型配置。

每轮默认最多 8 次搜索、30 篇目标笔记、每篇 20 条一级评论、1800 秒；主搜索与读取最多占总时限的三分之一，其余时间优先用于分析。继续一轮时，同条件的需求理解与兼容成功分析直接沿用，先分析已保存资料，再按剩余预算补搜；不会自动续轮。二级回复默认关闭，餐饮住宿补查共享预算。部分结果保留来源、未完成项与实际用量。模型失败或结果未知后，新轮需确认新增预算；未知费用不会显示为零。

行程底部“调用用量”可读取已记录的任务、地点和铁路查询用量，以及缓存、失败、在途和未知结果；也可打开脱敏 JSON 报告。同步查询从 schema 9 起记账，历史未计量操作不补算，地图底图与 MCP 握手不作为业务调用计数。token 是用量；未核对账号单价与账单前，人民币费用始终未核定。

2026-10-17完成固定20篇/100评论的v11 Agnes同口径复测：对象/主要维度正确68/75（90.7%）、可判覆盖75/91（82.4%），达到90%/80%门槛。沿用用户接受的AI参考及assistant身份，保留5条用户真实反馈；没有重启逐条人工流程或改写原91可判/9不足分母。17篇有效抽取、2篇失败、1篇结果未知；16条可判弃判及3条不足超判均保留。性质62/75、立场55/75仍有错误，固定文本达标不代表所有推荐可靠自动采纳或图片/视频已验证。历史失败和完整证据见 [P6验收报告](docs/p6-acceptance.md)、[P2记录](docs/p2-acceptance.md) 与 [评审说明](docs/p2-review.md)。

发现结果展示后，具体地点候选会自动批量匹配高德，并按名称、类别、分店及区域线索选定默认 POI，无需逐个确认；保留备选和不确定项，需要更换时点击“调整地点”，用户已调整的结果不会被自动覆盖。首次地图展示前 8 个不同 POI 和已确认出发点；“加入地图”“加入行程”分别控制地图展示与行程选择，均不生成路线或时间表。整组替换仍需点击“刷新此筛选的 Top 8”。未取得相关真实 POI、泛称和城市方向保留为文字线索；出发地文字不会自动成为坐标。

P3 每次核对最多处理 20 个候选、40 次物理高德请求、60 秒；查询按名称、城市和 v5 版本缓存 24 小时，应用共享 1.1 秒请求间隔。排序仅使用原文短语、不同作者、亲历正负证据与来源时间；未知入口、当前营业和价格保持待核实，不将点赞或样本比例称为平台好评率。2026-10-14 已完成 20 个真实高德案例的有限检查，得到 2 个唯一匹配、17 个歧义和 1 个未匹配；这是真实接口结果与 AI 审阅，不是 20 个人工标注的质量认证。619 项后端、63 项前端测试、Ruff 和构建通过，真实 SDK 地图闭环已验证，见 [P3 实施与验收记录](docs/p3-acceptance.md)。

## 真实铁路查询

在项目根目录 `.env` 配置 `12306_MCP=http://127.0.0.1:18061/mcp`，重启 EasyTrip 后端生效。服务由用户单独维护，应用不会自动启动或重启。兼容别名 `RAIL_MCP` 非空时优先使用；地址及凭证不发送到浏览器。

在“地点与地图”手动确认两座具体火车站，再在规划条件中添加铁路段、选择日期与两站，点击“查询 12306 车次与余票”。查询会先保存编辑；选用班次只填入待确认草稿并清空旧铁路费用，核对后勾选确认，再生成或重新校验行程。参考票价不自动变成已购票费用，空值保持未知。

接入 [Joooook/12306-mcp](https://github.com/Joooook/12306-mcp) 的 Streamable HTTP `/mcp`，只调用站名映射与余票查询。单次会话总计最多 60 秒，不自动重试；整批结果限制 1MiB 文本、2000 行，按具体两站过滤后最多显示 20 班及过滤数量。车次时间采用北京时间和完整日期；跨日按历时验证，不把列车始发日期当作本站乘车日。失败、日期不可查询或无匹配时保留手工输入，不填默认车次。

EasyTrip 到 MCP 使用直连客户端；MCP 到 12306 的代理由 MCP 进程控制。本机已提供 `D:\mcp\12306-mcp\start-http-direct.cmd`，清除该进程的代理环境并仅监听本机。TUN／透明代理需要另在代理软件配置 `*.12306.cn` 直连。真实联调范围见 [P5 验收记录](docs/p5-acceptance.md)。
