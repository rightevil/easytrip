# Changelog

版本变更记录由 Commitizen 在 `cz bump` 时生成。

## v0.2.0 (2026-10-17)

### Feat

- **交付**: 增加安全源码打包与Windows交付清单
- **用量**: 完成P6行程调用账本与脱敏成本报告
- **运维**: 完成P6启动诊断与数据库备份恢复
- **rail**: 接入真实12306 MCP车次与余票查询
- **p5**: 完成跨日调整与完整阶段验收
- **p5**: 实现用户确认铁路与跨日两端接驳校验
- **p5**: 实现逐晚住宿补查与显式选择闭环
- **p5**: 实现逐晚酒店与多日日窗规划闭环
- **p4**: 增加手动候选地点与独立地图选择入口
- **p4**: 支持用户指定地点搜索确认与规划
- **p4**: 展示补餐依据并支持进度与方案调整
- **p4**: 补查餐饮并按进度重排未完成安排
- **p4**: 接通真实路线与单日约束排程
- **p4**: 保存规划约束与不可变方案版本
- **p3**: add verified POI matching and candidate maps
- add explicit AI provenance to P2 semantic scoring
- compare discovery directions with cited source excerpts
- **workspace**: expose discovery evidence and manual review
- **discovery**: assemble source-grounded candidates and gaps
- **discovery**: collect bounded readonly sources with recovery
- **agent**: add bounded discovery runs and model recovery
- **discovery**: persist versioned sources and evidence
- **tasks**: add persistent local preparation with safe recovery
- **workspace**: add persistent React trip drafts and autosave
- **api**: add versioned trip and workspace endpoints
- **storage**: persist trip drafts with atomic versioned updates

### Fix

- **验收**: 完成固定样本语义复测并同步质量结论
- **语义**: 对齐对象判断与地点关联的请求契约
- **语义**: 保留多对象证据并阻止明确错对象关联
- **p6**: 封闭MCP校验网络边界并补齐凭证日志脱敏
- keep route comparisons out of single place votes
- isolate invalid activities and clarify P2 comment judgements
- preserve evidence nature and bound P2 closeout scope
- ground text activity descriptions in their own citations
- preserve generic hints without inflating place counts
- repair linked evidence with missing comment objects
- resolve extraction quotes from source line ranges
- **evidence**: preserve valid fields during bounded repair
- **discovery**: version extraction cache and preserve physical model identity
- **evidence**: report schema and citation errors in one repair
- **discovery**: separate object coverage from evidence sentiment
- **model**: receive bounded Agnes streams and retain usage
- **discovery**: preserve literal source spans in model citations
- **discovery**: report all citation failures in the single repair
- **model**: enable Agnes thinking and retain usage on failures

### Refactor

- **repo**: organize EasyTrip monorepo and initialize Commitizen
