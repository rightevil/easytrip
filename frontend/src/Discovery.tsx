import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';
import { api } from './api';
import type { Run, Workspace } from './api';
import type { Candidate, CandidatePage, DiscoveryBudget, DiscoveryResult, Evidence, Gap, InterpretedValue, Source, Understanding } from './discovery-types';
import type { TripSession } from './session';
import type { PlaceMatch, Poi } from './places-types';

const stageLabels: Partial<Record<Run['stage'], string>> = {
  understanding: '理解需求', discovering: '发现攻略', fetching: '读取笔记与评论', extracting: '分析原文与图片', evaluating: '汇总证据与缺口', candidates_ready: '候选已就绪', done: '本轮结束',
};
const statusLabels: Record<Run['status'], string> = { pending: '等待执行', running: '发现中', paused: '等待处理后继续', recoverable: '服务重启后可恢复', completed: '本轮完成', partial: '部分完成', failed: '本轮失败', cancelled: '已取消', stale: '旧条件记录' };
const categories = { eat: '餐饮', play: '游玩', stay: '住宿' };
const stances = { positive: '正面', negative: '负面', mixed: '混合观点', question: '疑问', unknown: '未判定' };
const natures = { experience: '实际体验', question: '提问', hearsay: '转述', wish: '愿望', suggestion: '建议', content_reaction: '内容互动', unknown: '属性未知' };
const linked = { linked: '已关联具体对象', unlinked: '未关联对象', uncertain: '对象指代有歧义' };
const provenance = { user: '用户明确提供', default: '默认值', inferred: '模型推测', unknown: '未知' };
const fieldLabels: Record<string, string> = { destination: '目的地', origin: '出发地', start_at: '出发时间', duration_minutes: '可用时长', transport: '交通方式', budget_cny: '预算', participants: '人数', preferences: '偏好', theme: '出游主题' };
const gapStatuses = { open: '待补查', undetermined: '尚无法判断', filled: '已有文字候选', unfilled: '补查后仍有缺口' };
const gapPeriods = { lunch: '午餐', dinner: '晚餐', overnight: '过夜住宿' };
const pauseReasons: Record<string, string> = {
  login_required: '小红书需要重新登录，请由服务维护者完成登录后恢复。',
  source_paused: '小红书服务需要处理，已保留取得的内容。',
  network_uncertain: '网络响应不确定，可能已有调用完成。请检查服务后恢复。',
  model_failed: '模型分析未通过，已取得的来源仍保留。',
  model_usage_unknown: '模型调用未返回完整用量，费用存在未知部分。',
  app_restarted: '上次任务因服务重启中断，可从已保存进度继续。',
  request_changed: '行程条件已更新，本轮只保留为历史记录。',
  user_cancelled: '取消状态已确认，已有来源与检查点仍保留。',
  invalid_run_data: '任务数据不完整，请开始新的发现任务。',
  source_login_or_access_required: '小红书登录或访问受限。已取得内容仍保留，请由服务维护者处理后恢复。',
  model_configuration_missing: 'Agnes 配置尚未就绪。请在本机配置后恢复。',
  model_request_failed: '模型调用结果未知，可能已执行，费用尚未确认。已取得来源和未知用量仍保留；本轮不能恢复，请确认新增预算后继续一轮。',
  model_output_invalid: '模型结果未通过结构与引用校验，原始来源仍保留。',
  discovery_stage_failed: '发现阶段执行失败，已有内容保留，请检查本机服务后恢复。',
  storage_unavailable: '本机存储暂不可用，请恢复存储后重试。',
  trip_archived: '行程已归档，任务已停止。',
  stage_not_available: '本轮部分阶段未能完成，已有结果保留。',
  model_retry_requires_new_run: '本轮模型调用失败或结果未知，重复提交图片存在额外用量风险。已取得内容保留，请确认新增预算后继续一轮。',
  model_result_unknown: '模型调用结果未知，可能已执行，费用尚未确认。已取得来源和未知用量仍保留；本轮不能恢复，请确认新增预算后继续一轮。',
  note_target_not_reached: '本轮新增取得的笔记未达到目标数量。',
  comments_partly_unanalysed: '本轮选取的部分评论尚未完成分析。',
  activities_partly_unanalysed: '部分活动引用未通过校验，已保留有效评论；相关活动未纳入候选。',
  no_source_backed_candidates: '当前来源尚未形成有依据的地点候选。',
  insufficient_exploration_directions: '有来源的探索方向不足两个，暂无法充分比较。',
};
const active = (run: Run) => ['pending', 'running'].includes(run.status);
export const discoveryBudgetExhausted = (run: Run) => /budget_exhausted|budget_reached/.test(run.stop_reason ?? '')
  || run.usage.steps >= run.budget.max_steps || run.usage.elapsed_seconds >= run.budget.max_seconds;
const errorText = (error: unknown) => error instanceof Error ? error.message : '暂时无法读取，请重试。';
const count = (value: number | null | undefined) => value == null ? '未知' : value.toLocaleString('zh-CN');
const runResult = (run: Run | undefined) => run?.result && !('conditions' in run.result) ? run.result as DiscoveryResult : null;

export function Discovery({ session, requestVersion, category, archived }: { session: TripSession; requestVersion: number; category: Workspace['category']; archived: boolean }) {
  const { runs, busy, error, operation, discoveryBudget } = useSyncExternalStore(session.subscribeRuns, session.getRunSnapshot);
  const { sending } = useSyncExternalStore(session.subscribeChat, session.getChatSnapshot);
  const places = useSyncExternalStore(session.subscribePlaces, session.getPlacesSnapshot);
  const workspace = useSyncExternalStore(session.workspace.subscribe, session.workspace.getSnapshot).value;
  const discoveries = runs.filter(run => run.kind === 'discover').sort((a, b) => b.created_at.localeCompare(a.created_at));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = discoveries.find(run => run.id === selectedId) ?? discoveries.find(run => run.request_version === requestVersion) ?? discoveries[0];
  const historical = Boolean(selected && (selected.request_version !== requestVersion || selected.status === 'stale'));
  const [page, setPage] = useState<CandidatePage | null>(null);
  const [gaps, setGaps] = useState<Gap[]>([]);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [detail, setDetail] = useState<Candidate | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [mentionSources, setMentionSources] = useState<Source[]>([]);
  const [source, setSource] = useState<Source | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [confirmDirection, setConfirmDirection] = useState<Candidate | null>(null);
  const [confirmContinue, setConfirmContinue] = useState(false);
  const [confirmMatch, setConfirmMatch] = useState<{ candidate: Candidate; match: PlaceMatch } | null>(null);
  const [chosenPoi, setChosenPoi] = useState<Poi | null>(null);
  const context = `${session.trip.id}:${selected?.id ?? ''}:${category}`;
  const contextRef = useRef(context);
  contextRef.current = context;
  const previousContext = useRef('');
  const detailSequence = useRef(0);

  useEffect(() => { if (selected) void session.readPlaces(selected.id); }, [session, selected?.id, selected?.updated_at]);

  useEffect(() => {
    let alive = true;
    if (previousContext.current !== context) { setPage(null); setGaps([]); setDetail(null); setSource(null); detailSequence.current++; }
    previousContext.current = context;
    if (!selected) return;
    setLoading(true); setReadError(null);
    void Promise.all([api.candidates(session.trip.id, selected.id, category), api.gaps(session.trip.id, selected.id)]).then(([candidates, missing]) => {
      if (!alive) return;
      if (candidates.items.some(item => item.trip_id !== session.trip.id || item.run_id !== selected.id || item.request_version !== selected.request_version)
        || missing.items.some(item => item.trip_id !== session.trip.id || item.run_id !== selected.id || item.request_version !== selected.request_version)) throw new Error('结果归属不一致，已阻止展示。');
      setPage(candidates); setGaps(missing.items);
    }).catch(reason => { if (alive) setReadError(errorText(reason)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [session, selected?.id, selected?.updated_at, category, context, refresh]);

  const loadMore = async () => {
    if (!page || !selected) return;
    const requested = context;
    setLoading(true);
    try {
      const more = await api.candidates(session.trip.id, selected.id, category, page.offset + page.items.length);
      if (contextRef.current !== requested) return;
      if (more.items.some(item => item.trip_id !== session.trip.id || item.run_id !== selected.id || item.request_version !== selected.request_version)) throw new Error('结果归属不一致，已阻止展示。');
      setPage({ ...more, offset: 0, items: [...page.items, ...more.items.filter(item => !page.items.some(existing => existing.id === item.id))] });
    } catch (reason) { if (contextRef.current === requested) setReadError(errorText(reason)); }
    finally { if (contextRef.current === requested) setLoading(false); }
  };
  const showEvidence = async (candidate: Candidate) => {
    const requested = context;
    const sequence = ++detailSequence.current;
    setDetail(candidate); setEvidence([]); setMentionSources([]); setSource(null); setDetailError(null); setDetailLoading(true);
    try {
      const result = await api.evidence(session.trip.id, candidate.id);
      if (contextRef.current !== requested || sequence !== detailSequence.current) return;
      if (result.items.some(item => item.trip_id !== session.trip.id || item.run_id !== candidate.run_id || item.request_version !== candidate.request_version)) throw new Error('证据归属不一致，已阻止展示。');
      if ((result.sources ?? []).some(item => item.trip_id !== session.trip.id || item.run_id !== candidate.run_id || item.request_version !== candidate.request_version)) throw new Error('提及来源归属不一致，已阻止展示。');
      setEvidence(result.items);
      setMentionSources(result.sources ?? []);
    } catch (reason) { if (contextRef.current === requested && sequence === detailSequence.current) setDetailError(errorText(reason)); }
    finally { if (contextRef.current === requested && sequence === detailSequence.current) setDetailLoading(false); }
  };
  const showSource = async (item: Evidence) => {
    const requested = context;
    const sequence = ++detailSequence.current;
    setDetailLoading(true); setDetailError(null);
    try {
      const original = await api.source(session.trip.id, item.source_id, item.run_id);
      if (contextRef.current !== requested || sequence !== detailSequence.current) return;
      if (original.trip_id !== session.trip.id || original.id !== item.source_id || original.run_id !== item.run_id || original.request_version !== item.request_version) throw new Error('来源归属不一致，已阻止展示。');
      setSource(original);
    } catch (reason) { if (contextRef.current === requested && sequence === detailSequence.current) setDetailError(errorText(reason)); }
    finally { if (contextRef.current === requested && sequence === detailSequence.current) setDetailLoading(false); }
  };
  const result = runResult(selected);
  const understanding = selected?.checkpoint.understanding ?? result?.understanding;
  const placeSnapshot = places.snapshot?.run_id === selected?.id ? places.snapshot : null;
  const ranked = placeSnapshot?.candidates.filter(candidate => category === 'all' || candidate.category === category);
  const shownCandidates = ranked?.length ? ranked : page?.items;
  const canChangePlaces = !archived && !busy && !places.busy && !historical;

  return <div className="discovery">
    <div className="discovery-heading"><div><h3>有来源的候选地点</h3><p>先发现和比较，再核对高德地点。泛称保留为线索，P2 语义质量仍未验证。</p></div><button className="primary" disabled={archived || busy || sending || places.busy || runs.some(active) || operation?.kind === 'prepare_workspace'} onClick={() => { void session.startDiscovery(); }}>{busy ? '正在提交…' : operation?.kind === 'discover' ? '重试发现任务' : '发现攻略'}</button></div>
    <BudgetEditor budget={discoveryBudget} disabled={busy || archived} onChange={budget => session.setDiscoveryBudget(budget)} />
    {error && <div className="message-error" role="alert"><p>{error}</p>{operation?.kind === 'discover' && <button className="text-button" disabled={busy} onClick={() => session.forgetRunOperation()}>放弃这次重试</button>}</div>}
    {discoveries.length > 0 && <label className="discovery-run-selector">查看发现轮次<select value={selected?.id ?? ''} onChange={event => setSelectedId(event.target.value)}>{discoveries.map((run, index) => <option key={run.id} value={run.id}>第 {discoveries.length - index} 轮 · 条件版本 {run.request_version} · {statusLabels[run.status]}</option>)}</select></label>}
    {selected ? <>
      <div className={`discovery-status ${historical ? 'historical' : ''}`} role="status"><strong>{historical ? '旧条件的发现记录' : statusLabels[selected.status]}</strong><span>{stageLabels[selected.stage] ?? '任务准备中'} · 条件版本 {selected.request_version}</span></div>
      {historical && <p className="discovery-note">这些结果属于旧条件，仅供查看。采用方向与恢复将不会用于覆盖当前条件。</p>}
      <Coverage run={selected} />
      {understanding && <UnderstandingView understanding={understanding} />}
      {selected.stop_reason && <p className="discovery-note">{discoveryBudgetExhausted(selected) ? '本轮预算已耗尽，已有结果保留。继续一轮会创建新任务并使用新增预算。' : pauseReasons[selected.stop_reason] ?? '本轮已停止或暂停，请检查服务状态及已取得内容后继续。'}</p>}
      <div className="discovery-actions">{active(selected) && <button disabled={busy} onClick={() => { void session.controlRun(selected.id, 'cancel'); }}>取消发现任务</button>}{!historical && ['paused', 'recoverable', 'cancelled', 'failed', 'partial'].includes(selected.status) && !['done', 'candidates_ready'].includes(selected.stage) && !discoveryBudgetExhausted(selected) && selected.error_code !== 'invalid_run_data' && !['model_retry_requires_new_run', 'model_result_unknown', 'model_request_failed'].includes(selected.stop_reason ?? '') && !archived && <button disabled={busy || sending} onClick={() => { void session.controlRun(selected.id, 'resume'); }}>恢复发现任务</button>}{!active(selected) && !archived && <button disabled={busy || sending || places.busy || runs.some(active) || operation?.kind === 'prepare_workspace'} onClick={() => setConfirmContinue(true)}>继续一轮（新增预算）</button>}<button disabled={!canChangePlaces || active(selected)} onClick={() => { void session.matchPlaces(selected.id); }}>{places.busy ? '正在核对地点…' : '核对本轮高德地点'}</button></div>
      {places.error && <div className="message-error" role="alert"><p>{places.error}</p><button disabled={places.busy} onClick={() => { void session.readPlaces(selected.id); }}>重新读取地点状态</button></div>}
      {readError && <div className="message-error" role="alert"><span>{readError}</span><button className="text-button" onClick={() => setRefresh(value => value + 1)}>重新读取候选</button></div>}
      {loading && !page && <p className="discovery-note" role="status">正在读取本轮候选与缺口…</p>}
      {page && <><div className="candidate-count">本轮此类别 {ranked?.length || page.total} 个候选 <span>基于取得样本，不代表全平台评价</span></div><div className="candidate-list">{shownCandidates?.map(candidate => {
        const match = placeSnapshot?.matches.find(item => item.candidate_id === candidate.id);
        const mapVisible = (places.snapshot?.points ?? []).some(point => point.candidate_ids.some(id => workspace.map_visible_ids.includes(id)) && (point.candidate_ids.includes(candidate.id) || point.id === match?.poi?.id));
        return <CandidateCard key={candidate.id} candidate={candidate} match={match} historical={historical} canAdopt={canChangePlaces} mapVisible={mapVisible} selected={workspace.selected_ids.includes(candidate.id)} focused={places.focus?.candidate_id === candidate.id}
          onEvidence={() => { void showEvidence(candidate); }} onAdopt={() => setConfirmDirection(candidate)} onConfirm={() => { if (match) { setConfirmMatch({ candidate, match }); setChosenPoi(null); } }}
          onMap={() => { if (mapVisible) session.focusPlace(candidate.id); else void session.mapAction('add', candidate.id); }}
          onSelect={() => { void session.mapAction(workspace.selected_ids.includes(candidate.id) ? 'unselect' : 'select', candidate.id); }} />;
      })}</div>{!ranked?.length && page.items.length < page.total && <button className="text-button" disabled={loading} onClick={() => { void loadMore(); }}>加载更多候选</button>}{!shownCandidates?.length && <p className="discovery-empty">本轮此类别尚无可用候选。任务失败、未读内容和服务限制不等同于当地没有地点。</p>}</>}
      {gaps.length > 0 && <section className="gap-section" aria-label="餐饮住宿缺口"><h4>餐饮与住宿的待确认项</h4>{gaps.map(gap => <article key={gap.id} className={`gap-card gap-${gap.status}`}><div><strong>{gap.period ? gapPeriods[gap.period] : gap.kind === 'meal' ? '餐饮' : '住宿'}</strong><span className="badge">{gapStatuses[gap.status]}</span></div><p>{gap.reason}</p>{gap.area && <small>区域：{gap.area}</small>}{gap.conditions.length > 0 && <small>条件：{gap.conditions.join('；')}</small>}</article>)}</section>}
    </> : <div className="discovery-empty"><strong>从一个愿望开始发现</strong><p>目的地可以留空。发现会调用小红书只读服务和 Agnes，按本轮预算取得来源并分析；数量不足与未知项会如实保留。</p></div>}
    {detail && <DiscoveryDialog title={`${detail.name} · 来源与证据`} onClose={() => { setDetail(null); setSource(null); detailSequence.current++; }}><div className="trace-description">证据是本轮对原文的结构化判断，疑问、转述和对象歧义分别保留。</div>{detailLoading && <p role="status">正在读取原文…</p>}{detailError && <p className="inline-error" role="alert">{detailError}</p>}{source ? <SourceView source={source} back={() => setSource(null)} /> : <>{mentionSources.length > 0 && <section className="mention-sources" aria-label="候选提及的原始来源"><h3>提及原文</h3><p>这些来源支持候选名称提及，不代表赞成评价或已核实事实。</p>{mentionSources.map(item => <article key={item.id}><strong>{item.title || (item.kind === 'comment' ? '评论原文' : '笔记原文')}</strong><p>{item.text.slice(0, 180)}{item.text.length > 180 ? '…' : ''}</p><button className="text-button" onClick={() => { detailSequence.current++; setDetailLoading(false); setSource(item); }}>查看提及原文</button></article>)}</section>}<div className="evidence-list">{evidence.map(item => <article key={item.id} className={`evidence-card stance-${item.stance}`}><div><span className="badge">{stances[item.stance]}</span><span>{item.object_text || '未确定对象'} · {item.dimension}</span></div><blockquote>{item.quote}</blockquote>{item.citation_kind === 'image' && <p className="image-observation">原图第 {item.image_index} 张 · 图片观察待核验{item.image_observation && <span>{item.image_observation.text}</span>}</p>}<p>{natures[item.nature]} · {linked[item.link_status]}</p><div className="evidence-footer"><span>{item.model_id || '模型未知'} · {item.schema_version}</span><button className="text-button" onClick={() => { void showSource(item); }}>查看原文</button></div></article>)}</div>{!detailLoading && !evidence.length && <p className="discovery-note">本候选尚无可显示的评价证据，不能据此声称高置信推荐。</p>}</>}</DiscoveryDialog>}
    {confirmDirection && <DiscoveryDialog title={`采用“${confirmDirection.name}”方向？`} onClose={() => setConfirmDirection(null)}><p>将目的地明确设为“{confirmDirection.city}”，保存新条件版本。其它方向会保留为旧条件记录；此操作不会生成地图或行程时间表。</p><div className="modal-actions"><button onClick={() => setConfirmDirection(null)}>继续比较</button><button className="primary" disabled={busy} onClick={() => { void session.adoptDirection(confirmDirection); setConfirmDirection(null); }}>确认采用并保存</button></div></DiscoveryDialog>}
    {confirmContinue && selected && <DiscoveryDialog title="继续一轮攻略发现？" onClose={() => setConfirmContinue(false)}><p>按当前行程条件创建新任务，目标 {discoveryBudget.target_notes} 篇笔记、每篇最多分析 {discoveryBudget.comment_limit} 条一级评论。新增最多 {discoveryBudget.max_searches} 次搜索、{discoveryBudget.max_model_calls} 次模型调用与 {discoveryBudget.max_images} 次图片提交，费用仍待服务结算。旧任务用量与停止原因会保留，不重置旧预算。</p><div className="modal-actions"><button onClick={() => setConfirmContinue(false)}>暂不继续</button><button className="primary" disabled={busy} onClick={() => { void session.startDiscovery(selected.id); setConfirmContinue(false); }}>确认新增预算并继续</button></div></DiscoveryDialog>}
    {confirmMatch && <DiscoveryDialog title={`确认“${confirmMatch.candidate.name}”的具体地点`} onClose={() => setConfirmMatch(null)}><p>对照来源名称、城市和地址选择分店。尚有歧义时保留候选，不将其绘成正式坐标点。</p><div className="poi-options">{confirmMatch.match.options.map(poi => <button key={poi.id} aria-pressed={chosenPoi?.id === poi.id} disabled={places.busy} onClick={() => setChosenPoi(poi)}><strong>{poi.name || `名称未返回（ID ${poi.id}）`}</strong><small>{poi.city} · {poi.address || '地址未知'}</small><small>GCJ-02 {poi.location.join(', ')} · POI {poi.id}</small></button>)}</div>{chosenPoi && <p>确认对应：{chosenPoi.name || `名称未返回（ID ${chosenPoi.id}）`}，{chosenPoi.address || '地址未知'}。景区中心点不代表入口。</p>}{places.error && <p className="inline-error" role="alert">{places.error}</p>}<div className="modal-actions"><button onClick={() => setConfirmMatch(null)}>保留歧义</button><button className="primary" disabled={!chosenPoi || !canChangePlaces} onClick={() => { if (chosenPoi) void session.confirmPlace(confirmMatch.candidate.id, chosenPoi.id).then(saved => { if (saved) setConfirmMatch(null); }); }}>确认对应地点并保存</button></div></DiscoveryDialog>}
  </div>;
}

function BudgetEditor({ budget, disabled, onChange }: { budget: DiscoveryBudget; disabled: boolean; onChange: (budget: DiscoveryBudget) => void }) {
  return <details className="discovery-budget"><summary>本轮预算：目标 {budget.target_notes} 篇 · 每篇 {budget.comment_limit} 条一级评论</summary><fieldset disabled={disabled}><legend className="sr-only">发现任务预算</legend><div className="budget-fields"><label>目标笔记数<input type="number" min={1} max={60} value={budget.target_notes} onChange={event => { const value = event.target.valueAsNumber; if (Number.isInteger(value) && value >= 1 && value <= 60) onChange({ ...budget, target_notes: value }); }} /></label><label>每篇一级评论上限<input type="number" min={0} max={50} value={budget.comment_limit} onChange={event => { const value = event.target.valueAsNumber; if (Number.isInteger(value) && value >= 0 && value <= 50) onChange({ ...budget, comment_limit: value }); }} /></label></div><label className="reply-switch"><input type="checkbox" checked={budget.include_replies} onChange={event => onChange({ ...budget, include_replies: event.target.checked })} />分析二级回复（本轮总上限 {budget.reply_limit} 条）</label></fieldset><p>最多 {budget.max_searches} 次搜索，其中预留 {budget.reserved_gap_searches} 次用于吃住补查；最长 {Math.round(budget.max_seconds / 60)} 分钟。目标是上限，不保证取得足量来源。</p></details>;
}
function Coverage({ run }: { run: Run }) {
  const result = runResult(run);
  const coverage = { ...result?.coverage, ...run.usage };
  const partialReasons = [...new Set([...(result?.partial_reasons ?? []), ...(coverage.partial_reasons ?? [])])];
  const cumulative = run.trip_usage ?? result?.trip_usage;
  const unjudged = coverage.comments_obtained == null || coverage.comments_judged == null ? undefined : Math.max(0, coverage.comments_obtained - coverage.comments_judged);
  const reusedUnjudged = coverage.comments_reused == null || coverage.comments_reused_judged == null ? undefined : Math.max(0, coverage.comments_reused - coverage.comments_reused_judged);
  return <section className="coverage" aria-label="实际采集与分析覆盖">
    <div><span>本轮新增笔记 / 目标</span><strong>{count(coverage.notes_obtained)} / {coverage.targets?.notes ?? run.budget.target_notes ?? '未知'}</strong></div>
    <div><span>已分析 / 读取失败笔记</span><strong>{count(coverage.notes_analyzed)} / {count(coverage.notes_failed)}</strong></div>
    <div><span>取得 / 已分析评论</span><strong>{count(coverage.comments_obtained)} / {count(coverage.comments_analyzed)}</strong></div>
    <div><span>已判 / 未判评论</span><strong>{count(coverage.comments_judged)} / {count(unjudged)}</strong></div>
    <div><span>模型判断条目</span><strong>{count(coverage.judgement_rows)}</strong></div>
    {typeof coverage.activities_rejected === 'number' && <div><span>引用校验未通过活动</span><strong>{count(coverage.activities_rejected)}</strong></div>}
    <div><span>取得图片 / 提交次数</span><strong>{count(coverage.images_obtained)} / {count(run.usage.images)}</strong></div>
    <div><span>已分析 / 未分析图片（去重）</span><strong>{count(coverage.images_analyzed_unique)} / {count(coverage.images_unanalyzed)}</strong></div>
    <div><span>沿用笔记：已分析 / 总数</span><strong>{count(coverage.notes_reused_analyzed)} / {count(coverage.notes_reused)}</strong></div>
    <div><span>沿用评论：已分析 / 总数</span><strong>{count(coverage.comments_reused_analyzed)} / {count(coverage.comments_reused)}</strong></div>
    <div><span>沿用评论：已判 / 未判</span><strong>{count(coverage.comments_reused_judged)} / {count(reusedUnjudged)}</strong></div>
    <div><span>沿用图片：已分析 / 总数 / 未分析（去重）</span><strong>{count(coverage.images_reused_analyzed_unique)} / {count(coverage.images_reused_obtained)} / {count(coverage.images_reused_unanalyzed)}</strong></div>
    <p>新增来源的采集与分析单独计数；沿用来源的分析覆盖另列。本轮首次分析旧来源也显示在沿用覆盖中，沿用数量不计作新增采集或收费次数。历史记录未保存的计数保持未知。</p>
    <p>本轮外部调用 {count(run.usage.external_calls)} · 搜索 {count(run.usage.searches)} · 模型调用 {count(run.usage.model_calls)} · 已知模型 token {count(run.usage.model_tokens)} · 未结清预留 token {count(run.usage.reserved_tokens)} · 费用未核定{run.usage.unknown_cost_calls ? ` · ${run.usage.unknown_cost_calls} 次调用用量未知` : ''}。token 是用量，不是金额。</p>
    <p>评论取得、送交分析与判断分别计数；本轮新增来源选取评论 {count(coverage.comments_selected)}。图片提交次数含重试和旧来源的新提交；去重覆盖按来源与原图序号计数，视频未分析。</p>
    {run.usage.unknown_result_calls ? <p>有 {run.usage.unknown_result_calls} 次外部调用结果未知，可能已经执行。</p> : null}
    {cumulative && <p>本行程累计：外部调用 {count(cumulative.external_calls)} · token {count(cumulative.model_tokens)} · 图片提交 {count(cumulative.images)} 次 · {cumulative.unknown_cost_calls} 次用量不完整 · 费用{cumulative.cost_cny === null ? '未知' : `${cumulative.cost_cny} 元`}。</p>}
    {run.usage.interrupted_stage_time_unknown && <p>服务中断阶段耗时未知；已确认用量仍保留。</p>}
    {partialReasons.length > 0 && <p>未完成项：{partialReasons.map(reason => pauseReasons[reason] ?? reason).join('；')}</p>}
    {result?.model?.semantic_quality === 'not_validated' && <p>模型语义质量未验证，候选与评价请结合原文核对。</p>}
  </section>;
}
function UnderstandingView({ understanding }: { understanding: Understanding }) {
  const fields = Object.keys(fieldLabels).filter(name => name !== 'theme') as (keyof Understanding['intent'])[];
  return <details className="understanding"><summary>本轮如何理解需求</summary><p>{understanding.intent.destination_mode === 'explicit' ? '按用户明确的目的地发现' : '目的地未确认，保留多个探索方向'}</p><dl>{fields.map(name => { const item = understanding.intent[name] as InterpretedValue; return <div key={name}><dt>{fieldLabels[name]}</dt><dd>{item.value === null ? '未知' : String(item.value)}<span className={`badge provenance-${item.provenance}`}>{provenance[item.provenance]}</span>{item.evidence && <small>用户原文：{item.evidence}</small>}</dd></div>; })}</dl>{understanding.intent.themes.length > 0 && <p>主题：{understanding.intent.themes.join('、')}</p>}{understanding.intent.exclusions.length > 0 && <p>排除：{understanding.intent.exclusions.join('、')}</p>}{understanding.intent.must_visit.length > 0 && <p>必去线索：{understanding.intent.must_visit.join('、')}</p>}{understanding.intent.missing_for_scheduling.length > 0 && <p>排程待确认：{understanding.intent.missing_for_scheduling.map(name => fieldLabels[name]).join('、')}</p>}<p>有限查询计划：</p><ol>{understanding.query_plan.map((query, index) => <li key={index}>{query.query}{query.direction_hint && <span> · 暂定方向：{query.direction_hint}</span>}</li>)}</ol><p>模型推测和暂定搜索方向不会自动写入行程条件；采用有来源的方向需要你明确确认。</p></details>;
}
function CandidateCard({ candidate, match, historical, canAdopt, mapVisible, selected, focused, onEvidence, onAdopt, onConfirm, onMap, onSelect }: { candidate: Candidate; match?: PlaceMatch; historical: boolean; canAdopt: boolean; mapVisible: boolean; selected: boolean; focused: boolean; onEvidence: () => void; onAdopt: () => void; onConfirm: () => void; onMap: () => void; onSelect: () => void }) {
  const located = candidate.kind === 'place' && match?.status === 'unique' && Boolean(match.poi);
  const badge = historical ? '旧条件' : candidate.kind === 'generic' ? '泛称线索' : match?.status === 'unique' ? match.confirmed ? '人工确认地点' : '唯一匹配' : match?.status === 'ambiguous' ? '地点有歧义' : match ? '未匹配' : '待定位';
  return <article className={`candidate-card ${candidate.kind === 'direction' ? 'direction-card' : ''}${focused ? ' focused-candidate' : ''}`}><div className="candidate-title"><div><span className="eyebrow">{candidate.kind === 'direction' ? '探索方向' : categories[candidate.category]}</span><h4>{located && mapVisible ? <button className="candidate-name" onClick={onMap}>{candidate.name}</button> : candidate.name}</h4></div><span className="badge">{badge}</span></div><p className="candidate-location">{candidate.city || '城市线索未知'}{candidate.direction && candidate.direction !== candidate.city ? ` · ${candidate.direction}` : ''}</p>{candidate.summary && <p className="candidate-summary">{candidate.summary}</p>}<div className="candidate-stats"><span>不同作者 {candidate.stats.author_count}</span><span>正面 {candidate.stats.positive_count}</span><span>负面 {candidate.stats.negative_count}</span><span>不确定 {candidate.stats.uncertain_count}</span></div>{candidate.uncertainty.length > 0 && <p className="candidate-uncertainty">待核实：{candidate.uncertainty.join('；')}</p>}{candidate.ranking_reasons?.length ? <p className="candidate-location">排序依据：{candidate.ranking_reasons.join('；')}</p> : null}{match && <div className="poi-match"><p>{match.reason}</p>{match.poi && <><p><strong>{match.poi.name || `名称未返回（ID ${match.poi.id}）`}</strong> · {match.poi.city} · {match.poi.address || '地址未知'}</p><small>高德 GCJ-02 {match.poi.location.join(', ')} · {match.poi.api_version} · {new Date(match.poi.read_at).toLocaleString('zh-CN')}</small><p>{match.poi.entrance_location ? '有入口坐标，进出距离仍待路线核实。' : '入口位置待核实，中心点不代表入口，进出距离待核实。'}{match.poi.missing_fields.length ? ` 字段缺失：${match.poi.missing_fields.join('、')}` : ''}</p></>}</div>}<div className="candidate-actions"><button onClick={onEvidence}>查看来源与证据</button>{candidate.kind === 'direction' && candidate.city && <button className="primary" disabled={!canAdopt} onClick={onAdopt}>采用这个方向</button>}{candidate.kind === 'place' && match?.status === 'ambiguous' && <button disabled={!canAdopt} onClick={onConfirm}>确认具体地点</button>}{located && <><button disabled={!canAdopt} onClick={onMap}>{mapVisible ? '定位并高亮地图点' : '加入地图'}</button><button aria-pressed={selected} disabled={!canAdopt} onClick={onSelect}>{selected ? '移出行程选择' : '加入行程'}</button></>}</div>{located && <p className="candidate-location">地图{mapVisible ? '已展示' : '未展示'} · 行程{selected ? '已选择' : '未选择'} · 尚未生成路线与时间表</p>}</article>;
}
function SourceView({ source, back }: { source: Source; back: () => void }) {
  return <article className="source-view">
    <button className="text-button" onClick={back}>← 返回证据</button>
    <h3>{source.title || (source.kind === 'comment' ? '评论原文' : '笔记原文')}</h3>
    <p>{source.kind === 'comment' ? '评论' : '笔记'} · {source.read_status === 'success' ? '读取成功' : '读取失败'} · {source.completeness === 'complete' ? '文本读取完整' : source.completeness === 'partial' ? '部分内容' : '完整性未知'}</p>
    {source.published_at && <p>来源时间：{new Date(source.published_at).toLocaleString('zh-CN')}</p>}
    <blockquote>{source.text || '尚无可读取的正文。'}</blockquote>
    <p>原图片序号共 {source.images.length} 项，包含不可用占位；不代表这些图片或视频已经全部理解。</p>
    {source.images.length > 0 && <ol className="source-images" aria-label="原图片序号">{source.images.map((value, index) => {
      const url = originalImageUrl(value);
      return <li key={index}>{url ? <a href={url} target="_blank" rel="noreferrer">原图第 {index + 1} 张（外部链接）</a> : <span>原图第 {index + 1} 张：图片链接不可用</span>}</li>;
    })}</ol>}
    {source.dedupe_confidence === 'low' && <p>来源去重标识置信度低，覆盖统计应谨慎解释。</p>}
    <p>本轮读取：{new Date(source.read_at ?? source.last_read_at).toLocaleString('zh-CN')}</p>
    {source.queries?.length ? <p>发现查询：{source.queries.map(item => item.query).join('；')}</p> : null}
    <small>来源 ID：{source.id}{source.parent_source_id ? ` · 父笔记：${source.parent_source_id}` : ''}</small>
  </article>;
}
function originalImageUrl(value: string) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}
function DiscoveryDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const element = ref.current; element?.showModal(); return () => { element?.close(); }; }, []);
  return <dialog ref={ref} className="modal discovery-dialog" aria-labelledby="discovery-dialog-title" onCancel={event => { event.preventDefault(); onClose(); }}><div className="modal-title"><h2 id="discovery-dialog-title">{title}</h2><button className="icon-button" aria-label="关闭来源对话框" onClick={onClose}>×</button></div>{children}</dialog>;
}
