import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from './api';
import type { Trip } from './api';
import { Planning } from './Planning';
import { defaultPlanning, newConstraint } from './planning-types';
import type { DayPlan, DayWindow, Plan, PlanningInput, PlanningPlace, RouteLeg } from './planning-types';
import { TripSession } from './session';

type CanvasProps = Parameters<typeof import('./AMapCanvas').AMapCanvas>[0];
let canvas: CanvasProps;
vi.mock('./AMapCanvas', () => ({ AMapCanvas: (props: CanvasProps) => { canvas = props; return <div data-testid="daily-map">{props.routes?.map(route => <span key={route.id}>{route.id}</span>)}{props.points.map(point => <span key={point.id}>{point.name}</span>)}</div>; } }));
const timestamp = '2026-10-15T01:00:00Z';
const windowFor = (day: number): DayWindow => ({ date: `2026-10-${day}`, start_at: `2026-10-${day}T01:00:00Z`, end_at: `2026-10-${day}T10:00:00Z` });
const place = (id: string, category: PlanningPlace['category'] = 'play'): PlanningPlace => ({ candidate_id: id, name: category === 'stay' ? '同名酒店' : id, category, kind: 'place', city: '杭州市', run_id: null, source: 'user', poi: { id: `poi-${id}`, name: category === 'stay' ? '同名酒店' : id, location: id === 'hotel-a' ? [120.1, 30.2] : [120.3, 30.4], city: '杭州市', address: `${id}分店具体地址`, adcode: '330100', type: null, entrance_location: null, read_at: timestamp, api_version: 'v5', missing_fields: [] }, activities: [], sources: [], evidence: [], mentions: [] });
const hotelA = place('hotel-a', 'stay'), hotelB = place('hotel-b', 'stay');
let trip: Trip;
let session: TripSession;
let input: PlanningInput;
beforeEach(() => {
  trip = { id: 'one', name: '多日甲', status: 'draft', request_version: 1, workspace_version: 1, request: { destination: '杭州', origin: '杭州东站', start_at: timestamp, duration_minutes: 4000, transport: 'walking', transport_source: 'user', budget_cny: null, participants: 1, preferences: '', planning: structuredClone(defaultPlanning) }, workspace: { active_tab: 'plan', category: 'all', map_center: null, map_zoom: 12, map_visible_ids: [], selected_ids: ['activity-a'] }, messages: [], runs: [], created_at: timestamp, updated_at: timestamp };
  input = { trip_id: 'one', request_version: 1, input_hash: 'a'.repeat(64), request: structuredClone(trip.request), planning: structuredClone(defaultPlanning), origin: { name: '杭州东站', source: 'map', poi_id: null, location: [120, 30], request_origin: '杭州东站' }, selected_ids: ['activity-a'], selected: [place('activity-a')], lodging_candidates: [hotelA, hotelB], lodgings: [], missing: [], active_plan_id: null };
  vi.spyOn(api, 'planningInput').mockImplementation(async () => structuredClone(input));
  vi.spyOn(api, 'plans').mockResolvedValue({ items: [], active_plan_id: null });
  vi.spyOn(api, 'update').mockImplementation(async (_id, version, draft) => { trip = { ...trip, ...draft, request_version: version + 1 }; input = { ...input, request_version: trip.request_version, request: structuredClone(trip.request), planning: structuredClone(trip.request.planning!) }; return structuredClone(trip); });
  vi.spyOn(api, 'workspace').mockImplementation(async (_id, version, workspace) => ({ ...structuredClone(trip), workspace, workspace_version: version + 1 }));
  vi.spyOn(api, 'startPlanning');
  session = new TripSession(structuredClone(trip), () => {});
});
afterEach(() => { cleanup(); session.dispose(); vi.restoreAllMocks(); });
const beginMultiDay = (count = 3) => {
  const planning = { ...defaultPlanning, day_windows: Array.from({ length: count }, (_, i) => windowFor(i + 15)), overnight_stays: [], activity_constraints: [] };
  trip.request.planning = structuredClone(planning); input.planning = structuredClone(planning); input.request = structuredClone(trip.request);
  session.dispose(); session = new TripSession(structuredClone(trip), () => {});
};

it('keeps P4 unchanged until a real date is explicitly added, then preserves empty daily times as unknown', async () => {
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: 'activity-a' });
  expect(screen.getByRole('button', { name: '生成单日方案' })).toBeTruthy();
  expect(screen.queryByLabelText('活动安排日期')).toBeNull();
  expect(api.update).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('首日日期'), { target: { value: '2026-10-15' } });
  await userEvent.click(screen.getByRole('button', { name: '添加首日日窗' }));
  await userEvent.click(screen.getByRole('button', { name: '添加下一日日窗' }));
  await userEvent.click(screen.getByRole('button', { name: '添加下一日日窗' }));
  const windows = session.request.getSnapshot().value.request.planning?.day_windows;
  expect(windows).toEqual([15, 16, 17].map(day => ({ date: `2026-10-${day}`, start_at: null, end_at: null })));
  expect(screen.getAllByLabelText('本晚酒店候选')).toHaveLength(2);
  expect(screen.getByText('2026-10-15 晚缺少已核对酒店，仅显示本晚缺口，不沿用其他晚的选择。')).toBeTruthy();
  expect(screen.getByRole('button', { name: '生成多日方案' })).toBeTruthy();
  await act(async () => { await session.flush(); });
  expect(vi.mocked(api.update).mock.calls[0][2].request.planning?.day_windows).toEqual(windows);
  expect(api.startPlanning).not.toHaveBeenCalled();
});

it('binds different branches independently for each night without adding hotels to selected activities', async () => {
  beginMultiDay();
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: 'activity-a' });
  const first = within(screen.getByRole('group', { name: '2026-10-15 晚住宿' }));
  const second = within(screen.getByRole('group', { name: '2026-10-16 晚住宿' }));
  expect(first.getByRole('option', { name: /hotel-a分店具体地址/ })).toBeTruthy();
  expect(first.getByRole('option', { name: /hotel-b分店具体地址/ })).toBeTruthy();
  await userEvent.selectOptions(first.getByLabelText('本晚酒店候选'), 'hotel-a');
  expect((second.getByLabelText('本晚酒店候选') as HTMLSelectElement).value).toBe('');
  await userEvent.selectOptions(second.getByLabelText('本晚酒店候选'), 'hotel-b');
  await userEvent.selectOptions(screen.getByLabelText('活动安排日期'), '2026-10-16');
  await act(async () => { await session.flush(); });
  expect(session.request.getSnapshot().value.request.planning).toMatchObject({ overnight_stays: [{ date: '2026-10-15', candidate_id: 'hotel-a' }, { date: '2026-10-16', candidate_id: 'hotel-b' }], activity_constraints: [{ candidate_id: 'activity-a', date: '2026-10-16', duration_minutes: null, cost_cny: null }] });
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['activity-a']);
  expect(screen.getAllByText(/选择只绑定这一晚，不代表已预订/)).toHaveLength(2);
  await userEvent.selectOptions(first.getByLabelText('本晚酒店候选'), '');
  expect(session.request.getSnapshot().value.request.planning?.overnight_stays).toEqual([{ date: '2026-10-16', candidate_id: 'hotel-b' }]);
});

it('saves and clears explicit daily times without making up overnight check-in or checkout rules', async () => {
  beginMultiDay(2);
  render(<Planning session={session} archived={false} />);
  const day = within(await screen.findByRole('group', { name: '2026-10-15 日窗' }));
  fireEvent.change(day.getByLabelText('当日开始（本机时区输入）'), { target: { value: '2026-10-15T10:00' } });
  const saved = session.request.getSnapshot().value.request.planning?.day_windows?.[0].start_at;
  expect(saved).toBe(new Date('2026-10-15T10:00').toISOString());
  fireEvent.change(day.getByLabelText('当日结束（本机时区输入）'), { target: { value: '' } });
  await act(async () => { await session.flush(); });
  expect(session.request.getSnapshot().value.request.planning?.day_windows?.[0].end_at).toBeNull();
  expect(day.getByText('2026-10-15 日窗时间未知，补齐前保留草案。')).toBeTruthy();
  expect(session.request.getSnapshot().value.request.planning).not.toHaveProperty('check_in_at');
  expect(session.request.getSnapshot().value.request.planning).not.toHaveProperty('check_out_at');
});

it('removes the final date and only its activity assignment and obsolete hotel binding in one save', async () => {
  beginMultiDay();
  const draft = session.request.getSnapshot().value;
  session.request.update({ ...draft, request: { ...draft.request, planning: { ...draft.request.planning!, overnight_stays: [{ date: '2026-10-15', candidate_id: 'hotel-a' }, { date: '2026-10-16', candidate_id: 'hotel-b' }], activity_constraints: [{ ...newConstraint('activity-a'), date: '2026-10-17', duration_minutes: 90 }] } } });
  render(<Planning session={session} archived={false} />);
  await userEvent.click(screen.getByRole('button', { name: '移除末日日窗及其日期分配和失效住宿绑定' }));
  expect(session.request.getSnapshot().value.request.planning).toMatchObject({ day_windows: [windowFor(15), windowFor(16)], overnight_stays: [{ date: '2026-10-15', candidate_id: 'hotel-a' }], activity_constraints: [{ candidate_id: 'activity-a', date: null, duration_minutes: 90 }] });
});

it('retains multi-day edits after an autosave failure and restores saved windows and nights after reopening', async () => {
  beginMultiDay(2);
  const view = render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: 'activity-a' });
  await userEvent.selectOptions(screen.getByLabelText('本晚酒店候选'), 'hotel-a');
  await userEvent.selectOptions(screen.getByLabelText('活动安排日期'), '2026-10-15');
  vi.mocked(api.update).mockRejectedValueOnce(new Error('磁盘保存失败'));
  await act(async () => { await session.flush().catch(() => {}); });
  expect(screen.getByText(/规划条件尚未保存/)).toBeTruthy();
  view.unmount(); render(<Planning session={session} archived={false} />);
  expect((screen.getByLabelText('本晚酒店候选') as HTMLSelectElement).value).toBe('hotel-a');
  await act(async () => { await session.request.retry(); });
  cleanup(); session.dispose(); session = new TripSession(structuredClone(trip), () => {});
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: 'activity-a' });
  expect((screen.getByLabelText('活动安排日期') as HTMLSelectElement).value).toBe('2026-10-15');
  expect((screen.getByLabelText('本晚酒店候选') as HTMLSelectElement).value).toBe('hotel-a');
  expect(screen.getByRole('group', { name: '2026-10-16 日窗' })).toBeTruthy();
});

const route = (day: number): RouteLeg => ({ id: `route-${day}`, mode: 'walking', origin: [120, 30], destination: [120.1, 30.2], departure_at: `2026-10-${day}T01:00:00Z`, read_at: timestamp, valid_until: '2099-10-15T01:00:00Z', api_version: 'v5', status: 'ok', duration_seconds: 600, distance_meters: 800, walking_distance_meters: 800, geometry: [[[120, 30], [120.1, 30.2]]], geometry_complete: true, cost_cny: null, cost_source: 'unknown', duration_source: 'amap', conditional: false, conditions: [] });
const dailyPlan = (count = 3): Plan => {
  const frozen = structuredClone(input); frozen.lodgings = [hotelA, hotelB]; frozen.planning.day_windows = Array.from({ length: count }, (_, i) => windowFor(i + 15)); frozen.planning.overnight_stays = [{ date: '2026-10-15', candidate_id: 'hotel-a' }, ...(count === 3 ? [{ date: '2026-10-16', candidate_id: 'hotel-b' }] : [])];
  const originFor = (hotel: PlanningPlace) => ({ name: hotel.name, city: hotel.city, source: 'amap' as const, poi_id: hotel.poi.id, location: hotel.poi.location, request_origin: null });
  const days: DayPlan[] = Array.from({ length: count }, (_, i) => ({ ...windowFor(i + 15), origin: i === 0 ? frozen.origin : originFor(i === 1 ? hotelA : hotelB), endpoint: originFor(i === 0 ? hotelA : hotelB), lodging_candidate_id: i < count - 1 ? i === 0 ? 'hotel-a' : 'hotel-b' : null, itinerary: [{ kind: 'route', date: `2026-10-${i + 15}`, start_at: `2026-10-${i + 15}T01:00:00Z`, end_at: `2026-10-${i + 15}T01:10:00Z`, leg: route(i + 15) }], unknowns: [{ code: 'lodging_price_unknown', date: `2026-10-${i + 15}`, message: `第${i + 1}日费用未知` }], conflicts: [] }));
  return { id: 'multi-plan', trip_id: 'one', version: 1, request_version: 1, input_hash: input.input_hash, parent_plan_id: null, run_id: null, status: 'conditional', created_at: timestamp, payload: { input: frozen, days, itinerary: days.flatMap(day => day.itinerary), unknowns: [], conflicts: [], usage: {}, diff: [], valid_until: '2099-10-15T01:00:00Z' } };
};

it('shows three saved days independently and draws only the selected date with its hotel origin', async () => {
  const value = dailyPlan();
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  const first = within(await screen.findByRole('region', { name: '2026-10-15 日计划' }));
  expect(first.getByText(/hotel-a分店具体地址/)).toBeTruthy();
  expect(first.getByText(/用户指定，无攻略或评价证据/)).toBeTruthy();
  expect(screen.getByRole('list', { name: '方案 1 · 2026-10-15 时间表' })).toBeTruthy();
  expect(canvas.routes?.map(leg => leg.id)).toEqual(['route-15']);
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-16');
  expect(screen.getByRole('region', { name: '2026-10-16 日计划' })).toBeTruthy();
  expect(canvas.routes?.map(leg => leg.id)).toEqual(['route-16']);
  expect(canvas.points.find(point => point.origin)?.name).toBe('同名酒店');
  expect(canvas.points.some(point => point.name === '杭州东站')).toBe(false);
  expect(screen.queryByRole('list', { name: '方案 1 · 2026-10-15 时间表' })).toBeNull();
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-17');
  expect(canvas.routes?.map(leg => leg.id)).toEqual(['route-17']);
  expect(screen.getByRole('region', { name: '2026-10-17 日计划' })).toBeTruthy();
});

it('keeps a missing night and the next unknown origin visible instead of falling back to the original trip origin', async () => {
  const value = dailyPlan(2); value.status = 'partial';
  value.payload.days![0].lodging_candidate_id = null;
  value.payload.days![0].unknowns = [{ code: 'lodging_gap', date: '2026-10-15', message: '10月15日晚酒店缺口' }];
  value.payload.days![1].origin = null; value.payload.days![1].itinerary = [];
  value.payload.days![1].unknowns = [{ code: 'day_origin_unknown', message: '前一晚酒店未明确，接驳未知' }];
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  expect(await screen.findByText('2026-10-15 晚缺少已核对酒店；翌日起点与住宿接驳仍未知。')).toBeTruthy();
  expect(screen.getByText('2026-10-15 · 10月15日晚酒店缺口')).toBeTruthy();
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-16');
  expect(screen.getByText(/当日出发：未知，不能确认接驳/)).toBeTruthy();
  expect(screen.getByText('前一晚酒店未明确，接驳未知')).toBeTruthy();
  expect(screen.queryByTestId('daily-map')).toBeNull();
  expect(screen.queryByRole('button', { name: /采用/ })).toBeNull();
});

it('preserves source evidence for the exact nightly hotel without implying availability or booking', async () => {
  const value = dailyPlan(2);
  value.payload.input.lodgings![0] = { ...hotelA, source: undefined, sources: [{ id: 'hotel-source', title: '酒店原始笔记', text: '原文只描述了这家酒店的位置。' }], evidence: [{ quote: '附近能步行到湖边' }] };
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  await screen.findByText('本晚酒店来源与证据');
  expect(screen.getByText('酒店原始笔记')).toBeTruthy();
  expect(screen.getByText('原文只描述了这家酒店的位置。')).toBeTruthy();
  expect(screen.getByText('附近能步行到湖边')).toBeTruthy();
  expect(screen.getByText(/房态、当晚房价、入住、退房与行李寄存未知/)).toBeTruthy();
  expect(screen.queryByText(/^已预订$/)).toBeNull();
});

it('opens older P4 plans without days and makes archived multi-day fields read-only', async () => {
  beginMultiDay(2);
  const value = dailyPlan(2); delete value.payload.days;
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived />);
  await screen.findByRole('list', { name: '方案 1 时间表' });
  expect(screen.queryByLabelText('查看日计划')).toBeNull();
  expect(screen.getByRole('button', { name: '添加下一日日窗' }).matches(':disabled')).toBe(true);
  expect(screen.getByLabelText('本晚酒店候选').matches(':disabled')).toBe(true);
  expect(screen.getByLabelText('活动安排日期').matches(':disabled')).toBe(true);
});

it('binds a dated lodging suggestion explicitly, preserving the other night and sources without starting planning', async () => {
  beginMultiDay();
  const value = dailyPlan(); value.status = 'partial';
  value.payload.supplements = [hotelA, hotelB].map((hotel, index) => ({ ...hotel, category: 'stay', sources: [{ title: '往年入住记录', text: '去年房价不表示今日可订。' }], lodging_window: { date: `2026-10-${15 + index}`, city: '杭州市', anchor_name: '当日活动', anchor_location: [120.1, 30.2], anchor_candidate_id: 'activity-a' } }));
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  const first = within(await screen.findByRole('region', { name: '2026-10-15 晚住宿建议' }));
  expect(first.getByText(/历史价格不是当晚可订房价/)).toBeTruthy();
  expect(first.getByText('去年房价不表示今日可订。')).toBeTruthy();
  await userEvent.click(first.getByRole('button', { name: '选择为这一晚酒店' }));
  await act(async () => { await session.flush(); });
  expect(session.request.getSnapshot().value.request.planning?.overnight_stays).toEqual([{ date: '2026-10-15', candidate_id: 'hotel-a' }]);
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-16');
  const second = within(await screen.findByRole('region', { name: '2026-10-16 晚住宿建议' }));
  await userEvent.click(second.getByRole('button', { name: '选择为这一晚酒店' }));
  await act(async () => { await session.flush(); });
  expect(session.request.getSnapshot().value.request.planning?.overnight_stays).toEqual([{ date: '2026-10-15', candidate_id: 'hotel-a' }, { date: '2026-10-16', candidate_id: 'hotel-b' }]);
  expect(api.startPlanning).not.toHaveBeenCalled();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['activity-a']);
});

it('keeps lodging suggestions read-only when their exact POI is unavailable', async () => {
  beginMultiDay(2);
  input.lodging_candidates = [];
  const value = dailyPlan(2); value.payload.supplements = [{ ...hotelA, category: 'stay', lodging_window: { date: '2026-10-15', city: '杭州市', anchor_name: '公园', anchor_location: [120.1, 30.2], anchor_candidate_id: 'activity-a' } }];
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  const region = within(await screen.findByRole('region', { name: '2026-10-15 晚住宿建议' }));
  expect(region.getByRole('button', { name: '选择为这一晚酒店' }).matches(':disabled')).toBe(true);
  expect(api.update).not.toHaveBeenCalled();
});

it('shows cross-midnight railway separately from city geometry and does not invent a hotel night', async () => {
  const value = dailyPlan(2);
  const rail = { kind: 'rail' as const, rail_id: 'night-train', train_number: '用户确认夜车', start_at: '2026-10-15T15:00:00Z', end_at: '2026-10-15T23:00:00Z', departure_station: place('出发站'), arrival_station: place('到达站'), source: 'user' as const, confirmed: true, boarding_buffer_minutes: 30, arrival_buffer_minutes: 20, cost_cny: null, cost_source: 'unknown' as const };
  value.payload.days![0].overnight_rail_id = rail.rail_id;
  value.payload.days![0].lodging_candidate_id = null;
  value.payload.days![0].itinerary.push(rail);
  value.payload.itinerary = value.payload.days!.flatMap(day => day.itinerary);
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  expect(await screen.findByText('铁路 · 用户确认夜车')).toBeTruthy();
  expect(screen.getByText(/本晚为跨日铁路在途：night-train/)).toBeTruthy();
  expect(screen.queryByText('2026-10-15 晚缺少已核对酒店；翌日起点与住宿接驳仍未知。')).toBeNull();
  expect(screen.getByText(/铁路段独立于高德市内路线/)).toBeTruthy();
  expect(canvas.routes?.map(leg => leg.id)).toEqual(['route-15']);
  expect(canvas.points.some(point => point.name === '出发站')).toBe(true);
  expect(canvas.points.some(point => point.name === '到达站')).toBe(true);
});

it('shows a resumed location only on its own local date without replacing the saved daily origin', async () => {
  const value = dailyPlan();
  value.payload.input.planning.progress_at = '2026-10-16T02:00:00Z';
  value.payload.input.planning.progress_origin = { name: '用户确认当前位置', city: '杭州市', location: [120.2, 30.3], source: 'map', poi_id: null, request_origin: null };
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('region', { name: '2026-10-15 日计划' });
  expect(canvas.points.some(point => point.id === 'plan-progress-origin')).toBe(false);
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-16');
  expect(canvas.points.some(point => point.id === 'plan-progress-origin' && point.location[0] === 120.2)).toBe(true);
  expect(canvas.points.find(point => point.origin)?.name).toBe('同名酒店');
  expect(screen.getByText(/本日恢复从用户确认当前位置：用户确认当前位置/)).toBeTruthy();
  await userEvent.selectOptions(screen.getByLabelText('查看日计划'), '2026-10-17');
  expect(canvas.points.some(point => point.id === 'plan-progress-origin')).toBe(false);
});

it('does not add another marker when the daily endpoint is already a scheduled POI', async () => {
  const value = dailyPlan(2);
  const activity = place('activity-a');
  value.payload.days![0].itinerary.push({ ...newConstraint('activity-a'), date: '2026-10-15', kind: 'activity', name: activity.name, category: 'play', poi: activity.poi, start_at: '2026-10-15T01:10:00Z', end_at: '2026-10-15T01:40:00Z' });
  value.payload.days![0].endpoint = { name: activity.name, city: activity.city, source: 'amap', poi_id: activity.poi.id, location: activity.poi.location, request_origin: null };
  value.payload.itinerary = value.payload.days!.flatMap(day => day.itinerary);
  vi.mocked(api.plans).mockResolvedValue({ items: [value], active_plan_id: null });
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('region', { name: '2026-10-15 日计划' });
  expect(canvas.points.filter(point => point.id === activity.poi.id)).toHaveLength(1);
  expect(canvas.points.some(point => point.id === 'day-endpoint')).toBe(false);
});

it('saves user-known hotel times and nightly total for only that night and clears them when the branch changes', async () => {
  beginMultiDay();
  render(<Planning session={session} archived={false} />);
  await screen.findByRole('group', { name: 'activity-a' });
  const first = within(screen.getByRole('group', { name: '2026-10-15 晚住宿' }));
  await userEvent.selectOptions(first.getByLabelText('本晚酒店候选'), 'hotel-a');
  fireEvent.change(first.getByLabelText('用户确认的最早入住（本机时区输入）'), { target: { value: '2026-10-15T14:00' } });
  fireEvent.change(first.getByLabelText('用户确认的最晚退房（本机时区输入）'), { target: { value: '2026-10-16T10:00' } });
  fireEvent.change(first.getByLabelText('用户确认本晚总费用（元，空值未知）'), { target: { value: '100' } });
  await act(async () => { await session.flush(); });
  expect(session.request.getSnapshot().value.request.planning?.overnight_stays).toEqual([{ date: '2026-10-15', candidate_id: 'hotel-a', check_in_not_before: new Date('2026-10-15T14:00').toISOString(), check_out_by: new Date('2026-10-16T10:00').toISOString(), nightly_cost_cny: 100 }]);
  await userEvent.selectOptions(first.getByLabelText('本晚酒店候选'), 'hotel-b');
  expect(session.request.getSnapshot().value.request.planning?.overnight_stays).toEqual([{ date: '2026-10-15', candidate_id: 'hotel-b' }]);
  expect(api.startPlanning).not.toHaveBeenCalled();
});
