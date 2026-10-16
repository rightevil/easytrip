import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import { defaultPlanning, defaultPlanningBudget, newConstraint } from './planning-types';
import type { ActivityConstraint, DayPlan, ItineraryItem, LodgingSupplement, MealSupplement, Plan, PlanningConstraints, PlanningInput, PlanningIssue, PlanningReplacements, RailSegment } from './planning-types';
import { MultiDayEditor } from './MultiDayEditor';
import { RailEditor } from './RailEditor';
import { LodgingSuggestions } from './LodgingSuggestions';
import type { LodgingSelection } from './LodgingSuggestions';
import type { TripSession } from './session';
import { AMapCanvas } from './AMapCanvas';
import type { MapPoint } from './AMapCanvas';
import type { Coordinate } from './amap';
import { validCoordinate } from './amap';

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
const progressDraft = (planning: PlanningConstraints) => ({ name: planning.progress_origin?.name ?? '', city: planning.progress_origin?.city ?? '', longitude: planning.progress_origin?.location[0].toString() ?? '', latitude: planning.progress_origin?.location[1].toString() ?? '', at: toLocal(planning.progress_at ?? null) });
const issueText = (issue: PlanningIssue, timezone: string) => {
  const period = issue.period ?? issue.meal_window?.period;
  const start = issue.window_start ?? issue.meal_window?.start_at;
  const end = issue.window_end ?? issue.meal_window?.end_at;
  return `${issue.date ? `${issue.date} · ` : ''}${issue.message}${period && start && end ? ` · ${period === 'lunch' ? '午餐' : '晚餐'} ${time(start, timezone)} → ${time(end, timezone)}` : ''}${issue.anchor?.name ? ` · ${issue.anchor.city ?? ''} ${issue.anchor.name}附近` : ''}${issue.relaxations?.length ? ` · 可调整：${issue.relaxations.join('、')}` : ''}`;
};

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
  const [dietary, setDietary] = useState(() => (planning.dietary_restrictions ?? []).join('\n'));
  const dietaryEntries = dietary.split('\n').map(value => value.trim()).filter(Boolean);
  const dietaryInvalid = dietaryEntries.length > 20 || dietaryEntries.some(value => value.length > 200);
  const [progress, setProgress] = useState(() => progressDraft(planning));
  const [progressPicking, setProgressPicking] = useState(false);
  const [progressError, setProgressError] = useState<string | null>(null);
  const [replacementPlaces, setReplacementPlaces] = useState<PlanningReplacements | null>(null);
  const [replacementId, setReplacementId] = useState<Record<string, string>>({});
  const [replacementReading, setReplacementReading] = useState(false);
  const currentSession = useRef(session);
  currentSession.current = session;
  const replacementSequence = useRef(0);
  const selection = workspace.value.selected_ids.join('|');
  const origin = JSON.stringify(workspace.value.confirmed_origin ?? null);
  const planningRuns = tasks.runs.filter(run => run.kind === 'plan');
  const settledRuns = planningRuns.filter(run => !active(run.status)).map(run => `${run.id}:${run.generation}:${run.updated_at}`).join('|');
  const adoptedId = session.trip.active_plan_id ?? null;
  useEffect(() => { setInput(null); setPlans([]); setViewId(null); setCompareId(''); setDietary((session.request.getSnapshot().value.request.planning?.dietary_restrictions ?? []).join('\n')); setProgress(progressDraft(session.request.getSnapshot().value.request.planning ?? defaultPlanning)); setProgressPicking(false); setProgressError(null); setReplacementPlaces(null); setReplacementId({}); setReplacementReading(false); return () => { replacementSequence.current++; }; }, [session]);
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
    try { await session.flush(); if (currentSession.current === session) setRefresh(value => value + 1); }
    catch (reason) { if (currentSession.current === session) setError(reason instanceof Error ? reason.message : '请先完成保存。'); }
  };
  const chosen = plans.find(plan => plan.id === viewId) ?? plans.find(plan => plan.id === input?.active_plan_id) ?? plans[0];
  const adopted = plans.find(plan => plan.id === input?.active_plan_id);
  const adoptedMeals = adopted?.payload.supplements?.filter((meal): meal is MealSupplement => 'meal_window' in meal && adopted.payload.itinerary.some(item => item.kind === 'activity' && item.candidate_id === meal.candidate_id && item.state !== 'skipped')) ?? [];
  const editablePlaces = [...new Map([...(input?.selected ?? []), ...adoptedMeals].map(place => [place.candidate_id, place])).values()];
  const compared = plans.find(plan => plan.id === compareId);
  const clean = request.status === 'saved' && workspace.status === 'saved';
  const current = Boolean(chosen && input && chosen.input_hash === input.input_hash && chosen.request_version === request.version && clean);
  const fresh = Boolean(chosen?.payload.valid_until && Date.parse(chosen.payload.valid_until) > now);
  const canAdopt = Boolean(chosen && current && fresh && ['validated', 'conditional'].includes(chosen.status) && chosen.payload.itinerary.length && !chosen.payload.conflicts.length && !chosen.payload.input.missing.length && !input?.missing.length);
  const adopt = async (plan: Plan) => { if (await session.adoptPlan(plan) && currentSession.current === session) setRefresh(value => value + 1); };
  const update = (changes: Partial<PlanningConstraints>) => {
    const draft = session.request.getSnapshot().value;
    session.request.update({ ...draft, request: { ...draft.request, planning: { ...(draft.request.planning ?? defaultPlanning), ...changes } } });
  };
  const searchRail = async (rail: RailSegment) => {
    await session.flush();
    const snapshot = session.request.getSnapshot();
    const matches = () => currentSession.current === session && session.trip.status === 'draft' && session.request.getSnapshot().value.request.planning?.rail_segments?.some(value => value.id === rail.id && value.date === rail.date && value.departure_station_id === rail.departure_station_id && value.arrival_station_id === rail.arrival_station_id);
    if (snapshot.status !== 'saved' || !matches() || !rail.departure_station_id || !rail.arrival_station_id) throw new Error('行程或铁路条件已变化，请重新查询。');
    const value = await api.searchRail(session.trip.id, { expected_version: snapshot.version, date: rail.date, departure_station_id: rail.departure_station_id, arrival_station_id: rail.arrival_station_id });
    const latest = session.request.getSnapshot();
    if (latest.status !== 'saved' || latest.version !== snapshot.version || !matches() || value.trip_id !== session.trip.id || value.request_version !== snapshot.version || value.date !== rail.date || value.departure_station_id !== rail.departure_station_id || value.arrival_station_id !== rail.arrival_station_id) throw new Error('行程或铁路条件已变化，请重新查询。');
    return value;
  };
  const change = (id: string, changes: Partial<ActivityConstraint>) => {
    const values = (session.request.getSnapshot().value.request.planning ?? defaultPlanning).activity_constraints;
    const previous = values.find(item => item.candidate_id === id) ?? { ...newConstraint(id), ...adoptedMeals.find(item => item.candidate_id === id)?.constraint };
    update({ activity_constraints: [...values.filter(item => item.candidate_id !== id), { ...previous, ...changes }] });
  };
  const selectLodging = (hotel: LodgingSupplement) => {
    const latest = session.request.getSnapshot().value.request.planning ?? defaultPlanning;
    const date = hotel.lodging_window.date;
    if (!latest.day_windows?.slice(0, -1).some(day => day.date === date) || !input?.lodging_candidates?.some(place => place.candidate_id === hotel.candidate_id && place.poi.id === hotel.poi.id)) {
      setError('住宿日期或候选已改变，请刷新后重新选择。'); return;
    }
    update({ overnight_stays: [...(latest.overnight_stays ?? []).filter(stay => stay.date !== date), { date, candidate_id: hotel.candidate_id }] });
    void reread();
  };
  const saveDietary = () => {
    if (dietaryInvalid) return false;
    if (JSON.stringify(dietaryEntries) !== JSON.stringify(session.request.getSnapshot().value.request.planning?.dietary_restrictions ?? [])) update({ dietary_restrictions: dietaryEntries });
    return true;
  };
  const saveProgress = async (clear = false) => {
    setProgressError(null);
    try {
      const coordinate: Coordinate = [Number(progress.longitude), Number(progress.latitude)];
      if (!clear && (!progress.name.trim() || !progress.city.trim() || !progress.longitude.trim() || !progress.latitude.trim() || !validCoordinate(coordinate) || !progress.at)) throw new Error('请提供名称、城市、有效的 GCJ02 坐标与当前日期时间。');
      update({ progress_origin: clear ? null : { name: progress.name.trim(), city: progress.city.trim(), location: coordinate, source: 'map', poi_id: null, request_origin: null }, progress_at: clear ? null : toIso(progress.at) });
      await session.request.flush();
      if (currentSession.current !== session) return;
      setProgressPicking(false);
      if (clear) setProgress(progressDraft(defaultPlanning));
    } catch (reason) { if (currentSession.current === session) setProgressError(reason instanceof Error ? reason.message : '出行进度尚未保存，编辑仍保留。'); }
  };
  const pickProgress = (coordinate: Coordinate) => { setProgress(value => ({ ...value, longitude: coordinate[0].toString(), latitude: coordinate[1].toString() })); setProgressPicking(false); };
  const readReplacements = async () => {
    const sequence = ++replacementSequence.current;
    setReplacementReading(true); setError(null);
    try {
      const snapshot = await api.planningReplacements(session.trip.id);
      if (currentSession.current !== session || replacementSequence.current !== sequence) return;
      if (snapshot.trip_id !== session.trip.id) throw new Error('替换地点归属不一致，已阻止展示。');
      setReplacementPlaces(snapshot);
    } catch (reason) { if (currentSession.current === session && replacementSequence.current === sequence) setError(reason instanceof Error ? reason.message : '替换地点读取失败。'); }
    finally { if (currentSession.current === session && replacementSequence.current === sequence) setReplacementReading(false); }
  };
  const replacementOptions = replacementPlaces?.candidates.filter(candidate => !workspace.value.selected_ids.includes(candidate.candidate_id) && validCoordinate(candidate.poi.location)) ?? [];
  return <section className="planning-workspace" aria-label={planning.day_windows?.length ? '多日规划工作区' : '单日规划工作区'}>
    <div className="section-subheading"><h3>{planning.day_windows?.length ? '多日规划条件' : '单日规划条件'}</h3><button disabled={reading || tasks.busy} onClick={() => { void reread(); }}>重新读取规划输入</button></div>
    <p className="discovery-note">时长、预约和费用手填值记为用户确认；空值保持未知。候选与评价仍可能误读，请结合原文核对；地点确认不代表营业或费用已核实。</p>
    {reading && <p role="status">正在读取已保存的规划输入…</p>}
    {error && <p role="alert" className="inline-error">{error}</p>}
    {request.status !== 'saved' && <p role="status">规划条件{request.status === 'error' || request.status === 'conflict' ? '尚未保存，请先处理保存提示' : '正在保存'}；已保存快照不会覆盖当前编辑。</p>}
    {input?.missing.length ? <div className="planning-issues"><strong>生成前需要补齐</strong><ul>{input.missing.map((item, i) => <li key={`${item.code}-${i}`}>{item.message}</li>)}</ul></div> : input && <p>起点、日期和已选地点已保存；规划会按这些条件核对交通与硬约束。</p>}
    <MultiDayEditor key={session.trip.id} planning={planning} input={input} disabled={archived || tasks.busy} onChange={update} />
    <RailEditor key={`rail-${session.trip.id}`} planning={planning} input={input} disabled={archived || tasks.busy} tripId={session.trip.id} requestVersion={request.version} saved={request.status === 'saved'} onSearch={searchRail} onChange={update} />
    <fieldset disabled={archived || tasks.busy}><legend>餐饮条件</legend><label>饮食限制与忌口（每行一项）<textarea rows={3} value={dietary} onChange={event => setDietary(event.target.value)} onBlur={saveDietary} aria-describedby="dietary-guidance" aria-invalid={dietaryInvalid} /></label><p id="dietary-guidance" className="discovery-note">最多 20 项，每项 200 字。补查仅提供来源线索；忌口、过敏、营业与菜品需要向商家核实，不能据此确认安全。</p>{dietaryInvalid && <p role="alert" className="inline-error">饮食限制最多 20 项，每项不得超过 200 字；请修改后保存。</p>}</fieldset>
    <details className="planning-progress"><summary>出行进度与当前位置</summary><p>明确提供或在方案地图点选当前位置；坐标使用 GCJ02，不自动获取设备位置。保存进度后重算未完成安排，已完成与锁定项留在历史中。</p>{planning.progress_origin && planning.progress_at && <p>已保存：{planning.progress_origin.name} · {planning.progress_origin.city || '城市未明确'} · {time(planning.progress_at, planning.timezone)}</p>}<fieldset disabled={archived || tasks.busy}><legend className="sr-only">用户确认的当前位置与时间</legend><div className="planning-fields"><label>当前位置名称<input maxLength={300} value={progress.name} onChange={event => setProgress(value => ({ ...value, name: event.target.value }))} /></label><label>当前位置城市<input maxLength={200} value={progress.city} onChange={event => setProgress(value => ({ ...value, city: event.target.value }))} /></label><label>当前位置经度（GCJ02）<input type="number" step="any" min="-180" max="180" value={progress.longitude} onChange={event => setProgress(value => ({ ...value, longitude: event.target.value }))} /></label><label>当前位置纬度（GCJ02）<input type="number" step="any" min="-90" max="90" value={progress.latitude} onChange={event => setProgress(value => ({ ...value, latitude: event.target.value }))} /></label><label>当前日期与时间（本机时区输入）<input type="datetime-local" value={progress.at} onChange={event => setProgress(value => ({ ...value, at: event.target.value }))} /></label></div><div className="planning-progress-actions"><button disabled={!chosen} onClick={() => setProgressPicking(value => !value)}>{progressPicking ? '取消当前位置点选' : '在方案地图点选当前位置'}</button><button onClick={() => { void saveProgress(); }}>确认当前位置与时间并保存</button>{planning.progress_origin && <button onClick={() => { void saveProgress(true); }}>清除当前位置与时间</button>}</div></fieldset>{progressPicking && <p role="status">请点击下面的方案地图，再补充名称、城市与时间并确认保存。</p>}{progressError && <p role="alert" className="inline-error">{progressError}</p>}</details>
    <fieldset disabled={archived || tasks.busy}><legend>交通与返程约束</legend><div className="planning-fields"><label>起点所在城市（地图点选请明确填写）<input maxLength={200} value={planning.origin_city ?? input?.origin?.city ?? ""} onChange={event => update({ origin_city: event.target.value.trim() || null })} /></label><label><input type="checkbox" checked={planning.return_to_origin} onChange={event => update({ return_to_origin: event.target.checked, end_candidate_id: null })} /> 计划返回出发点</label><label>指定终点<select disabled={planning.return_to_origin} value={planning.end_candidate_id ?? ''} onChange={event => update({ end_candidate_id: event.target.value || null })}><option value="">不指定，最后一个活动结束</option>{input?.selected.map(place => <option key={place.candidate_id} value={place.candidate_id}>{place.name} · {place.poi.name}</option>)}</select></label><label>结束期限（本机时区输入）<input type="datetime-local" value={toLocal(planning.end_at)} onChange={event => update({ end_at: toIso(event.target.value) })} /></label><label>累计步行上限（米）<input type="number" min="0" max="100000" value={planning.max_walk_meters ?? ''} onChange={event => update({ max_walk_meters: event.target.value === '' ? null : Number(event.target.value) })} /></label><label>每段额外缓冲（分钟，程序默认 10）<input type="number" min="0" max="120" value={planning.transfer_buffer_minutes} onChange={event => update({ transfer_buffer_minutes: Number(event.target.value) })} /></label></div><div className="planning-modes">禁用交通：{modes.map(([mode, label]) => <label key={mode}><input type="checkbox" checked={planning.disabled_modes.includes(mode)} onChange={event => update({ disabled_modes: event.target.checked ? [...planning.disabled_modes, mode] : planning.disabled_modes.filter(value => value !== mode) })} /> {label}</label>)}</div></fieldset>
    {editablePlaces.map(place => {
      const supplement = adoptedMeals.find(item => item.candidate_id === place.candidate_id);
      const constraint = planning.activity_constraints.find(item => item.candidate_id === place.candidate_id) ?? { ...newConstraint(place.candidate_id), ...supplement?.constraint };
      const completed = constraint.state === 'completed';
      const protectedEdit = completed || constraint.locked;
      return <fieldset key={place.candidate_id} disabled={archived || tasks.busy} className="planning-activity">
        <legend>{place.name}</legend><p>{place.poi.name} · {place.poi.address || '地址未知'} · {place.source === 'user' ? '用户指定，无攻略或评价证据' : `${place.activities.length} 项来源活动`}</p>
        {supplement && <p className="discovery-note">当前采用方案实际安排的补查餐饮；其默认停留仍为程序估计，餐次和营业待核实。</p>}
        <label>项目进度<select value={constraint.state} disabled={completed} onChange={event => change(place.candidate_id, { state: event.target.value as ActivityConstraint['state'] })}><option value="pending">未完成</option><option value="completed" disabled={!input?.active_plan_id || constraint.excluded}>已完成</option><option value="skipped" disabled={constraint.required || constraint.locked}>跳过</option></select></label>
        {completed && <p className="discovery-note">已完成项目的时间、费用与历史位置保留，不参与后缀替换。</p>}
        <fieldset disabled={protectedEdit}><legend className="sr-only">停留与时间条件</legend><div className="planning-fields">
          {planning.day_windows?.length ? <label>活动安排日期<select value={constraint.date ?? ''} onChange={event => change(place.candidate_id, { date: event.target.value || null })}><option value="">日期未知，尚未分配</option>{planning.day_windows.map(day => <option key={day.date} value={day.date}>{day.date}</option>)}</select></label> : null}
          <label>停留分钟<input type="number" min="1" max="1440" value={constraint.duration_minutes ?? ''} onChange={event => change(place.candidate_id, { duration_minutes: event.target.value === '' ? null : Number(event.target.value), duration_source: event.target.value === '' ? 'unknown' : 'user' })} /></label>
          <label>确认费用（元/同行全组）<input type="number" min="0" step="0.01" value={constraint.cost_cny ?? ''} onChange={event => change(place.candidate_id, { cost_cny: event.target.value === '' ? null : Number(event.target.value), cost_source: event.target.value === '' ? 'unknown' : 'user' })} /></label>
          <label>固定预约（本机时区输入）<input type="datetime-local" value={toLocal(constraint.fixed_start_at)} onChange={event => change(place.candidate_id, { fixed_start_at: toIso(event.target.value) })} /></label>
          <label>可进入时间<input type="datetime-local" value={toLocal(constraint.not_before)} onChange={event => change(place.candidate_id, { not_before: toIso(event.target.value) })} /></label>
          <label>须离开时间<input type="datetime-local" value={toLocal(constraint.not_after)} onChange={event => change(place.candidate_id, { not_after: toIso(event.target.value) })} /></label>
        </div></fieldset>
        <div className="planning-flags"><label><input type="checkbox" checked={constraint.required} disabled={completed || constraint.excluded || constraint.state === 'skipped'} onChange={event => change(place.candidate_id, { required: event.target.checked })} /> 必去</label><label><input type="checkbox" checked={constraint.excluded} disabled={completed || constraint.required || constraint.locked} onChange={event => change(place.candidate_id, { excluded: event.target.checked })} /> 排除</label><label><input type="checkbox" checked={constraint.locked} disabled={completed || !constraint.locked && (!input?.active_plan_id && !constraint.fixed_start_at || constraint.excluded || constraint.state === 'skipped')} onChange={event => change(place.candidate_id, { locked: event.target.checked })} /> 锁定预约</label><span>停留：{provenanceLabels[constraint.duration_source]}；费用：{provenanceLabels[constraint.cost_source]}</span></div>
        {!supplement && !completed && !constraint.locked && !constraint.required && !constraint.fixed_start_at && <div className="planning-replacement"><button disabled={replacementReading} onClick={() => { void readReplacements(); }}>读取已核对的替换候选</button>{replacementPlaces && <><label>替换为另一已确认地点<select value={replacementId[place.candidate_id] ?? ''} onChange={event => setReplacementId(value => ({ ...value, [place.candidate_id]: event.target.value }))}><option value="">请选择</option>{replacementOptions.map(candidate => <option key={candidate.candidate_id} value={candidate.candidate_id}>{candidate.name} · {candidate.poi.name || '名称未知'} · {candidate.poi.address || '地址未知'}</option>)}</select></label>{!replacementOptions.length && <p>当前没有其他已核对地点，请先在地点与地图中核对候选。</p>}<button disabled={!replacementId[place.candidate_id]} onClick={() => { void session.replacePlanningCandidate(place.candidate_id, replacementId[place.candidate_id]).then(saved => { if (saved && currentSession.current === session) { setReplacementPlaces(null); setRefresh(value => value + 1); } }); }}>替换此未完成项目</button></>}</div>}
      </fieldset>;
    })}
    <div className="planning-generation"><button className="primary" disabled={archived || dietaryInvalid || Boolean(tasks.planningReplacement) || tasks.busy || tasks.runs.some(run => active(run.status)) || Boolean(tasks.operation && tasks.operation.kind !== 'plan')} onClick={() => { if (saveDietary()) void session.startPlanning(input?.active_plan_id ?? undefined); }}>{tasks.operation?.kind === 'plan' ? '重试同一规划任务' : input?.active_plan_id ? '重算未完成安排' : planning.day_windows?.length ? '生成多日方案' : '生成单日方案'}</button><p>本轮最多 {defaultPlanningBudget.max_routes} 次高德请求（含路线、城市与地点核对）、{defaultPlanningBudget.max_combinations} 个组合、{defaultPlanningBudget.max_seconds / 60} 分钟。资料不足保留草案，生成不会自动采用。</p></div>
    {tasks.error && <div className="message-error" role="alert"><p>{tasks.error}</p>{tasks.operation?.kind === 'plan' && <><p>已发出的请求可能完成。重试沿用原操作；放弃后重新生成会开始新任务。</p><button disabled={tasks.busy} onClick={() => session.forgetRunOperation()}>明确放弃本次规划重试</button></>}{tasks.planAdoption && <><p>采用请求可能已保存，重试沿用同一操作。</p><button disabled={tasks.busy} onClick={() => session.forgetPlanAdoption()}>明确放弃采用重试</button></>}{tasks.planningReplacement && <><p>替换请求可能已保存；重试沿用原项目、原候选与同一操作。</p><button disabled={tasks.busy} onClick={() => { const operation = tasks.planningReplacement!; void session.replacePlanningCandidate(operation.candidate_id, operation.replacement_id); }}>重试同一项目替换</button><button disabled={tasks.busy} onClick={() => session.forgetPlanningReplacement()}>明确放弃替换重试</button></>}</div>}
    {planningRuns.length > 0 && <div className="planning-run-list" aria-label="规划任务记录">{planningRuns.map(run => {
      const old = run.request_version !== request.version || run.status === 'stale';
      const exhausted = /budget/.test(run.stop_reason ?? '') || run.usage.elapsed_seconds >= run.budget.max_seconds || (run.usage.amap_calls ?? (run.usage.route_calls ?? 0) + (run.usage.city_calls ?? 0) + (run.usage.poi_calls ?? 0)) >= (run.budget.max_routes ?? Infinity);
      return <article key={run.id}><strong>{old ? '旧条件记录' : runLabels[run.status]}</strong><span> · {stageLabels[run.stage] ?? '准备规划'} · 条件版本 {run.request_version}</span><p>路线调用 {run.usage.route_calls ?? 0} · 城市核对 {run.usage.city_calls ?? 0} · 地点核对 {run.usage.poi_calls ?? 0} · 来源搜索 {run.usage.searches ?? 0} · 模型调用 {run.usage.model_calls ?? 0} · 缓存命中 {run.usage.cache_hits ?? 0}</p>{run.result && 'plan_ids' in run.result && <p>{run.result.summary}</p>}{run.stop_reason && <p>停止原因：{run.stop_reason}</p>}<div>{active(run.status) && <button disabled={tasks.busy} onClick={() => { void session.controlRun(run.id, 'cancel'); }}>取消规划任务</button>}{!old && !exhausted && !archived && ['paused', 'recoverable', 'cancelled', 'failed', 'partial'].includes(run.status) && run.stage !== 'done' && run.error_code !== 'invalid_run_data' && <button disabled={tasks.busy} onClick={() => { void session.controlRun(run.id, 'resume'); }}>恢复规划任务</button>}</div></article>;
    })}</div>}
    <div className="planning-history"><h4>已保存方案</h4>{plans.length ? <><label>查看方案版本<select value={chosen?.id ?? ''} onChange={event => { setViewId(event.target.value); setCompareId(''); }}>{plans.map(plan => <option key={plan.id} value={plan.id}>方案 {plan.version} · {statusLabels[plan.status]} · 条件版本 {plan.request_version}{plan.id === input?.active_plan_id ? ' · 当前采用' : ''}</option>)}</select></label>{chosen && <><div className="planning-plan-status"><strong>{statusLabels[chosen.status]}{chosen.id === input?.active_plan_id ? ' · 当前采用' : ''}</strong><span>条件版本 {chosen.request_version} · 规划时区 {chosen.payload.input.planning.timezone}</span></div>{!current && <p className="planning-warning">当前条件或编辑与这份冻结方案不同，仅供历史查看。</p>}{!fresh && <p className="planning-warning">路线校验有效期已过或未知，请重新生成。不能据此确认可执行。</p>}{canAdopt && chosen.status === 'validated' && <p>当前条件已校验；交通预测仍需在出发时核对。</p>}{chosen.payload.unknowns.length > 0 && <div className="planning-issues"><strong>仍需确认</strong><ul>{chosen.payload.unknowns.map((issue, index) => <li key={index}>{issueText(issue, chosen.payload.input.planning.timezone)}</li>)}</ul></div>}{chosen.payload.conflicts.length > 0 && <div className="planning-issues" role="alert"><strong>硬约束冲突</strong><ul>{chosen.payload.conflicts.map((issue, index) => <li key={index}>{issueText(issue, chosen.payload.input.planning.timezone)}</li>)}</ul></div>}<PlanDetails key={chosen.id} plan={chosen} progressPicking={progressPicking} onProgressPick={pickProgress} lodgingSelection={{ input, planning, disabled: archived || reading || !clean || tasks.busy || tasks.runs.some(run => active(run.status)), onSelect: selectLodging }} /><button disabled={archived || dietaryInvalid || Boolean(tasks.planningReplacement) || tasks.busy || tasks.runs.some(run => active(run.status)) || Boolean(tasks.operation)} onClick={() => { if (saveDietary()) void session.startPlanning(chosen.id); }}>按此版本重新校验</button><p className="discovery-note">重新校验会按当前条件创建新草案，保留已完成和锁定项；旧版本与当前采用记录继续保留。</p>{['conditional', 'validated'].includes(chosen.status) && chosen.id !== input?.active_plan_id && <button className="primary" disabled={archived || dietaryInvalid || Boolean(tasks.planningReplacement) || tasks.busy || Boolean(tasks.planAdoption && tasks.planAdoption.planId !== chosen.id) || !canAdopt && tasks.planAdoption?.planId !== chosen.id} onClick={() => { void adopt(chosen); }}>{tasks.planAdoption?.planId === chosen.id ? '重试同一采用请求' : chosen.status === 'conditional' ? '采用有条件草案' : '采用已校验方案'}</button>}<label>只读比较另一版本<select value={compareId} onChange={event => setCompareId(event.target.value)}><option value="">不比较</option>{plans.filter(plan => plan.id !== chosen.id).map(plan => <option key={plan.id} value={plan.id}>方案 {plan.version} · 条件版本 {plan.request_version}</option>)}</select></label>{compared && <div className="planning-comparison" aria-label="方案版本比较"><div><h5>方案 {chosen.version}</h5><PlanSummary plan={chosen} /></div><div><h5>方案 {compared.version}</h5><PlanSummary plan={compared} /></div></div>}</>}</> : <p>尚无方案。保存条件与已选地点后，生成真实交通与单日排程草案。</p>}</div>
  </section>;
}

function itineraryName(item: ItineraryItem) {
  return item.kind === 'activity' ? item.name : item.kind === 'route' ? `${modeLabels[item.leg.mode] ?? item.leg.mode}交通` : item.kind === 'rail' ? `铁路 · ${item.train_number ?? '待确定车次'}` : '等待与缓冲';
}
function PlanSummary({ plan }: { plan: Plan }) {
  const timezone = plan.payload.input.planning.timezone;
  return <><p>{statusLabels[plan.status]} · 条件版本 {plan.request_version}</p><FrozenConditions input={plan.payload.input} />{plan.payload.days?.length ? plan.payload.days.map((day, index) => <section key={day.date}><DayContext day={day} plan={plan} overnight={index < plan.payload.days!.length - 1} /><ol>{day.itinerary.map((item, i) => <li key={i}>{itineraryName(item)} · {time(item.start_at, timezone)} → {time(item.end_at, timezone)}</li>)}</ol></section>) : <ol>{plan.payload.itinerary.map((item, index) => <li key={index}>{itineraryName(item)} · {time(item.start_at, timezone)} → {time(item.end_at, timezone)}</li>)}</ol>}<p>{plan.payload.unknowns.map(issue => issueText(issue, timezone)).join('；')}</p><p>{plan.payload.conflicts.map(issue => issueText(issue, timezone)).join('；')}</p>{plan.payload.diff.map((item, index) => typeof item.message === 'string' ? <p key={index}>{item.message}</p> : null)}</>;
}
function FrozenConditions({ input }: { input: PlanningInput }) {
  const { planning } = input;
  return <><SingleDayFrozenConditions input={input} />{planning.day_windows?.length ? <details className="planning-frozen-conditions"><summary>冻结多日窗口与逐晚住宿（只读）</summary>{planning.day_windows.map(day => <p key={day.date}>{day.date} · {time(day.start_at, planning.timezone)} → {time(day.end_at, planning.timezone)}</p>)}{planning.overnight_stays?.map(stay => <p key={stay.date}>{stay.date} 晚酒店候选：{input.lodgings?.find(place => place.candidate_id === stay.candidate_id)?.name ?? '地点尚待核对'}；不表示已预订。</p>)}{planning.activity_constraints.filter(constraint => constraint.date).map(constraint => <p key={constraint.candidate_id}>{input.selected.find(place => place.candidate_id === constraint.candidate_id)?.name ?? '所选项目'} · 活动日期 {constraint.date}</p>)}</details> : null}</>;
}
function SingleDayFrozenConditions({ input }: { input: PlanningInput }) {
  const { request, planning } = input;
  return <details className="planning-frozen-conditions"><summary>冻结方案条件（只读）</summary><p>从 {input.origin?.name ?? '起点未确认'} 于 {time(request.start_at, planning.timezone)} 出发 · 可用 {request.duration_minutes ?? '未知'} 分钟 · {request.transport === 'mixed' ? '灵活组合' : modeLabels[request.transport]} · 同行 {request.participants} 人 · {request.budget_cny === null ? '预算未设上限' : `总预算 ¥${request.budget_cny}`}</p><p>终点：{planning.return_to_origin ? '返回出发点' : input.selected.find(place => place.candidate_id === planning.end_candidate_id)?.name ?? '最后一个活动结束'} · 结束期限 {time(planning.end_at, planning.timezone)} · 步行上限 {planning.max_walk_meters ?? '未设'} 米 · 每段缓冲 {planning.transfer_buffer_minutes} 分钟</p><p>禁用：{planning.disabled_modes.map(mode => modeLabels[mode]).join('、') || '无'} · 偏好：{request.preferences || '未填写'}</p><p>饮食限制：{planning.dietary_restrictions?.join('、') || '未填写'}；需向商家核实。</p>{planning.progress_origin && planning.progress_at && <p>用户确认当前位置：{planning.progress_origin.name} · {planning.progress_origin.city || '城市未明确'} · {time(planning.progress_at, planning.timezone)}</p>}{planning.activity_constraints.map(item => <p key={item.candidate_id}>{input.selected.find(place => place.candidate_id === item.candidate_id)?.name ?? '已选项目'} · 停留 {item.duration_minutes ?? '未知'} 分钟（{sourceLabel(item.duration_source)}） · {amount(item.cost_cny)}（{sourceLabel(item.cost_source)}）{item.required ? ' · 必去' : ''}{item.excluded ? ' · 排除' : ''}{item.locked ? ' · 锁定' : ''}{item.fixed_start_at ? ` · 固定预约 ${time(item.fixed_start_at, planning.timezone)}` : ''}{item.not_before ? ` · 可进入 ${time(item.not_before, planning.timezone)}` : ''}{item.not_after ? ` · 须离开 ${time(item.not_after, planning.timezone)}` : ''}</p>)}</details>;
}
function MealSources({ plan, candidateIds }: { plan: Plan; candidateIds?: string[] }) {
  const meals = (plan.payload.supplements ?? []).filter((meal): meal is MealSupplement => 'meal_window' in meal && (!candidateIds || candidateIds.includes(meal.candidate_id)));
  if (!meals.length) return null;
  const timezone = plan.payload.input.planning.timezone;
  return <details className="planning-meal-sources"><summary>餐饮补查候选与原文证据（{meals.length} 项）</summary><p>来源提及和 POI 核对仅支持候选；只有列入时间表的条目才是本方案安排，营业、费用与饮食限制仍需核实。</p>{meals.map(meal => {
    const scheduled = plan.payload.itinerary.some(item => item.kind === 'activity' && item.candidate_id === meal.candidate_id && item.state !== 'skipped');
    return <article key={meal.candidate_id} aria-label={`${meal.name}补查来源`}><h5>{meal.name} · {scheduled ? '列入此方案时间表' : '未列入此方案时间表'}</h5><p>{meal.poi.name} · {meal.poi.address || '地址未知'} · {meal.meal_window.period === 'lunch' ? '午餐' : '晚餐'}窗口 {time(meal.meal_window.start_at, timezone)} → {time(meal.meal_window.end_at, timezone)}</p><p>高德 {meal.poi.api_version} · 读取 {time(meal.poi.read_at, timezone)} · GCJ02 {meal.poi.location.join(', ')}</p>{meal.sources.length ? meal.sources.map((source, index) => <div key={index}><strong>{typeof source.title === 'string' && source.title || (source.kind === 'comment' ? '评论原文' : '笔记原文')}</strong><blockquote>{typeof source.text === 'string' && source.text || '正文缺失，不可据此推定推荐。'}</blockquote><small>来源 ID：{typeof source.id === 'string' ? source.id : '未知'} · {source.read_status === 'success' ? '读取成功' : '读取状态未知'} · {source.completeness === 'complete' ? '文本读取完整' : '文本完整性待核实'}{typeof source.read_at === 'string' ? ` · 本轮读取 ${time(source.read_at, timezone)}` : ''}</small></div>) : <p>未保存可读原文，来源支持仍未知。</p>}{meal.evidence.length ? meal.evidence.map((evidence, index) => <div key={index}><p>结构化评价 · {typeof evidence.object_text === 'string' ? evidence.object_text : '对象未知'} · {typeof evidence.dimension === 'string' ? evidence.dimension : '性质未知'} · {evidence.stance === 'positive' ? '正面' : evidence.stance === 'negative' ? '负面' : '不确定'}</p><blockquote>{typeof evidence.quote === 'string' ? evidence.quote : '原文引用缺失'}</blockquote><small>证据来源 ID：{typeof evidence.source_id === 'string' ? evidence.source_id : '未知'} · {typeof evidence.model_id === 'string' ? evidence.model_id : '模型未知'} · {typeof evidence.schema_version === 'string' ? evidence.schema_version : '版本未知'}</small></div>) : <p>本候选无结构化评价证据，不能据此声称推荐。</p>}</article>;
  })}</details>;
}
function DayContext({ day, plan, overnight }: { day: DayPlan; plan: Plan; overnight: boolean }) {
  const timezone = plan.payload.input.planning.timezone;
  const hotel = plan.payload.input.lodgings?.find(place => place.candidate_id === day.lodging_candidate_id);
  const stay = plan.payload.input.planning.overnight_stays?.find(item => item.date === day.date);
  return <section className="planning-day-context" aria-label={`${day.date} 日计划`}><h5>{day.date}</h5><p>当日窗口：{time(day.start_at, timezone)} → {time(day.end_at, timezone)}（{timezone}）</p><p>当日出发：{day.origin?.name ?? '未知，不能确认接驳'} · 当日结束：{day.endpoint?.name ?? '未知，不能确认接驳'}</p>{plan.payload.input.planning.progress_at && plan.payload.input.planning.progress_origin && new Intl.DateTimeFormat('sv-SE', { timeZone: timezone }).format(new Date(plan.payload.input.planning.progress_at)) === day.date && <p>本日恢复从用户确认当前位置：{plan.payload.input.planning.progress_origin.name} · {time(plan.payload.input.planning.progress_at, timezone)}</p>}{day.overnight_rail_id && <p>本晚为跨日铁路在途：{day.overnight_rail_id}。保留实际到达日期，不自动补酒店；车票与卧铺/座席条件仍需核实。</p>}{day.continuing_rail_id && <p>承接前日跨日铁路：{day.continuing_rail_id}，到达及出站缓冲后才安排市内活动。</p>}{overnight && !day.overnight_rail_id && (hotel ? <><p>本晚酒店候选：{hotel.name} · {hotel.city || '城市未知'} · {hotel.poi.address || '地址未知'}</p><p>{hotel.source === 'user' ? '用户指定，无攻略或评价证据。' : '有来源的候选，仍需核实。'}绑定仅对应 {day.date} 晚，不表示已预订；{stay?.check_in_not_before || stay?.check_out_by || stay?.nightly_cost_cny != null ? '平台房态、实时可订价格、办理时长与行李寄存仍未核实。' : '房态、当晚房价、入住、退房与行李寄存未知。'}</p>{(stay?.check_in_not_before || stay?.check_out_by || stay?.nightly_cost_cny != null) && <p>用户明确约束：最早入住 {time(stay.check_in_not_before ?? null, timezone)} · 最晚退房 {time(stay.check_out_by ?? null, timezone)} · 本晚总费用 {amount(stay.nightly_cost_cny ?? null)}；不是平台实时可订报价。</p>}{hotel.source !== 'user' && <details><summary>本晚酒店来源与证据</summary>{hotel.sources.map((source, index) => <div key={index}><strong>{typeof source.title === 'string' ? source.title : '原文来源'}</strong><blockquote>{typeof source.text === 'string' ? source.text : '正文未知'}</blockquote><small>来源 ID：{typeof source.id === 'string' ? source.id : '未知'}</small></div>)}{hotel.evidence.map((evidence, index) => <blockquote key={index}>{typeof evidence.quote === 'string' ? evidence.quote : '引用未知'}</blockquote>)}{!hotel.sources.length && !hotel.evidence.length && <p>暂无可读来源与评价证据。</p>}</details>}</> : <p className="planning-warning">{day.date} 晚缺少已核对酒店；翌日起点与住宿接驳仍未知。</p>)}{day.unknowns.length > 0 && <div className="planning-issues"><strong>本日待确认</strong><ul>{day.unknowns.map((issue, index) => <li key={index}>{issueText(issue, timezone)}</li>)}</ul></div>}{day.conflicts.length > 0 && <div className="planning-issues" role="alert"><strong>本日硬约束冲突</strong><ul>{day.conflicts.map((issue, index) => <li key={index}>{issueText(issue, timezone)}</li>)}</ul></div>}</section>;
}
function PlanDetails({ plan, progressPicking, onProgressPick, lodgingSelection }: { plan: Plan; progressPicking: boolean; onProgressPick: (coordinate: Coordinate) => void; lodgingSelection?: LodgingSelection }) {
  const [date, setDate] = useState(plan.payload.days?.[0]?.date ?? '');
  const days = plan.payload.days ?? [];
  const day = days.find(value => value.date === date) ?? days[0];
  return <>{days.length > 0 && <><label>查看日计划<select value={day?.date ?? ''} onChange={event => setDate(event.target.value)}>{days.map(value => <option key={value.date} value={value.date}>{value.date}</option>)}</select></label>{day && <><DayContext day={day} plan={plan} overnight={days.indexOf(day) < days.length - 1} /><LodgingSuggestions plan={plan} date={day.date} selection={lodgingSelection} /></>}</>}<DayDetails key={`${plan.id}:${day?.date ?? 'single'}`} plan={plan} day={day} progressPicking={progressPicking} onProgressPick={onProgressPick} /></>;
}
function DayDetails({ plan, day, progressPicking, onProgressPick }: { plan: Plan; day?: DayPlan; progressPicking: boolean; onProgressPick: (coordinate: Coordinate) => void }) {
  const timezone = plan.payload.input.planning.timezone;
  const itinerary = day?.itinerary ?? plan.payload.itinerary;
  const routes = useMemo(() => itinerary.filter(item => item.kind === 'route').map(item => item.leg), [itinerary]);
  const points = useMemo(() => {
    const scheduledMeals = plan.payload.supplements?.filter(meal => itinerary.some(item => item.kind === 'activity' && item.candidate_id === meal.candidate_id && item.state !== 'skipped')) ?? [];
    const result: MapPoint[] = [...plan.payload.input.selected, ...scheduledMeals].filter(place => !day || itinerary.some(item => item.kind === 'activity' && item.candidate_id === place.candidate_id)).map(place => ({ id: place.poi.id, name: place.poi.name ?? place.name, location: place.poi.location }));
    const origin = day ? day.origin : plan.payload.input.origin;
    if (origin) result.unshift({ id: 'plan-origin', name: origin.name, location: origin.location, origin: true });
    const endpoint = day?.endpoint;
    if (endpoint && !result.some(point => point.id === endpoint.poi_id || point.origin && point.location.every((value, index) => value === endpoint.location[index]))) result.push({ id: 'day-endpoint', name: `当日终点：${endpoint.name}`, location: endpoint.location });
    itinerary.filter(item => item.kind === 'rail').forEach(item => [item.departure_station, item.arrival_station].forEach(station => { if (station) result.push({ id: station.poi.id, name: station.poi.name ?? station.name, location: station.poi.location }); }));
    const progress = plan.payload.input.planning.progress_origin;
    const progressAt = plan.payload.input.planning.progress_at;
    if (progress && (!day || progressAt && new Intl.DateTimeFormat('sv-SE', { timeZone: timezone }).format(new Date(progressAt)) === day.date)) result.push({ id: 'plan-progress-origin', name: `用户确认当前位置：${progress.name}`, location: progress.location });
    return result;
  }, [plan, day, itinerary]);
  const [center, setCenter] = useState<[number, number] | null>(null);
  const [zoom, setZoom] = useState(12);
  const [focus, setFocus] = useState<{ id: string; sequence: number } | null>(null);
  useEffect(() => { setCenter(null); setZoom(12); setFocus(null); }, [plan.id]);
  return <div className="planning-plan-details"><FrozenConditions input={plan.payload.input} /><MealSources plan={plan} candidateIds={day ? itinerary.flatMap(item => item.kind === 'activity' ? [item.candidate_id] : []) : undefined} /><ol className="planning-timeline" aria-label={`方案 ${plan.version}${day ? ` · ${day.date}` : ''} 时间表`}>{itinerary.map((item, index) => <li key={index} className={`planning-item-${item.kind}`}><div className="planning-time">{time(item.start_at, timezone)} → {time(item.end_at, timezone)}</div><strong>{itineraryName(item)}</strong>{item.historical && <p className="planning-warning">{item.untravelled || item.execution_unknown || item.kind === 'activity' && item.state === 'skipped' ? '原方案历史快照：已跳过或实际交通未确认，不能计作已发生。' : '已完成历史段；保留原记录，不能据此确认当前交通。'}</p>}{item.kind === 'activity' ? <><p>{item.poi.name} · 停留 {item.duration_minutes ?? '未知'} 分钟（{sourceLabel(item.duration_source)}） · {amount(item.cost_cny)}（{sourceLabel(item.cost_source)}）</p><p>{item.required ? '必去 · ' : ''}{item.locked ? '锁定 · ' : ''}{item.state === 'completed' ? '已完成' : item.state === 'skipped' ? '已跳过' : '待进行'}</p></> : item.kind === 'route' ? <><p>预计 {item.leg.duration_seconds === null ? '时长未知' : `${Math.ceil(item.leg.duration_seconds / 60)} 分钟`}（{sourceLabel(item.leg.duration_source)}） · 累计本段步行 {item.leg.walking_distance_meters ?? '未知'} 米 · {amount(item.leg.cost_cny)}（{sourceLabel(item.leg.cost_source)}）</p><p>高德 {item.leg.api_version} · 读取 {time(item.leg.read_at, timezone)} · 有效至 {time(item.leg.valid_until, timezone)}</p>{item.leg.conditions.length > 0 && <p>{item.leg.conditions.map(condition => typeof condition === 'string' ? condition : condition.message).join('；')}</p>}{(!item.leg.geometry_complete || !item.leg.geometry.length) && <p className="planning-warning">路线形状缺失或不完整，地图仅绘制高德返回的独立线段。</p>}</> : item.kind === 'rail' ? <><p>{item.departure_station?.poi.name ?? '出发站待确认'} → {item.arrival_station?.poi.name ?? '到达站待确认'} · {item.confirmed ? '用户确认时刻' : '车次时刻待确认'}</p><p>进站预留 {item.boarding_buffer_minutes} 分钟 · 出站预留 {item.arrival_buffer_minutes} 分钟 · {amount(item.cost_cny)}（{sourceLabel(item.cost_source)}）</p><p>铁路段独立于高德市内路线；未证明已购票、余票或准点，跨午夜按完整日期展示。</p></> : <p>{item.reason}</p>}</li>)}</ol>{(routes.length > 0 || itinerary.some(item => item.kind === 'rail') || progressPicking) && <div className="planning-route-map"><h5>{day ? `${day.date} 的地点与交通路线` : '此方案的地点与交通路线'}</h5><p>按交通方式着色：步行绿色、公交地铁蓝色、自行车黄色、电动车棕色、驾车紫色。缺失线段不补直线，其他日期与方案不叠加。</p><AMapCanvas points={points} routes={routes} center={center} zoom={zoom} focus={focus} picking={progressPicking} pickNotice="点击地图选择当前位置，再确认时间并保存。" onViewport={(value, level) => { setCenter(value); setZoom(level); }} onPick={onProgressPick} onSelect={id => setFocus(value => ({ id, sequence: (value?.sequence ?? 0) + 1 }))} /></div>}{plan.payload.diff.length > 0 && <details><summary>相对父版本的已保存差异</summary>{plan.payload.diff.map((item, index) => typeof item.message === 'string' ? <p key={index}>{item.message}</p> : null)}</details>}</div>;
}
