import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { api } from './api';
import { ApiError } from './api';
import type { Run, Trip } from './api';
import { Planning } from './Planning';
import { defaultPlanning, defaultPlanningBudget, newConstraint } from './planning-types';
import type { MealSupplement, Plan, PlanningInput, PlanningReplacements, RouteLeg } from './planning-types';
import { TripSession } from './session';
import { Runs } from './Runs';
import type { PlacesSnapshot } from './places-types';

type CanvasProps = Parameters<typeof import('./AMapCanvas').AMapCanvas>[0];
let canvas: CanvasProps | undefined;
vi.mock('./AMapCanvas', () => ({ AMapCanvas: (props: CanvasProps) => { canvas = props; return <div data-testid="plan-route-canvas">{props.picking && <button onClick={() => props.onPick([120.17, 30.26])}>测试点选当前位置</button>}</div>; } }));

const timestamp = '2026-10-15T01:00:00Z';
const trip: Trip = { id: 'one', name: '规划甲', status: 'draft', request: { destination: '杭州', origin: '杭州东站', start_at: timestamp, duration_minutes: 360, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' }, request_version: 1, workspace: { active_tab: 'plan', category: 'all', map_center: null, map_zoom: 12, map_visible_ids: ['candidate-one'], selected_ids: ['candidate-one'] }, workspace_version: 1, messages: [], runs: [], created_at: timestamp, updated_at: timestamp };
let session: TripSession;
let input: PlanningInput;
beforeEach(() => {
  canvas = undefined;
  input = { request_version: 1, input_hash: 'a'.repeat(64), request: structuredClone(trip.request), planning: structuredClone(defaultPlanning), origin: null, selected_ids: ['candidate-one'], selected: [{ candidate_id: 'candidate-one', name: '西湖散步', category: 'play', kind: 'place', city: '杭州市', run_id: 'run-one', poi: { id: 'poi-one', name: '西湖', location: [120.15,30.25], address: '西湖区', city: '杭州市', adcode: '330106', type: '风景名胜', entrance_location: null, read_at: timestamp, api_version: 'v5', missing_fields: [] }, activities: [{ description:'沿湖散步' }], sources: [], evidence: [], mentions: [] }], missing: [{ code: 'origin_missing', message: '请明确确认出发点坐标' }], active_plan_id: null };
  vi.spyOn(api, 'planningInput').mockResolvedValue(structuredClone(input));
  vi.spyOn(api, 'plans').mockResolvedValue({ items: [], active_plan_id: null });
  vi.spyOn(api, 'update').mockImplementation(async (_id, version, draft) => ({ ...structuredClone(trip), ...draft, request_version: version + 1 }));
  vi.spyOn(api, 'workspace').mockImplementation(async (_id, version, workspace) => ({ ...structuredClone(trip), workspace: structuredClone(workspace), workspace_version: version + 1 }));
  session = new TripSession(structuredClone(trip), () => {});
});
afterEach(() => { cleanup(); session.dispose(); vi.restoreAllMocks(); });

it('shows missing origin and real chosen places without claiming a feasible plan', async () => {
  render(<Planning session={session} archived={false} />);
  expect(await screen.findByText('请明确确认出发点坐标')).toBeTruthy();
  expect(screen.getByRole('group', { name: '西湖散步' })).toBeTruthy();
  expect(screen.getByText(/尚无方案/)).toBeTruthy();
  expect(screen.queryByText('已确认可执行')).toBeNull();
});

it('saves explicit duration and disabled cycling with user provenance and keeps unrelated conditions', async () => {
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  fireEvent.change(screen.getByLabelText('停留分钟'), { target: { value: '60' } });
  fireEvent.click(screen.getByLabelText('自行车'));
  fireEvent.click(screen.getByLabelText('必去'));
  await act(async () => { await session.flush(); });
  const saved = vi.mocked(api.update).mock.calls[0][2].request;
  expect(saved.destination).toBe('杭州');
  expect(saved.planning?.disabled_modes).toEqual(['bicycling']);
  expect(saved.planning?.activity_constraints[0]).toMatchObject({ duration_minutes: 60, duration_source: 'user', required: true, cost_cny: null, cost_source: 'unknown' });
});

it('clearing a cost keeps it unknown and excludes neither a required nor locked activity silently', async () => {
  render(<Planning session={session} archived={false} />);
  const group = within(await screen.findByRole('group', { name: '西湖散步' }));
  fireEvent.change(group.getByLabelText('确认费用（元/同行全组）'), { target: { value: '10' } });
  fireEvent.change(group.getByLabelText('确认费用（元/同行全组）'), { target: { value: '' } });
  fireEvent.click(group.getByLabelText('必去'));
  expect((group.getByLabelText('排除') as HTMLInputElement).disabled).toBe(true);
  expect(session.request.getSnapshot().value.request.planning?.activity_constraints[0]).toMatchObject({ cost_cny: null, cost_source: 'unknown', excluded: false });
});

it('discards an old response after switching trips', async () => {
  let finish!: (value: PlanningInput) => void;
  vi.mocked(api.planningInput).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const other = new TripSession({ ...structuredClone(trip), id: 'two', name: '规划乙' }, () => {});
  vi.mocked(api.planningInput).mockResolvedValue({ ...structuredClone(input), selected: [], selected_ids: [], missing: [{ code:'selection_missing', message:'乙行程还未选择地点' }] });
  const view = render(<Planning session={session} archived={false} />);
  view.rerender(<Planning session={other} archived={false} />);
  await screen.findByText('乙行程还未选择地点');
  await act(async () => { finish(input); });
  await waitFor(() => expect(screen.queryByRole('group', { name: '西湖散步' })).toBeNull());
  other.dispose();
});

it('can release a lock after its fixed appointment is cleared', async () => {
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  fireEvent.change(screen.getByLabelText('固定预约（本机时区输入）'), { target: { value:'2026-10-15T12:00' } });
  fireEvent.click(screen.getByLabelText('锁定预约'));
  fireEvent.change(screen.getByLabelText('固定预约（本机时区输入）'), { target: { value:'' } });
  expect((screen.getByLabelText('锁定预约') as HTMLInputElement).disabled).toBe(false);
  fireEvent.click(screen.getByLabelText('锁定预约'));
  expect(session.request.getSnapshot().value.request.planning?.activity_constraints[0].locked).toBe(false);
});

it('labels estimates separately from source reports and user confirmation', async () => {
  session.request.update({ ...session.request.getSnapshot().value, request: { ...trip.request, planning: { ...defaultPlanning, activity_constraints: [{ ...newConstraint('candidate-one'), duration_minutes: 60, duration_source: 'estimated', cost_cny: 10, cost_source: 'source' }] } } });
  render(<Planning session={session} archived={false} />);
  expect(await screen.findByText(/停留：程序估计；费用：来源报告/)).toBeTruthy();
});

const leg = (id = 'leg-one'): RouteLeg => ({ id, mode: 'walking', origin: [120.1, 30.2], destination: [120.15, 30.25], departure_at: timestamp, read_at: timestamp, valid_until: new Date(Date.now() + 3600000).toISOString(), api_version: 'v5', status: 'ok', duration_seconds: 600, distance_meters: 800, walking_distance_meters: 800, geometry: [[[120.1, 30.2], [120.12, 30.22]], [[120.13, 30.23], [120.15, 30.25]]], geometry_complete: false, cost_cny: null, cost_source: 'unknown', duration_source: 'amap', conditional: true, conditions: ['步道开放情况需核当前有效性'] });
const plan = (id = 'plan-one', version = 1, status: Plan['status'] = 'conditional'): Plan => ({ id, trip_id: 'one', version, request_version: 1, input_hash: input.input_hash, parent_plan_id: null, run_id: null, status, created_at: timestamp, payload: { input: structuredClone(input), itinerary: [{ kind: 'route', start_at: timestamp, end_at: '2026-10-15T01:10:00Z', leg: leg(id) }, { kind: 'activity', candidate_id: 'candidate-one', name: '西湖散步', category: 'play', poi: input.selected[0].poi, start_at: '2026-10-15T01:10:00Z', end_at: '2026-10-15T02:10:00Z', duration_minutes: 60, duration_source: 'estimated', cost_cny: null, cost_source: 'unknown', required: false, locked: false, state: 'pending' }], unknowns: [{ code: 'cost_unknown', message: '费用未核实' }], conflicts: [], usage: { route_calls: 1 }, diff: [], valid_until: new Date(Date.now() + 3600000).toISOString() } });
const planningRun = (status: Run['status'] = 'pending'): Run => ({ id: 'planning-run', trip_id: 'one', kind: 'plan', request_version: 1, generation: 1, status, stage: 'planning_input', params: {}, budget: { max_steps: 100, ...defaultPlanningBudget }, checkpoint: {}, usage: { steps: 0, elapsed_seconds: 0, external_calls: 0, model_tokens: 0, images: 0, route_calls: 0, city_calls: 0, cache_hits: 0 }, result: null, error_code: null, stop_reason: null, created_at: timestamp, updated_at: timestamp });
const showPlans = (...values: Plan[]) => { vi.mocked(api.plans).mockResolvedValue({ items: values, active_plan_id: input.active_plan_id }); };
const completeInput = () => {
  input.missing = [];
  input.origin = { name: '杭州东站', location: [120.1, 30.2], source: 'map', poi_id: null, request_origin: trip.request.origin };
  vi.mocked(api.planningInput).mockResolvedValue(structuredClone(input));
};

const meal = (): MealSupplement => ({ ...input.selected[0], candidate_id: 'meal-one', name: '湖畔餐馆', category: 'eat', poi: { ...input.selected[0].poi, id: 'meal-poi', name: '湖畔餐馆分店' }, meal_window: { period: 'lunch', start_at: '2026-10-15T03:00:00Z', end_at: '2026-10-15T06:00:00Z', anchor_candidate_id: 'candidate-one' }, constraint: { duration_minutes: 45, duration_source: 'estimated', cost_cny: null, cost_source: 'unknown', not_before: '2026-10-15T03:00:00Z', not_after: '2026-10-15T06:00:00Z' }, sources: [{ id: 'meal-source', kind: 'note', title: '餐馆原始笔记', text: '这是当次读取的原文，提到了湖畔餐馆。', read_status: 'success', completeness: 'partial', read_at: timestamp }], evidence: [{ source_id: 'meal-source', quote: '店里环境安静', object_text: '湖畔餐馆', dimension: '环境', stance: 'positive', model_id: 'Agnes', schema_version: 'p2-v1' }] });

it('shows frozen meal sources and an unfilled gap without presenting an unused candidate as an arranged meal', async () => {
  const value = plan(); const candidate = meal();
  value.payload.supplements = [candidate];
  value.payload.unknowns.push({ code: 'meal_gap', message: '午餐窗口尚未排入满足条件的餐馆' });
  showPlans(value);
  render(<Planning session={session} archived={false} />);
  await userEvent.click(await screen.findByText('餐饮补查候选与原文证据（1 项）'));
  const source = within(screen.getByRole('article', { name: '湖畔餐馆补查来源' }));
  expect(source.getByText('这是当次读取的原文，提到了湖畔餐馆。')).toBeTruthy();
  expect(source.getByText('店里环境安静')).toBeTruthy();
  expect(source.getByText(/未列入此方案时间表/)).toBeTruthy();
  expect(screen.getByText('午餐窗口尚未排入满足条件的餐馆')).toBeTruthy();
  expect(screen.queryByRole('group', { name: candidate.name })).toBeNull();
  expect(canvas?.points.some(point => point.id === candidate.poi.id)).toBe(false);
});

it('exposes progress for an adopted arranged meal and preserves estimated duration and unknown fee on completion', async () => {
  const value = plan('adopted-plan'); const candidate = meal();
  input.active_plan_id = value.id; vi.mocked(api.planningInput).mockResolvedValue(structuredClone(input));
  value.payload.supplements = [candidate];
  value.payload.itinerary.push({ kind: 'activity', candidate_id: candidate.candidate_id, name: candidate.name, category: 'eat', poi: candidate.poi, start_at: candidate.meal_window.start_at, end_at: '2026-10-15T03:45:00Z', duration_minutes: 45, duration_source: 'estimated', cost_cny: null, cost_source: 'unknown', required: false, locked: false, state: 'pending' });
  showPlans(value);
  render(<Planning session={session} archived={false} />);
  const group = within(await screen.findByRole('group', { name: candidate.name }));
  expect(group.getByText(/默认停留仍为程序估计/)).toBeTruthy();
  await userEvent.selectOptions(group.getByLabelText('项目进度'), 'completed');
  expect((group.getByLabelText('项目进度') as HTMLSelectElement).disabled).toBe(true);
  await act(async () => { await session.request.flush(); });
  expect(vi.mocked(api.update).mock.calls[0][2].request.planning?.activity_constraints).toContainEqual(expect.objectContaining({ candidate_id: candidate.candidate_id, state: 'completed', duration_minutes: 45, duration_source: 'estimated', cost_cny: null, cost_source: 'unknown', not_before: candidate.meal_window.start_at, not_after: candidate.meal_window.end_at }));
  expect(canvas?.points.some(point => point.id === candidate.poi.id)).toBe(true);
});

it('renders route condition objects and labels historical legs without claiming present execution', async () => {
  const value = plan();
  value.payload.itinerary[0] = { kind: 'route', start_at: timestamp, end_at: timestamp, historical: true, execution_unknown: true, leg: { ...leg(), conditions: [{ code: 'execution_unknown', message: '历史实际交通方式未知，费用和步行保持未知' }] } };
  showPlans(value); render(<Planning session={session} archived={false} />);
  expect(await screen.findByText('历史实际交通方式未知，费用和步行保持未知')).toBeTruthy();
  expect(screen.getByText('原方案历史快照：已跳过或实际交通未确认，不能计作已发生。')).toBeTruthy();
  expect(screen.queryByText('[object Object]')).toBeNull();
});

it('does not label skipped activities or untravelled historical routes as completed, while keeping completed activity history', async () => {
  const value = plan();
  const activity = value.payload.itinerary.find(item => item.kind === 'activity')!;
  value.payload.itinerary = [
    { kind: 'route', start_at: timestamp, end_at: timestamp, historical: true, untravelled: true, leg: leg() },
    { ...activity, candidate_id: 'skipped-place', name: '已跳过的原方案项目', historical: true, state: 'skipped' },
    { ...activity, candidate_id: 'completed-place', name: '实际已完成的历史项目', historical: true, state: 'completed' },
  ];
  showPlans(value); render(<Planning session={session} archived={false} />);
  const timeline = await screen.findByRole('list', { name: '方案 1 时间表' });
  const items = within(timeline).getAllByRole('listitem');
  for (const item of items.slice(0, 2)) {
    expect(within(item).getByText('原方案历史快照：已跳过或实际交通未确认，不能计作已发生。')).toBeTruthy();
    expect(within(item).queryByText('已完成历史段；保留原记录，不能据此确认当前交通。')).toBeNull();
  }
  expect(within(items[1]).getByText('已跳过')).toBeTruthy();
  expect(within(items[2]).getByText('已完成历史段；保留原记录，不能据此确认当前交通。')).toBeTruthy();
  expect(within(items[2]).getByText('已完成')).toBeTruthy();
});

it('shows actual local meal-gap windows and areas and supplied conflict adjustment choices', async () => {
  const value = plan();
  value.payload.unknowns = [{ code: 'meal_gap', message: '午餐仍缺少安排', period: 'lunch', window_start: '2026-10-15T04:30:00Z', window_end: '2026-10-15T05:45:00Z', anchor: { name: '西湖', city: '杭州市' } }];
  value.payload.conflicts = [{ code: 'deadline_conflict', message: '超过结束期限', relaxations: ['减少活动', '明确修改结束期限'] }];
  showPlans(value); render(<Planning session={session} archived={false} />);
  expect(await screen.findByText(/午餐仍缺少安排.*12:30.*13:45.*西湖附近/)).toBeTruthy();
  expect(screen.getByText(/超过结束期限.*可调整：减少活动、明确修改结束期限/)).toBeTruthy();
});

it('saves multiple explicit dietary restrictions and keeps the need for merchant verification visible', async () => {
  render(<Planning session={session} archived={false} />);
  const field = screen.getByLabelText('饮食限制与忌口（每行一项）');
  fireEvent.change(field, { target: { value: '花生过敏\n\n素食' } });
  expect((field as HTMLTextAreaElement).value).toBe('花生过敏\n\n素食');
  fireEvent.blur(field);
  await act(async () => { await session.request.flush(); });
  expect(vi.mocked(api.update).mock.calls[0][2].request.planning?.dietary_restrictions).toEqual(['花生过敏', '素食']);
  expect(screen.getByText(/需要向商家核实，不能据此确认安全/)).toBeTruthy();
});

it('retains invalid dietary edits, blocks generation and resets their draft on trip switching', async () => {
  const view = render(<Planning session={session} archived={false} />);
  fireEvent.change(screen.getByLabelText('饮食限制与忌口（每行一项）'), { target: { value: '字'.repeat(201) } });
  fireEvent.blur(screen.getByLabelText('饮食限制与忌口（每行一项）'));
  expect(screen.getByText(/每项不得超过 200 字/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '生成单日方案' }) as HTMLButtonElement).disabled).toBe(true);
  expect(api.update).not.toHaveBeenCalled();
  const other = new TripSession({ ...trip, id: 'other', request: { ...trip.request, planning: { ...defaultPlanning, dietary_restrictions: ['不吃辣'] } } }, () => {});
  view.rerender(<Planning session={other} archived={false} />);
  expect((screen.getByLabelText('饮食限制与忌口（每行一项）') as HTMLTextAreaElement).value).toBe('不吃辣');
  expect(screen.queryByText(/每项不得超过 200 字/)).toBeNull();
  other.dispose();
});

it('flushes condition edits before planning and repeats the exact operation after an unknown response', async () => {
  session.request.update({ ...session.request.getSnapshot().value, name: '已编辑名称' });
  let finishSave!: (value: Trip) => void;
  vi.mocked(api.update).mockReturnValue(new Promise(resolve => { finishSave = resolve; }));
  vi.spyOn(api, 'startPlanning').mockRejectedValueOnce(new ApiError('响应中断', 0, 'network_error')).mockResolvedValueOnce({ ...planningRun(), request_version: 2 });
  render(<Planning session={session} archived={false} />);
  await fireEvent.click(screen.getByRole('button', { name: '生成单日方案' }));
  expect(api.startPlanning).not.toHaveBeenCalled();
  await act(async () => { finishSave({ ...trip, name: '已编辑名称', request_version: 2 }); });
  await screen.findByRole('button', { name: '重试同一规划任务' });
  await act(async () => { await session.startRun(); });
  expect(session.getRunSnapshot().operation?.kind).toBe('plan');
  await fireEvent.click(screen.getByRole('button', { name: '重试同一规划任务' }));
  await waitFor(() => expect(api.startPlanning).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.startPlanning).mock.calls[0]).toEqual(vi.mocked(api.startPlanning).mock.calls[1]);
  expect(vi.mocked(api.startPlanning).mock.calls[0][2]).toBe(2);
  expect(vi.mocked(api.startPlanning).mock.calls[0][3]).toEqual(defaultPlanningBudget);
});

it('keeps failed condition edits and does not submit planning after saving fails', async () => {
  session.request.update({ ...session.request.getSnapshot().value, name: '本地未保存' });
  vi.mocked(api.update).mockRejectedValue(new ApiError('保存失败', 503, 'database_unavailable'));
  vi.spyOn(api, 'startPlanning');
  await act(async () => { await session.startPlanning(); });
  expect(api.startPlanning).not.toHaveBeenCalled();
  expect(session.request.getSnapshot().value.name).toBe('本地未保存');
  expect(session.getRunSnapshot().operation).toBeNull();
});

it('shows actual travel paths and unknown costs, then adopts only the server-confirmed conditional plan', async () => {
  completeInput();
  const value = plan(); showPlans(value);
  vi.spyOn(api, 'adoptPlan').mockImplementation(async () => {
    input.active_plan_id = value.id;
    vi.mocked(api.planningInput).mockResolvedValue(structuredClone(input));
    return { trip: { ...trip, active_plan_id: value.id }, plan: value };
  });
  render(<Planning session={session} archived={false} />);
  const timeline = within(await screen.findByRole('list', { name: '方案 1 时间表' }));
  expect(timeline.getAllByText(/费用未知/)).toHaveLength(2);
  expect(timeline.getByText(/程序估计/)).toBeTruthy();
  expect(timeline.getByText(/路线形状缺失或不完整/)).toBeTruthy();
  expect(canvas?.routes?.[0].geometry).toEqual(value.payload.itinerary[0].kind === 'route' ? value.payload.itinerary[0].leg.geometry : []);
  expect(screen.queryByText('当前条件已校验；交通预测仍需在出发时核对。')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '采用有条件草案' }));
  await waitFor(() => expect(session.trip.active_plan_id).toBe(value.id));
  expect(api.adoptPlan).toHaveBeenCalledWith('one', value.id, expect.any(String), 1, input.input_hash);
  expect(await screen.findByText('有条件草案 · 当前采用')).toBeTruthy();
});

it('leaves expired or old-condition validated plans as read-only history', async () => {
  const value = plan('expired', 1, 'validated'); value.payload.valid_until = '2000-01-01T00:00:00Z';
  showPlans(value);
  const view = render(<Planning session={session} archived={false} />);
  await screen.findByRole('list', { name: '方案 1 时间表' });
  expect((screen.getByRole('button', { name: '采用已校验方案' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.queryByText('当前条件已校验；交通预测仍需在出发时核对。')).toBeNull();
  expect(screen.getByText(/路线校验有效期已过或未知/)).toBeTruthy();
  const latest = { ...trip, request_version: 2 };
  const other = new TripSession(latest, () => {});
  vi.mocked(api.planningInput).mockResolvedValue({ ...structuredClone(input), request_version: 2, input_hash: 'b'.repeat(64) });
  value.payload.valid_until = new Date(Date.now() + 3600000).toISOString(); showPlans(value);
  view.rerender(<Planning session={other} archived={false} />);
  expect(await screen.findByText(/当前条件或编辑与这份冻结方案不同/)).toBeTruthy();
  expect((screen.getByRole('button', { name: '采用已校验方案' }) as HTMLButtonElement).disabled).toBe(true);
  other.dispose();
});

it('never invents timestamps or offers adoption for a partial schedule', async () => {
  const value = plan('partial', 1, 'partial');
  value.payload.itinerary = [{ kind: 'activity', candidate_id: 'candidate-one', name: '停留时长待补', poi: input.selected[0].poi, category: 'play', start_at: null, end_at: null, duration_minutes: null, duration_source: 'unknown', cost_cny: null, cost_source: 'unknown', required: false, locked: false, state: 'pending' }];
  value.payload.valid_until = null; showPlans(value);
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('list', { name: '方案 1 时间表' });
  expect(screen.getByText('待排定 → 待排定')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /采用.*方案|采用有条件草案/ })).toBeNull();
  expect(canvas).toBeUndefined();
});

it('compares frozen versions without combining their route shapes', async () => {
  const first = plan('one-plan', 1); const second = plan('two-plan', 2);
  second.payload.diff = [{ code: 'route_changed', message: '新版采用另一条交通路线' }];
  showPlans(second, first);
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('list', { name: '方案 2 时间表' });
  expect(canvas?.routes?.map(route => route.id)).toEqual(['two-plan']);
  fireEvent.change(screen.getByLabelText('只读比较另一版本'), { target: { value: first.id } });
  expect(screen.getByLabelText('方案版本比较')).toBeTruthy();
  expect(canvas?.routes?.map(route => route.id)).toEqual(['two-plan']);
  fireEvent.change(screen.getByLabelText('查看方案版本'), { target: { value: first.id } });
  await screen.findByRole('list', { name: '方案 1 时间表' });
  expect(canvas?.routes?.map(route => route.id)).toEqual(['one-plan']);
});

it('retries an uncertain adoption with the original signature and operation id', async () => {
  completeInput();
  const value = plan(); showPlans(value);
  vi.spyOn(api, 'adoptPlan').mockRejectedValueOnce(new ApiError('采用响应中断', 0, 'network_error')).mockResolvedValueOnce({ trip: { ...trip, active_plan_id: value.id }, plan: value });
  render(<Planning session={session} archived={false} />);
  fireEvent.click(await screen.findByRole('button', { name: '采用有条件草案' }));
  await screen.findByRole('button', { name: '重试同一采用请求' });
  expect(session.trip.active_plan_id).toBeUndefined();
  fireEvent.click(screen.getByRole('button', { name: '重试同一采用请求' }));
  await waitFor(() => expect(session.trip.active_plan_id).toBe(value.id));
  expect(vi.mocked(api.adoptPlan).mock.calls[0]).toEqual(vi.mocked(api.adoptPlan).mock.calls[1]);
});

it('keeps edits made while adoption is in flight and ignores mismatched task ownership', async () => {
  completeInput();
  const value = plan();
  let finish!: (result: { trip: Trip; plan: Plan }) => void;
  vi.spyOn(api, 'adoptPlan').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const pending = session.adoptPlan(value);
  await waitFor(() => expect(api.adoptPlan).toHaveBeenCalledTimes(1));
  session.request.update({ ...session.request.getSnapshot().value, name: '采用期间的新编辑' });
  finish({ trip: { ...trip, active_plan_id: value.id }, plan: value }); await pending;
  expect(session.request.getSnapshot().value.name).toBe('采用期间的新编辑');
  expect(session.request.dirty).toBe(true);
  vi.spyOn(api, 'startPlanning').mockResolvedValue({ ...planningRun(), trip_id: 'another-trip' });
  await session.startPlanning();
  expect(session.getRunSnapshot().runs).toEqual([]);
  expect(session.getRunSnapshot().error).toContain('归属不一致');
});

it('uses current planning generations for cancel and resume without silently increasing budget', async () => {
  vi.spyOn(api, 'startPlanning');
  session.acceptRun(planningRun());
  vi.spyOn(api, 'runAction').mockResolvedValueOnce({ ...planningRun('cancelled'), generation: 2, updated_at: '2026-10-15T01:01:00Z' }).mockResolvedValueOnce({ ...planningRun('completed'), stage: 'done', generation: 3, updated_at: '2026-10-15T01:02:00Z' });
  render(<Planning session={session} archived={false} />);
  fireEvent.click(screen.getByRole('button', { name: '取消规划任务' }));
  await screen.findByRole('button', { name: '恢复规划任务' });
  expect(api.runAction).toHaveBeenNthCalledWith(1, 'planning-run', 'cancel', 'one', 1);
  fireEvent.click(screen.getByRole('button', { name: '恢复规划任务' }));
  await screen.findByText('规划结束');
  expect(api.runAction).toHaveBeenNthCalledWith(2, 'planning-run', 'resume', 'one', 2);
  expect(api.startPlanning).not.toHaveBeenCalled();
});

it('refreshes saved plan versions when the shared run poll observes planning completion', async () => {
  session.acceptRun(planningRun());
  const value = plan();
  vi.mocked(api.plans).mockImplementation(async () => ({ items: session.trip.runs[0].status === 'completed' ? [value] : [], active_plan_id: null }));
  vi.spyOn(api, 'getRun').mockResolvedValue({ ...planningRun('completed'), stage: 'done', updated_at: '2026-10-15T01:02:00Z', result: { plan_ids: [value.id], input_hash: input.input_hash, summary: '已保存有条件草案' } });
  render(<><Planning session={session} archived={false} /><Runs session={session} requestVersion={1} archived={false} /></>);
  await screen.findByRole('list', { name: '方案 1 时间表' });
  expect(api.getRun).toHaveBeenCalledWith('planning-run', 'one');
  expect(screen.getByText('已保存有条件草案')).toBeTruthy();
});

it('does not erase a confirmed adoption when an older trip detail arrives with the same condition version', async () => {
  completeInput();
  const value = plan();
  vi.spyOn(api, 'adoptPlan').mockResolvedValue({ trip: { ...trip, active_plan_id: value.id, updated_at: '2026-10-15T01:05:00Z' }, plan: value });
  await session.adoptPlan(value);
  session.accept({ ...trip, active_plan_id: null });
  session.accept({ ...trip, active_plan_id: null, updated_at: '2026-10-15T01:04:00Z' });
  expect(session.trip.active_plan_id).toBe(value.id);
  expect(session.request.getSnapshot().version).toBe(1);
});

it('confirms an earlier adoption retry without restoring it over a later adopted plan', async () => {
  completeInput();
  const previous = plan('previous-plan');
  const latest = plan('latest-plan', 2);
  vi.spyOn(api, 'adoptPlan').mockRejectedValueOnce(new ApiError('原采用响应中断', 0, 'network_error')).mockResolvedValueOnce({ trip: { ...trip, active_plan_id: latest.id, updated_at: '2026-10-15T01:05:00Z' }, plan: previous, operation_applied: true });
  expect(await session.adoptPlan(previous)).toBe(false);
  session.accept({ ...trip, active_plan_id: latest.id, updated_at: '2026-10-15T01:04:00Z' });
  expect(await session.adoptPlan(previous)).toBe(true);
  expect(session.trip.active_plan_id).toBe(latest.id);
  expect(session.getRunSnapshot().planAdoption).toBeNull();
  expect(vi.mocked(api.adoptPlan).mock.calls[0]).toEqual(vi.mocked(api.adoptPlan).mock.calls[1]);
});

it('keeps a mismatched adoption unknown when the server did not confirm the original operation', async () => {
  completeInput();
  const value = plan();
  vi.spyOn(api, 'adoptPlan').mockResolvedValue({ trip: { ...trip, active_plan_id: 'unrelated-plan' }, plan: value });
  expect(await session.adoptPlan(value)).toBe(false);
  expect(session.trip.active_plan_id).toBeUndefined();
  expect(session.getRunSnapshot().planAdoption?.planId).toBe(value.id);
  expect(session.getRunSnapshot().error).toContain('采用结果未确认');
});

it('saves an explicitly supplied position and aware current time as a pair, keeping unknown fields unchanged', async () => {
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  await userEvent.click(screen.getByText('出行进度与当前位置'));
  fireEvent.change(screen.getByLabelText('当前位置名称'), { target: { value: '午饭后的位置' } });
  fireEvent.change(screen.getByLabelText('当前位置城市'), { target: { value: '杭州市' } });
  fireEvent.change(screen.getByLabelText('当前位置经度（GCJ02）'), { target: { value: '120.17' } });
  fireEvent.change(screen.getByLabelText('当前位置纬度（GCJ02）'), { target: { value: '30.26' } });
  fireEvent.click(screen.getByRole('button', { name: '确认当前位置与时间并保存' }));
  await screen.findByText('请提供名称、城市、有效的 GCJ02 坐标与当前日期时间。');
  expect(api.update).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('当前日期与时间（本机时区输入）'), { target: { value: '2026-10-15T12:30' } });
  fireEvent.click(screen.getByRole('button', { name: '确认当前位置与时间并保存' }));
  await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
  const value = vi.mocked(api.update).mock.calls[0][2].request;
  expect(value.planning?.progress_at).toBe(new Date('2026-10-15T12:30').toISOString());
  expect(value.planning?.progress_origin).toMatchObject({ name: '午饭后的位置', city: '杭州市', location: [120.17, 30.26], source: 'map', poi_id: null });
  expect(value.planning?.activity_constraints).toEqual([]);
  expect(value.origin).toBe(trip.request.origin);
});

it('map-picking the current position changes only its local draft until explicitly confirmed', async () => {
  showPlans(plan());
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('list', { name: '方案 1 时间表' });
  await userEvent.click(screen.getByText('出行进度与当前位置'));
  fireEvent.click(screen.getByRole('button', { name: '在方案地图点选当前位置' }));
  fireEvent.click(await screen.findByRole('button', { name: '测试点选当前位置' }));
  expect((screen.getByLabelText('当前位置经度（GCJ02）') as HTMLInputElement).value).toBe('120.17');
  expect((screen.getByLabelText('当前位置纬度（GCJ02）') as HTMLInputElement).value).toBe('30.26');
  expect(session.request.dirty).toBe(false);
  expect(api.update).not.toHaveBeenCalled();
  expect(session.request.getSnapshot().value.request.planning?.progress_origin).toBeUndefined();
});

it('keeps current-position edits after save failure and clears both fields together on explicit clearing', async () => {
  const progress = { name: '确认位置', city: '杭州', location: [120.17, 30.26] as [number, number], source: 'map' as const, poi_id: null, request_origin: null };
  const local = { ...trip, request: { ...trip.request, planning: { ...defaultPlanning, progress_origin: progress, progress_at: timestamp } } };
  session.dispose(); session = new TripSession(local, () => {});
  vi.mocked(api.update).mockRejectedValueOnce(new ApiError('进度保存失败', 503, 'database_unavailable'));
  render(<Planning session={session} archived={false} />);
  await userEvent.click(screen.getByText('出行进度与当前位置'));
  fireEvent.change(screen.getByLabelText('当前位置名称'), { target: { value: '仍保留的编辑位置' } });
  fireEvent.click(screen.getByRole('button', { name: '确认当前位置与时间并保存' }));
  await screen.findByText('进度保存失败');
  expect(session.request.getSnapshot().value.request.planning?.progress_origin?.name).toBe('仍保留的编辑位置');
  expect((screen.getByLabelText('当前位置名称') as HTMLInputElement).value).toBe('仍保留的编辑位置');
  await act(async () => { await session.request.retry(); });
  fireEvent.click(screen.getByRole('button', { name: '清除当前位置与时间' }));
  await waitFor(() => expect(session.request.getSnapshot().value.request.planning?.progress_origin).toBeNull());
  expect(session.request.getSnapshot().value.request.planning?.progress_at).toBeNull();
});

it('requires an adopted parent to mark completion and keeps completed cards read-only', async () => {
  const view = render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  expect((screen.getByRole('option', { name: '已完成' }) as HTMLOptionElement).disabled).toBe(true);
  view.unmount();
  input.active_plan_id = 'active-plan';
  vi.mocked(api.planningInput).mockResolvedValue(structuredClone(input));
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  await userEvent.selectOptions(screen.getByLabelText('项目进度'), 'completed');
  expect((screen.getByLabelText('项目进度') as HTMLSelectElement).disabled).toBe(true);
  expect((screen.getByLabelText('停留分钟').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: '读取已核对的替换候选' })).toBeNull();
  await act(async () => { await session.request.flush(); });
  expect(vi.mocked(api.update).mock.calls[0][2].request.planning?.activity_constraints[0].state).toBe('completed');
});

it('does not silently skip required or locked activities', async () => {
  input.active_plan_id = 'active-plan'; vi.mocked(api.planningInput).mockResolvedValue(structuredClone(input));
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: '西湖散步' });
  fireEvent.click(screen.getByLabelText('必去'));
  expect((screen.getByRole('option', { name: '跳过' }) as HTMLOptionElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText('必去'));
  fireEvent.click(screen.getByLabelText('锁定预约'));
  expect((screen.getByRole('option', { name: '跳过' }) as HTMLOptionElement).disabled).toBe(true);
  expect((screen.getByLabelText('停留分钟').closest('fieldset') as HTMLFieldSetElement).disabled).toBe(true);
  expect(screen.queryByRole('button', { name: '读取已核对的替换候选' })).toBeNull();
});

it('revalidates a historical plan as a new parent-backed run without adopting its old pointer', async () => {
  const old = plan('historical-plan'); old.payload.valid_until = '2000-01-01T00:00:00Z'; showPlans(old);
  vi.spyOn(api, 'startPlanning').mockResolvedValue(planningRun());
  vi.spyOn(api, 'adoptPlan');
  render(<Planning session={session} archived={false} />);
  fireEvent.click(await screen.findByRole('button', { name: '按此版本重新校验' }));
  await waitFor(() => expect(api.startPlanning).toHaveBeenCalledTimes(1));
  expect(api.startPlanning).toHaveBeenCalledWith('one', expect.any(String), 1, defaultPlanningBudget, old.id);
  expect(api.adoptPlan).not.toHaveBeenCalled();
  expect(session.trip.active_plan_id).toBeUndefined();
});

const replacementSnapshot = (): PlanningReplacements => ({ trip_id: 'one', request_version: 1, workspace_version: 1, candidates: [{ candidate_id: 'candidate-two', name: '已核对下午地点', category: 'play', poi: { ...input.selected[0].poi, id: 'poi-two', name: '另一真实地点' } }] });
const replacedTrip = (): Trip => ({ ...trip, workspace_version: 2, workspace: { ...trip.workspace, selected_ids: ['candidate-two'] }, updated_at: '2026-10-15T01:01:00Z' });

it('replaces an optional unfinished selection only after the dedicated server action confirms it', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  vi.spyOn(api, 'replacePlanningCandidate').mockResolvedValue({ trip: replacedTrip(), operation_applied: true });
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(true);
  expect(api.replacePlanningCandidate).toHaveBeenCalledWith('one', { operation_id: expect.any(String), expected_version: 1, expected_workspace_version: 1, input_hash: input.input_hash, candidate_id: 'candidate-one', replacement_id: 'candidate-two' });
  expect(api.workspace).not.toHaveBeenCalled();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-two']);
  expect(session.workspace.getSnapshot().value.map_visible_ids).toEqual(['candidate-one']);
});

it('retains the original replacement operation after an unknown response without faking a local selection save', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  vi.spyOn(api, 'replacePlanningCandidate').mockRejectedValueOnce(new ApiError('替换响应丢失', 0, 'network_error')).mockResolvedValueOnce({ trip: replacedTrip(), operation_applied: true });
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(false);
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-one']);
  expect(session.workspace.dirty).toBe(false);
  expect(session.getRunSnapshot().planningReplacement?.replacement_id).toBe('candidate-two');
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(true);
  expect(vi.mocked(api.replacePlanningCandidate).mock.calls[0]).toEqual(vi.mocked(api.replacePlanningCandidate).mock.calls[1]);
  expect(api.planningReplacements).toHaveBeenCalledTimes(1);
  expect(session.getRunSnapshot().planningReplacement).toBeNull();
});

it('does not claim replacement success from a response that kept the original selection', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  vi.spyOn(api, 'replacePlanningCandidate').mockResolvedValue({ trip: { ...trip, workspace_version: 2 }, operation_applied: true });
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(false);
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-one']);
  expect(session.getRunSnapshot().error).toContain('替换结果未确认');
});

it('blocks replacements of completed, locked, required or fixed-appointment items and unknown replacement POIs', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  vi.spyOn(api, 'replacePlanningCandidate');
  for (const protection of [{ state: 'completed' as const }, { locked: true }, { required: true }, { fixed_start_at: timestamp }]) {
    session.request.update({ ...session.request.getSnapshot().value, request: { ...trip.request, planning: { ...defaultPlanning, activity_constraints: [{ ...newConstraint('candidate-one'), ...protection }] } } });
    expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(false);
  }
  expect(api.planningReplacements).not.toHaveBeenCalled();
  session.request.update({ ...session.request.getSnapshot().value, request: trip.request });
  vi.mocked(api.planningInput).mockImplementation(async () => ({ ...input, request_version: session.request.getSnapshot().version }));
  vi.mocked(api.planningReplacements).mockImplementation(async () => ({ ...replacementSnapshot(), request_version: session.request.getSnapshot().version, candidates: [] }));
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(false);
  expect(api.replacePlanningCandidate).not.toHaveBeenCalled();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-one']);
});

it('drops replacement reads that finish after switching trips', async () => {
  let finish!: (value: PlanningReplacements) => void;
  vi.spyOn(api, 'planningReplacements').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const view = render(<Planning session={session} archived={false} />);
  fireEvent.click(await screen.findByRole('button', { name: '读取已核对的替换候选' }));
  const other = new TripSession({ ...trip, id: 'two' }, () => {});
  vi.mocked(api.planningInput).mockResolvedValue({ ...input, selected: [], selected_ids: [], missing: [{ code: 'none', message: '另一个行程' }] });
  view.rerender(<Planning session={other} archived={false} />);
  await screen.findByText('另一个行程');
  await act(async () => { finish(replacementSnapshot()); });
  expect(screen.queryByRole('option', { name: /已核对下午地点/ })).toBeNull();
  other.dispose();
});

it('does not apply a late replacement response over a newer selection or active plan', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  let finish!: (value: { trip: Trip; operation_applied: boolean }) => void;
  vi.spyOn(api, 'replacePlanningCandidate').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const pending = session.replacePlanningCandidate('candidate-one', 'candidate-two');
  await waitFor(() => expect(api.replacePlanningCandidate).toHaveBeenCalled());
  session.accept({ ...trip, active_plan_id: 'newer-plan', workspace_version: 3, workspace: { ...trip.workspace, selected_ids: ['candidate-three'] }, updated_at: '2026-10-15T01:05:00Z' });
  finish({ trip: replacedTrip(), operation_applied: true });
  expect(await pending).toBe(true);
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-three']);
  expect(session.trip.active_plan_id).toBe('newer-plan');
});

it('confirms a historical replacement retry without replacing the current server selection again', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  const later = { ...trip, active_plan_id: 'later-plan', workspace_version: 3, workspace: { ...trip.workspace, selected_ids: ['candidate-three'] }, updated_at: '2026-10-15T01:05:00Z' };
  vi.spyOn(api, 'replacePlanningCandidate').mockRejectedValueOnce(new ApiError('结果未知', 0, 'network_error')).mockResolvedValueOnce({ trip: later, operation_applied: true });
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(false);
  expect(await session.replacePlanningCandidate('candidate-one', 'candidate-two')).toBe(true);
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-three']);
  expect(session.trip.active_plan_id).toBe('later-plan');
  expect(vi.mocked(api.replacePlanningCandidate).mock.calls[0]).toEqual(vi.mocked(api.replacePlanningCandidate).mock.calls[1]);
});

it('offers the original replacement retry after an unknown response and blocks planning until it is handled', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  vi.spyOn(api, 'replacePlanningCandidate').mockRejectedValueOnce(new ApiError('替换结果未知', 0, 'network_error')).mockResolvedValueOnce({ trip: replacedTrip(), operation_applied: true });
  render(<Planning session={session} archived={false} />);
  fireEvent.click(await screen.findByRole('button', { name: '读取已核对的替换候选' }));
  await screen.findByRole('option', { name: /已核对下午地点/ });
  await userEvent.selectOptions(screen.getByLabelText('替换为另一已确认地点'), 'candidate-two');
  fireEvent.click(screen.getByRole('button', { name: '替换此未完成项目' }));
  const retry = await screen.findByRole('button', { name: '重试同一项目替换' });
  expect((screen.getByRole('button', { name: '生成单日方案' }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(retry);
  await waitFor(() => expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['candidate-two']));
  expect(vi.mocked(api.replacePlanningCandidate).mock.calls[0]).toEqual(vi.mocked(api.replacePlanningCandidate).mock.calls[1]);
  expect(session.workspace.getSnapshot().value.map_visible_ids).toEqual(['candidate-one']);
});

it('preserves viewport edits made during a replacement while accepting only the server selection', async () => {
  vi.spyOn(api, 'planningReplacements').mockResolvedValue(replacementSnapshot());
  let finish!: (value: { trip: Trip; operation_applied: boolean }) => void;
  vi.spyOn(api, 'replacePlanningCandidate').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const pending = session.replacePlanningCandidate('candidate-one', 'candidate-two');
  await waitFor(() => expect(api.replacePlanningCandidate).toHaveBeenCalled());
  session.workspace.update({ ...session.workspace.getSnapshot().value, map_center: [120.18, 30.27], map_zoom: 15 });
  finish({ trip: replacedTrip(), operation_applied: true });
  expect(await pending).toBe(true);
  expect(session.workspace.getSnapshot().value).toMatchObject({ selected_ids: ['candidate-two'], map_visible_ids: ['candidate-one'], map_center: [120.18, 30.27], map_zoom: 15 });
  expect(session.workspace.dirty).toBe(true);
});

it('does not show a position-save error that arrives after switching to another trip', async () => {
  let fail!: (reason: Error) => void;
  vi.mocked(api.update).mockReturnValue(new Promise((_resolve, reject) => { fail = reject; }));
  const view = render(<Planning session={session} archived={false} />);
  await userEvent.click(screen.getByText('出行进度与当前位置'));
  for (const [label, value] of [['当前位置名称', '旧行程位置'], ['当前位置城市', '杭州市'], ['当前位置经度（GCJ02）', '120.17'], ['当前位置纬度（GCJ02）', '30.26'], ['当前日期与时间（本机时区输入）', '2026-10-15T12:30']]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: '确认当前位置与时间并保存' }));
  await waitFor(() => expect(api.update).toHaveBeenCalled());
  const other = new TripSession({ ...trip, id: 'two' }, () => {});
  vi.mocked(api.planningInput).mockResolvedValue({ ...input, selected: [], missing: [{ code: 'missing', message: '另一个行程条件' }] });
  view.rerender(<Planning session={other} archived={false} />);
  await screen.findByText('另一个行程条件');
  await act(async () => { fail(new ApiError('旧行程保存错误', 503, 'database_unavailable')); });
  expect(screen.queryByText('旧行程保存错误')).toBeNull();
  expect((screen.getByLabelText('当前位置名称') as HTMLInputElement).value).toBe('');
  other.dispose();
});

it('preserves a newer confirmed adoption when an earlier adoption response arrives late', async () => {
  completeInput();
  const earlier = plan('earlier-plan');
  let finish!: (result: { trip: Trip; plan: Plan; operation_applied: boolean }) => void;
  vi.spyOn(api, 'adoptPlan').mockReturnValue(new Promise(resolve => { finish = resolve; }));
  const pending = session.adoptPlan(earlier);
  await waitFor(() => expect(api.adoptPlan).toHaveBeenCalled());
  session.accept({ ...trip, active_plan_id: 'newer-plan', updated_at: '2026-10-15T01:05:00Z' });
  finish({ trip: { ...trip, active_plan_id: earlier.id, updated_at: '2026-10-15T01:04:00Z' }, plan: earlier, operation_applied: true });
  expect(await pending).toBe(true);
  expect(session.trip.active_plan_id).toBe('newer-plan');
  expect(session.getRunSnapshot().planAdoption).toBeNull();
});
