import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from './api';
import type { Trip } from './api';
import { Planning } from './Planning';
import { defaultPlanning, newConstraint } from './planning-types';
import type { PlanningInput } from './planning-types';
import { TripSession } from './session';

const timestamp = '2026-10-15T01:00:00Z';
const trip: Trip = { id: 'one', name: '规划甲', status: 'draft', request: { destination: '杭州', origin: '杭州东站', start_at: timestamp, duration_minutes: 360, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' }, request_version: 1, workspace: { active_tab: 'plan', category: 'all', map_center: null, map_zoom: 12, map_visible_ids: ['candidate-one'], selected_ids: ['candidate-one'] }, workspace_version: 1, messages: [], runs: [], created_at: timestamp, updated_at: timestamp };
let session: TripSession;
let input: PlanningInput;
beforeEach(() => {
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
