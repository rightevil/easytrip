# EasyTrip

P5 多日住宿、铁路与跨城已完成开发和有限验收：每日窗口与逐晚酒店、有限住宿补查、用户确认铁路及两端接驳、跨午夜和跨日进度调整、完成/锁定及历史版本保护。达到本轮完整候选版本，现按五步推进 [P6 发布验收与单机交付](docs/p6-acceptance.md)，尚未宣布完整发布。真实服务、自动化与质量限制分别记录在 [P5 实施与验收](docs/p5-acceptance.md)；P4 单城单日能力与 [既有验收](docs/p4-acceptance.md) 继续保留。

多日在“时间安排”设置每日窗口、活动日期和每晚具体酒店，可填写用户已确认的入住/退房时间与该晚总费用；空值保持未知。铁路需先核对具体火车站，再填入并明确确认班次与完整日期时刻。缺酒店或车次保留草案；生成后明确采用，修改时重新校验并保留旧版，不提供订房、售票或库存承诺。

也可直接在“地点与地图”输入城市和候选地名，搜索高德并确认具体地址，保存为“用户指定”候选。分别点击“加入地图”“加入行程”，再到“时间安排”填写停留时间、生成并选择采用方案；修改后点击重新校验。此入口无需先运行攻略发现，手动地点不附加攻略或评价依据，营业和费用保持待核实。

旅行与约会规划助手。已实现行程保存、**P2 只读攻略发现与来源证据**、**P3 高德地点核对与候选地图**、**P4 单日路线与时间表**和 **P5 多日住宿与铁路衔接**。P2 真实模型质量验收尚未通过；地点与路线核对不能代替推荐质量验收。P0 验证入口继续保留。

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

在行程中填写条件或想法，点击“保存并发现”保存原文并创建发现任务，也可单独点击“发现攻略”。目的地可空；有来源的探索方向需要明确确认后才写入目的地。“保存想法”和“准备工作区”不请求外部服务。P2 需要后端读取本机 `.env` 中的小红书与模型配置。

每轮默认最多 8 次搜索、30 篇目标笔记、每篇 20 条一级评论、600 秒；二级回复默认关闭，餐饮住宿补查共享预算。部分结果保留来源、未完成项与实际用量。模型失败或结果未知后，新轮需确认新增预算；未知费用不会显示为零。

P2 工程与 AI 初评已交付，保留 5 条用户真实反馈，逐条人工审核按用户决定跳过。2026-10-14 已对固定 20 篇笔记／100 条评论进行一轮实际 Agnes 输出与 AI 参考的语义评测：对象/主要维度正确率 8/11（72.7%）、可判评论覆盖率 11/91（12.1%），未达到 90%/80% 门槛。30 分钟预算内取得 24 条有效评论结果，缺失和弃判仍保留在覆盖率分母中。随后完成活动隔离、提示整理和关系归属保护；12 条定向难例实模正确率 7/7、覆盖率 7/11（63.6%），覆盖仍未达标，不替代完整样本验收。候选与评价需结合原文核对，不能可靠自动采纳。详见 [P2 实施与验收记录](docs/p2-acceptance.md) 和 [AI 评分与可选人工复核说明](docs/p2-review.md)。

候选可通过“核对高德地点”取得 `unique/ambiguous/unmatched` 状态；同名分店需对照地址显式确认。首次地图展示已定位的前 8 个不同 POI 和已确认出发点；随后“加入地图”只增量加点，“加入行程”另存选择，均不生成路线或时间表。整组替换需点击并确认“刷新此筛选的 Top 8”。未匹配、泛称和城市方向保留为文字线索；出发地文字不会自动成为坐标。

P3 每次核对最多处理 20 个候选、40 次物理高德请求、60 秒；查询按名称、城市和 v5 版本缓存 24 小时，应用共享 1.1 秒请求间隔。排序仅使用原文短语、不同作者、亲历正负证据与来源时间；未知入口、当前营业和价格保持待核实，不将点赞或样本比例称为平台好评率。2026-10-14 已完成 20 个真实高德案例的有限检查，得到 2 个唯一匹配、17 个歧义和 1 个未匹配；这是真实接口结果与 AI 审阅，不是 20 个人工标注的质量认证。619 项后端、63 项前端测试、Ruff 和构建通过，真实 SDK 地图闭环已验证，见 [P3 实施与验收记录](docs/p3-acceptance.md)。

## 真实铁路查询

在项目根目录 `.env` 配置 `12306_MCP=http://127.0.0.1:18061/mcp`，重启 EasyTrip 后端生效。服务由用户单独维护，应用不会自动启动或重启。兼容别名 `RAIL_MCP` 非空时优先使用；地址及凭证不发送到浏览器。

在“地点与地图”手动确认两座具体火车站，再在规划条件中添加铁路段、选择日期与两站，点击“查询 12306 车次与余票”。查询会先保存编辑；选用班次只填入待确认草稿并清空旧铁路费用，核对后勾选确认，再生成或重新校验行程。参考票价不自动变成已购票费用，空值保持未知。

接入 [Joooook/12306-mcp](https://github.com/Joooook/12306-mcp) 的 Streamable HTTP `/mcp`，只调用站名映射与余票查询。单次会话总计最多 60 秒，不自动重试；整批结果限制 1MiB 文本、2000 行，按具体两站过滤后最多显示 20 班及过滤数量。车次时间采用北京时间和完整日期；跨日按历时验证，不把列车始发日期当作本站乘车日。失败、日期不可查询或无匹配时保留手工输入，不填默认车次。

EasyTrip 到 MCP 使用直连客户端；MCP 到 12306 的代理由 MCP 进程控制。本机已提供 `D:\mcp\12306-mcp\start-http-direct.cmd`，清除该进程的代理环境并仅监听本机。TUN／透明代理需要另在代理软件配置 `*.12306.cn` 直连。真实联调范围见 [P5 验收记录](docs/p5-acceptance.md)。
