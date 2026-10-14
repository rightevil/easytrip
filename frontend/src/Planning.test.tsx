import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from './api';
import { ApiError } from './api';
import type { Run, Trip } from './api';
import { Planning } from './Planning';
import { defaultPlanning, defaultPlanningBudget, newConstraint } from './planning-types';
import type { Plan, PlanningInput, RouteLeg } from './planning-types';
import { TripSession } from './session';
import { Runs } from './Runs';

type CanvasProps = Parameters<typeof import('./AMapCanvas').AMapCanvas>[0];
let canvas: CanvasProps | undefined;
vi.mock('./AMapCanvas', () => ({ AMapCanvas: (props: CanvasProps) => { canvas = props; return <div data-testid="plan-route-canvas" />; } }));

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
