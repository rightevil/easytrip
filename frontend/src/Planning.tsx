import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import { defaultPlanning, defaultPlanningBudget, newConstraint } from './planning-types';
import type { ActivityConstraint, ItineraryItem, Plan, PlanningConstraints, PlanningInput } from './planning-types';
import type { TripSession } from './session';
import { AMapCanvas } from './AMapCanvas';
import type { MapPoint } from './AMapCanvas';

const provenanceLabels = { user: '用户确认', source: '来源报告（待核当前有效性）', estimated: '程序估计', unknown: '未知' };
const modes: [PlanningConstraints['disabled_modes'][number], string][] = [['walking', '步行'], ['transit', '公交地铁'], ['bicycling', '自行车'], ['electrobike', '电动车'], ['driving', '驾车']];
const toLocal = (value: string | null) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
const toIso = (value: string) => value ? new Date(value).toISOString() : null;
const modeLabels = Object.fromEntries(modes);
const statusLabels = { draft: '待补资料的草案', conditional: '有条件草案', partial: '部分排程', conflict: '硬约束冲突', validated: '已校验方案' };
const runLabels = { pending: '等待规划', running: '规划中', paused: '已暂停', recoverable: '重启后可恢复', completed: '规划结束', partial: '部分完成', failed: '规划失败', cancelled: '已取消', stale: '旧条件记录' };
const stageLabels: Record<string, string> = { planning_input: '冻结规划输入', routing: '核对高德交通', scheduled: '校验单日排程', done: '完成' };
const time = (value: string | null, timezone: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: timezone, month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '待排定';
const amount = (value: number | null) => value === null ? '费用未知' : `¥${value.toLocaleString('zh-CN')}`;
const sourceLabel = (value: string) => value === 'amap' ? '高德交通响应' : provenanceLabels[value as keyof typeof provenanceLabels] ?? '来源未知';
const active = (status: string) => status === 'pending' || status === 'running';

export function Planning({ session, archived }: { session: TripSession; archived: boolean }) {
  const request = useSyncExternalStore(session.request.subscribe, session.request.getSnapshot);
  const workspace = useSyncExternalStore(session.workspace.subscribe, session.workspace.getSnapshot);
  const tasks = useSyncExternalStore(session.subscribeRuns, session.getRunSnapshot);
  const [input, setInput] = useState<PlanningInput | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [viewId, setViewId] = useState<string | null>(null);
  const [compareId, setCompareId] = useState<string>('');
  const [now, setNow] = useState(Date.now);
  const planning = request.value.request.planning ?? defaultPlanning;
  const selection = workspace.value.selected_ids.join('|');
  const origin = JSON.stringify(workspace.value.confirmed_origin ?? null);
  const planningRuns = tasks.runs.filter(run => run.kind === 'plan');
  const settledRuns = planningRuns.filter(run => !active(run.status)).map(run => `${run.id}:${run.generation}:${run.updated_at}`).join('|');
  const adoptedId = session.trip.active_plan_id ?? null;
  useEffect(() => { setInput(null); setPlans([]); setViewId(null); setCompareId(''); }, [session]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    let current = true;
    setReading(true); setError(null);
    void Promise.all([api.planningInput(session.trip.id), api.plans(session.trip.id)]).then(([value, history]) => {
      if (value.trip_id && value.trip_id !== session.trip.id || history.items.some(plan => plan.trip_id !== session.trip.id)) throw new Error('规划记录归属不一致，已阻止展示。');
      if (current && value.request_version === session.request.getSnapshot().version) { setInput(value); setPlans(history.items); }
    }).catch(reason => { if (current) setError(reason instanceof Error ? reason.message : '规划输入读取失败'); }).finally(() => { if (current) setReading(false); });
    return () => { current = false; };
  }, [session, request.version, workspace.version, selection, origin, refresh, settledRuns, adoptedId]);
  const reread = async () => {
    try { await session.flush(); setRefresh(value => value + 1); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '请先完成保存。'); }
  };
  const chosen = plans.find(plan => plan.id === viewId) ?? plans.find(plan => plan.id === input?.active_plan_id) ?? plans[0];
  const compared = plans.find(plan => plan.id === compareId);
  const clean = request.status === 'saved' && workspace.status === 'saved';
  const current = Boolean(chosen && input && chosen.input_hash === input.input_hash && chosen.request_version === request.version && clean);
  const fresh = Boolean(chosen?.payload.valid_until && Date.parse(chosen.payload.valid_until) > now);
  const canAdopt = Boolean(chosen && current && fresh && ['validated', 'conditional'].includes(chosen.status) && chosen.payload.itinerary.length && !chosen.payload.conflicts.length && !chosen.payload.input.missing.length && !input?.missing.length);
  const adopt = async (plan: Plan) => { if (await session.adoptPlan(plan)) setRefresh(value => value + 1); };
  const update = (changes: Partial<PlanningConstraints>) => {
    const draft = session.request.getSnapshot().value;
    session.request.update({ ...draft, request: { ...draft.request, planning: { ...(draft.request.planning ?? defaultPlanning), ...changes } } });
  };
  const change = (id: string, changes: Partial<ActivityConstraint>) => {
    const values = (session.request.getSnapshot().value.request.planning ?? defaultPlanning).activity_constraints;
    const previous = values.find(item => item.candidate_id === id) ?? newConstraint(id);
    update({ activity_constraints: [...values.filter(item => item.candidate_id !== id), { ...previous, ...changes }] });
  };
  return <section className="planning-workspace" aria-label="单日规划工作区">
    <div className="section-subheading"><h3>单日规划条件</h3><button disabled={reading || tasks.busy} onClick={() => { void reread(); }}>重新读取规划输入</button></div>
    <p className="discovery-note">时长、预约和费用手填值记为用户确认；空值保持未知。P2 推荐语义仍未通过验收，地点确认不代表营业或费用已核实。</p>
    {reading && <p role="status">正在读取已保存的规划输入…</p>}
    {error && <p role="alert" className="inline-error">{error}</p>}
    {request.status !== 'saved' && <p role="status">规划条件{request.status === 'error' || request.status === 'conflict' ? '尚未保存，请先处理保存提示' : '正在保存'}；已保存快照不会覆盖当前编辑。</p>}
    {input?.missing.length ? <div className="planning-issues"><strong>生成前需要补齐</strong><ul>{input.missing.map((item, i) => <li key={`${item.code}-${i}`}>{item.message}</li>)}</ul></div> : input && <p>起点、日期和已选地点已保存；规划会按这些条件核对交通与硬约束。</p>}
    <fieldset disabled={archived || tasks.busy}><legend>交通与返程约束</legend><div className="planning-fields"><label>起点所在城市（地图点选请明确填写）<input maxLength={200} value={planning.origin_city ?? input?.origin?.city ?? ""} onChange={event => update({ origin_city: event.target.value.trim() || null })} /></label><label><input type="checkbox" checked={planning.return_to_origin} onChange={event => update({ return_to_origin: event.target.checked, end_candidate_id: null })} /> 计划返回出发点</label><label>指定终点<select disabled={planning.return_to_origin} value={planning.end_candidate_id ?? ''} onChange={event => update({ end_candidate_id: event.target.value || null })}><option value="">不指定，最后一个活动结束</option>{input?.selected.map(place => <option key={place.candidate_id} value={place.candidate_id}>{place.name} · {place.poi.name}</option>)}</select></label><label>结束期限（本机时区输入）<input type="datetime-local" value={toLocal(planning.end_at)} onChange={event => update({ end_at: toIso(event.target.value) })} /></label><label>累计步行上限（米）<input type="number" min="0" max="100000" value={planning.max_walk_meters ?? ''} onChange={event => update({ max_walk_meters: event.target.value === '' ? null : Number(event.target.value) })} /></label><label>每段额外缓冲（分钟，程序默认 10）<input type="number" min="0" max="120" value={planning.transfer_buffer_minutes} onChange={event => update({ transfer_buffer_minutes: Number(event.target.value) })} /></label></div><div className="planning-modes">禁用交通：{modes.map(([mode, label]) => <label key={mode}><input type="checkbox" checked={planning.disabled_modes.includes(mode)} onChange={event => update({ disabled_modes: event.target.checked ? [...planning.disabled_modes, mode] : planning.disabled_modes.filter(value => value !== mode) })} /> {label}</label>)}</div></fieldset>
    {input?.selected.map(place => {
      const constraint = planning.activity_constraints.find(item => item.candidate_id === place.candidate_id) ?? newConstraint(place.candidate_id);
      return <fieldset key={place.candidate_id} disabled={archived} className="planning-activity"><legend>{place.name}</legend><p>{place.poi.name} · {place.poi.address || '地址未知'} · {place.activities.length} 项来源活动</p><div className="planning-fields"><label>停留分钟<input type="number" min="1" max="1440" value={constraint.duration_minutes ?? ''} onChange={event => change(place.candidate_id, { duration_minutes: event.target.value === '' ? null : Number(event.target.value), duration_source: event.target.value === '' ? 'unknown' : 'user' })} /></label><label>确认费用（元/同行全组）<input type="number" min="0" step="0.01" value={constraint.cost_cny ?? ''} onChange={event => change(place.candidate_id, { cost_cny: event.target.value === '' ? null : Number(event.target.value), cost_source: event.target.value === '' ? 'unknown' : 'user' })} /></label><label>固定预约（本机时区输入）<input type="datetime-local" value={toLocal(constraint.fixed_start_at)} onChange={event => change(place.candidate_id, { fixed_start_at: toIso(event.target.value) })} /></label><label>可进入时间<input type="datetime-local" value={toLocal(constraint.not_before)} onChange={event => change(place.candidate_id, { not_before: toIso(event.target.value) })} /></label><label>须离开时间<input type="datetime-local" value={toLocal(constraint.not_after)} onChange={event => change(place.candidate_id, { not_after: toIso(event.target.value) })} /></label></div><div className="planning-flags"><label><input type="checkbox" checked={constraint.required} disabled={constraint.excluded || constraint.state === 'skipped'} onChange={event => change(place.candidate_id, { required: event.target.checked })} /> 必去</label><label><input type="checkbox" checked={constraint.excluded} disabled={constraint.required || constraint.locked} onChange={event => change(place.candidate_id, { excluded: event.target.checked })} /> 排除</label><label><input type="checkbox" checked={constraint.locked} disabled={!constraint.locked && (!input.active_plan_id && !constraint.fixed_start_at || constraint.excluded)} onChange={event => change(place.candidate_id, { locked: event.target.checked })} /> 锁定预约</label><span>停留：{provenanceLabels[constraint.duration_source]}；费用：{provenanceLabels[constraint.cost_source]}</span></div></fieldset>;
    })}
    <div className="planning-generation"><button className="primary" disabled={archived || tasks.busy || tasks.runs.some(run => active(run.status)) || Boolean(tasks.operation && tasks.operation.kind !== 'plan')} onClick={() => { void session.startPlanning(input?.active_plan_id ?? undefined); }}>{tasks.operation?.kind === 'plan' ? '重试同一规划任务' : '生成单日方案'}</button><p>本轮最多 {defaultPlanningBudget.max_routes} 次路线请求、{defaultPlanningBudget.max_combinations} 个组合、{defaultPlanningBudget.max_seconds / 60} 分钟。资料不足保留草案，生成不会自动采用。</p></div>
    {tasks.error && <div className="message-error" role="alert"><p>{tasks.error}</p>{tasks.operation?.kind === 'plan' && <><p>已发出的请求可能完成。重试沿用原操作；放弃后重新生成会开始新任务。</p><button disabled={tasks.busy} onClick={() => session.forgetRunOperation()}>明确放弃本次规划重试</button></>}{tasks.planAdoption && <><p>采用请求可能已保存，重试沿用同一操作。</p><button disabled={tasks.busy} onClick={() => session.forgetPlanAdoption()}>明确放弃采用重试</button></>}</div>}
    {planningRuns.length > 0 && <div className="planning-run-list" aria-label="规划任务记录">{planningRuns.map(run => {
      const old = run.request_version !== request.version || run.status === 'stale';
      const exhausted = /budget/.test(run.stop_reason ?? '') || run.usage.elapsed_seconds >= run.budget.max_seconds || (run.usage.route_calls ?? 0) >= (run.budget.max_routes ?? Infinity);
      return <article key={run.id}><strong>{old ? '旧条件记录' : runLabels[run.status]}</strong><span> · {stageLabels[run.stage] ?? '准备规划'} · 条件版本 {run.request_version}</span><p>路线调用 {run.usage.route_calls ?? 0} · 城市核对 {run.usage.city_calls ?? 0} · 缓存命中 {run.usage.cache_hits ?? 0}</p>{run.result && 'plan_ids' in run.result && <p>{run.result.summary}</p>}{run.stop_reason && <p>停止原因：{run.stop_reason}</p>}<div>{active(run.status) && <button disabled={tasks.busy} onClick={() => { void session.controlRun(run.id, 'cancel'); }}>取消规划任务</button>}{!old && !exhausted && !archived && ['paused', 'recoverable', 'cancelled', 'failed', 'partial'].includes(run.status) && run.stage !== 'done' && run.error_code !== 'invalid_run_data' && <button disabled={tasks.busy} onClick={() => { void session.controlRun(run.id, 'resume'); }}>恢复规划任务</button>}</div></article>;
    })}</div>}
    <div className="planning-history"><h4>已保存方案</h4>{plans.length ? <><label>查看方案版本<select value={chosen?.id ?? ''} onChange={event => { setViewId(event.target.value); setCompareId(''); }}>{plans.map(plan => <option key={plan.id} value={plan.id}>方案 {plan.version} · {statusLabels[plan.status]} · 条件版本 {plan.request_version}{plan.id === input?.active_plan_id ? ' · 当前采用' : ''}</option>)}</select></label>{chosen && <><div className="planning-plan-status"><strong>{statusLabels[chosen.status]}{chosen.id === input?.active_plan_id ? ' · 当前采用' : ''}</strong><span>条件版本 {chosen.request_version} · 规划时区 {chosen.payload.input.planning.timezone}</span></div>{!current && <p className="planning-warning">当前条件或编辑与这份冻结方案不同，仅供历史查看。</p>}{!fresh && <p className="planning-warning">路线校验有效期已过或未知，请重新生成。不能据此确认可执行。</p>}{canAdopt && chosen.status === 'validated' && <p>当前条件已校验；交通预测仍需在出发时核对。</p>}{chosen.payload.unknowns.length > 0 && <div className="planning-issues"><strong>仍需确认</strong><ul>{chosen.payload.unknowns.map((issue, index) => <li key={index}>{issue.message}</li>)}</ul></div>}{chosen.payload.conflicts.length > 0 && <div className="planning-issues" role="alert"><strong>硬约束冲突</strong><ul>{chosen.payload.conflicts.map((issue, index) => <li key={index}>{issue.message}</li>)}</ul></div>}<PlanDetails key={chosen.id} plan={chosen} />{['conditional', 'validated'].includes(chosen.status) && chosen.id !== input?.active_plan_id && <button className="primary" disabled={archived || tasks.busy || Boolean(tasks.planAdoption && tasks.planAdoption.planId !== chosen.id) || !canAdopt && tasks.planAdoption?.planId !== chosen.id} onClick={() => { void adopt(chosen); }}>{tasks.planAdoption?.planId === chosen.id ? '重试同一采用请求' : chosen.status === 'conditional' ? '采用有条件草案' : '采用已校验方案'}</button>}<label>只读比较另一版本<select value={compareId} onChange={event => setCompareId(event.target.value)}><option value="">不比较</option>{plans.filter(plan => plan.id !== chosen.id).map(plan => <option key={plan.id} value={plan.id}>方案 {plan.version} · 条件版本 {plan.request_version}</option>)}</select></label>{compared && <div className="planning-comparison" aria-label="方案版本比较"><div><h5>方案 {chosen.version}</h5><PlanSummary plan={chosen} /></div><div><h5>方案 {compared.version}</h5><PlanSummary plan={compared} /></div></div>}</>}</> : <p>尚无方案。保存条件与已选地点后，生成真实交通与单日排程草案。</p>}</div>
  </section>;
}

function itineraryName(item: ItineraryItem) {
  return item.kind === 'activity' ? item.name : item.kind === 'route' ? `${modeLabels[item.leg.mode] ?? item.leg.mode}交通` : '等待与缓冲';
}
function PlanSummary({ plan }: { plan: Plan }) {
  const timezone = plan.payload.input.planning.timezone;
  return <><p>{statusLabels[plan.status]} · 条件版本 {plan.request_version}</p><FrozenConditions input={plan.payload.input} /><ol>{plan.payload.itinerary.map((item, index) => <li key={index}>{itineraryName(item)} · {time(item.start_at, timezone)} → {time(item.end_at, timezone)}</li>)}</ol><p>{plan.payload.unknowns.map(issue => issue.message).join('；')}</p><p>{plan.payload.conflicts.map(issue => issue.message).join('；')}</p>{plan.payload.diff.map((item, index) => typeof item.message === 'string' ? <p key={index}>{item.message}</p> : null)}</>;
}
function FrozenConditions({ input }: { input: PlanningInput }) {
  const { request, planning } = input;
  return <details className="planning-frozen-conditions"><summary>冻结方案条件（只读）</summary><p>从 {input.origin?.name ?? '起点未确认'} 于 {time(request.start_at, planning.timezone)} 出发 · 可用 {request.duration_minutes ?? '未知'} 分钟 · {request.transport === 'mixed' ? '灵活组合' : modeLabels[request.transport]} · 同行 {request.participants} 人 · {request.budget_cny === null ? '预算未设上限' : `总预算 ¥${request.budget_cny}`}</p><p>终点：{planning.return_to_origin ? '返回出发点' : input.selected.find(place => place.candidate_id === planning.end_candidate_id)?.name ?? '最后一个活动结束'} · 结束期限 {time(planning.end_at, planning.timezone)} · 步行上限 {planning.max_walk_meters ?? '未设'} 米 · 每段缓冲 {planning.transfer_buffer_minutes} 分钟</p><p>禁用：{planning.disabled_modes.map(mode => modeLabels[mode]).join('、') || '无'} · 偏好：{request.preferences || '未填写'}</p>{planning.activity_constraints.map(item => <p key={item.candidate_id}>{input.selected.find(place => place.candidate_id === item.candidate_id)?.name ?? '已选项目'} · 停留 {item.duration_minutes ?? '未知'} 分钟（{sourceLabel(item.duration_source)}） · {amount(item.cost_cny)}（{sourceLabel(item.cost_source)}）{item.required ? ' · 必去' : ''}{item.excluded ? ' · 排除' : ''}{item.locked ? ' · 锁定' : ''}{item.fixed_start_at ? ` · 固定预约 ${time(item.fixed_start_at, planning.timezone)}` : ''}{item.not_before ? ` · 可进入 ${time(item.not_before, planning.timezone)}` : ''}{item.not_after ? ` · 须离开 ${time(item.not_after, planning.timezone)}` : ''}</p>)}</details>;
}
function PlanDetails({ plan }: { plan: Plan }) {
  const timezone = plan.payload.input.planning.timezone;
  const routes = useMemo(() => plan.payload.itinerary.filter(item => item.kind === 'route').map(item => item.leg), [plan]);
  const points = useMemo(() => {
    const result: MapPoint[] = plan.payload.input.selected.map(place => ({ id: place.poi.id, name: place.poi.name ?? place.name, location: place.poi.location }));
    const origin = plan.payload.input.origin;
    if (origin) result.unshift({ id: 'plan-origin', name: origin.name, location: origin.location, origin: true });
    return result;
  }, [plan]);
  const [center, setCenter] = useState<[number, number] | null>(null);
  const [zoom, setZoom] = useState(12);
  const [focus, setFocus] = useState<{ id: string; sequence: number } | null>(null);
  useEffect(() => { setCenter(null); setZoom(12); setFocus(null); }, [plan.id]);
  return <div className="planning-plan-details"><FrozenConditions input={plan.payload.input} /><ol className="planning-timeline" aria-label={`方案 ${plan.version} 时间表`}>{plan.payload.itinerary.map((item, index) => <li key={index} className={`planning-item-${item.kind}`}><div className="planning-time">{time(item.start_at, timezone)} → {time(item.end_at, timezone)}</div><strong>{itineraryName(item)}</strong>{item.kind === 'activity' ? <><p>{item.poi.name} · 停留 {item.duration_minutes ?? '未知'} 分钟（{sourceLabel(item.duration_source)}） · {amount(item.cost_cny)}（{sourceLabel(item.cost_source)}）</p><p>{item.required ? '必去 · ' : ''}{item.locked ? '锁定 · ' : ''}{item.state === 'completed' ? '已完成' : item.state === 'skipped' ? '已跳过' : '待进行'}</p></> : item.kind === 'route' ? <><p>预计 {item.leg.duration_seconds === null ? '时长未知' : `${Math.ceil(item.leg.duration_seconds / 60)} 分钟`}（{sourceLabel(item.leg.duration_source)}） · 累计本段步行 {item.leg.walking_distance_meters ?? '未知'} 米 · {amount(item.leg.cost_cny)}（{sourceLabel(item.leg.cost_source)}）</p><p>高德 {item.leg.api_version} · 读取 {time(item.leg.read_at, timezone)} · 有效至 {time(item.leg.valid_until, timezone)}</p>{item.leg.conditions.length > 0 && <p>{item.leg.conditions.join('；')}</p>}{(!item.leg.geometry_complete || !item.leg.geometry.length) && <p className="planning-warning">路线形状缺失或不完整，地图仅绘制高德返回的独立线段。</p>}</> : <p>{item.reason}</p>}</li>)}</ol>{routes.length > 0 && <div className="planning-route-map"><h5>此方案的真实交通路线</h5><p>按交通方式着色：步行绿色、公交地铁蓝色、自行车黄色、电动车棕色、驾车紫色。缺失线段不补直线，其他方案不叠加。</p><AMapCanvas points={points} routes={routes} center={center} zoom={zoom} focus={focus} picking={false} onViewport={(value, level) => { setCenter(value); setZoom(level); }} onPick={() => {}} onSelect={id => setFocus(value => ({ id, sequence: (value?.sequence ?? 0) + 1 }))} /></div>}{plan.payload.diff.length > 0 && <details><summary>相对父版本的已保存差异</summary>{plan.payload.diff.map((item, index) => typeof item.message === 'string' ? <p key={index}>{item.message}</p> : null)}</details>}</div>;
}
