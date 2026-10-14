import { useEffect, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import { defaultPlanning, newConstraint } from './planning-types';
import type { ActivityConstraint, Plan, PlanningConstraints, PlanningInput } from './planning-types';
import type { TripSession } from './session';

const provenanceLabels = { user: '用户确认', source: '来源报告（待核当前有效性）', estimated: '程序估计', unknown: '未知' };
const modes: [PlanningConstraints['disabled_modes'][number], string][] = [['walking', '步行'], ['transit', '公交地铁'], ['bicycling', '自行车'], ['electrobike', '电动车'], ['driving', '驾车']];
const toLocal = (value: string | null) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
const toIso = (value: string) => value ? new Date(value).toISOString() : null;

export function Planning({ session, archived }: { session: TripSession; archived: boolean }) {
  const request = useSyncExternalStore(session.request.subscribe, session.request.getSnapshot);
  const workspace = useSyncExternalStore(session.workspace.subscribe, session.workspace.getSnapshot);
  const [input, setInput] = useState<PlanningInput | null>(null);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const planning = request.value.request.planning ?? defaultPlanning;
  const selection = workspace.value.selected_ids.join('|');
  const origin = JSON.stringify(workspace.value.confirmed_origin ?? null);
  useEffect(() => {
    let current = true;
    setReading(true); setError(null);
    void Promise.all([api.planningInput(session.trip.id), api.plans(session.trip.id)]).then(([value, history]) => {
      if (current && value.request_version === session.request.getSnapshot().version) { setInput(value); setPlans(history.items); }
    }).catch(reason => { if (current) setError(reason instanceof Error ? reason.message : '规划输入读取失败'); }).finally(() => { if (current) setReading(false); });
    return () => { current = false; };
  }, [session, request.version, selection, origin, refresh]);
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
    <div className="section-subheading"><h3>单日规划条件</h3><button disabled={reading} onClick={() => setRefresh(value => value + 1)}>重新读取规划输入</button></div>
    <p className="discovery-note">时长、预约和费用手填值记为用户确认；空值保持未知。P2 推荐语义仍未通过验收，地点确认不代表营业或费用已核实。</p>
    {reading && <p role="status">正在读取已保存的规划输入…</p>}
    {error && <p role="alert" className="inline-error">{error}</p>}
    {request.status !== 'saved' && <p role="status">规划条件{request.status === 'error' || request.status === 'conflict' ? '尚未保存，请先处理保存提示' : '正在保存'}；已保存快照不会覆盖当前编辑。</p>}
    {input?.missing.length ? <div className="planning-issues"><strong>生成前需要补齐</strong><ul>{input.missing.map((item, i) => <li key={`${item.code}-${i}`}>{item.message}</li>)}</ul></div> : input && <p>起点、日期和已选地点已保存；后续仍需要真实交通与硬约束校验。</p>}
    <fieldset disabled={archived}><legend>交通与返程约束</legend><div className="planning-fields"><label><input type="checkbox" checked={planning.return_to_origin} onChange={event => update({ return_to_origin: event.target.checked, end_candidate_id: null })} /> 计划返回出发点</label><label>结束期限（本机时区输入）<input type="datetime-local" value={toLocal(planning.end_at)} onChange={event => update({ end_at: toIso(event.target.value) })} /></label><label>累计步行上限（米）<input type="number" min="0" max="100000" value={planning.max_walk_meters ?? ''} onChange={event => update({ max_walk_meters: event.target.value === '' ? null : Number(event.target.value) })} /></label><label>每段额外缓冲（分钟，程序默认 10）<input type="number" min="0" max="120" value={planning.transfer_buffer_minutes} onChange={event => update({ transfer_buffer_minutes: Number(event.target.value) })} /></label></div><div className="planning-modes">禁用交通：{modes.map(([mode, label]) => <label key={mode}><input type="checkbox" checked={planning.disabled_modes.includes(mode)} onChange={event => update({ disabled_modes: event.target.checked ? [...planning.disabled_modes, mode] : planning.disabled_modes.filter(value => value !== mode) })} /> {label}</label>)}</div></fieldset>
    {input?.selected.map(place => {
      const constraint = planning.activity_constraints.find(item => item.candidate_id === place.candidate_id) ?? newConstraint(place.candidate_id);
      return <fieldset key={place.candidate_id} disabled={archived} className="planning-activity"><legend>{place.name}</legend><p>{place.poi.name} · {place.poi.address || '地址未知'} · {place.activities.length} 项来源活动</p><div className="planning-fields"><label>停留分钟<input type="number" min="1" max="1440" value={constraint.duration_minutes ?? ''} onChange={event => change(place.candidate_id, { duration_minutes: event.target.value === '' ? null : Number(event.target.value), duration_source: event.target.value === '' ? 'unknown' : 'user' })} /></label><label>确认费用（元/同行全组）<input type="number" min="0" step="0.01" value={constraint.cost_cny ?? ''} onChange={event => change(place.candidate_id, { cost_cny: event.target.value === '' ? null : Number(event.target.value), cost_source: event.target.value === '' ? 'unknown' : 'user' })} /></label><label>固定预约（本机时区输入）<input type="datetime-local" value={toLocal(constraint.fixed_start_at)} onChange={event => change(place.candidate_id, { fixed_start_at: toIso(event.target.value) })} /></label><label>可进入时间<input type="datetime-local" value={toLocal(constraint.not_before)} onChange={event => change(place.candidate_id, { not_before: toIso(event.target.value) })} /></label><label>须离开时间<input type="datetime-local" value={toLocal(constraint.not_after)} onChange={event => change(place.candidate_id, { not_after: toIso(event.target.value) })} /></label></div><div className="planning-flags"><label><input type="checkbox" checked={constraint.required} disabled={constraint.excluded || constraint.state === 'skipped'} onChange={event => change(place.candidate_id, { required: event.target.checked })} /> 必去</label><label><input type="checkbox" checked={constraint.excluded} disabled={constraint.required || constraint.locked} onChange={event => change(place.candidate_id, { excluded: event.target.checked })} /> 排除</label><label><input type="checkbox" checked={constraint.locked} disabled={!constraint.locked && (!input.active_plan_id && !constraint.fixed_start_at || constraint.excluded)} onChange={event => change(place.candidate_id, { locked: event.target.checked })} /> 锁定预约</label><span>停留：{provenanceLabels[constraint.duration_source]}；费用：{provenanceLabels[constraint.cost_source]}</span></div></fieldset>;
    })}
    <div className="planning-history"><h4>已保存方案</h4>{plans.length ? plans.map(plan => <article key={plan.id}><strong>方案 {plan.version}{plan.id === input?.active_plan_id ? ' · 当前采用' : ''}</strong><span> · 条件版本 {plan.request_version} · {plan.status}</span><p>{plan.payload.unknowns.map(item => item.message).join('；')}{plan.payload.conflicts.map(item => item.message).join('；')}</p></article>) : <p>尚无方案。当前步骤保存规划输入；真实路线与单日排程将在下一步接通。</p>}</div>
  </section>;
}
