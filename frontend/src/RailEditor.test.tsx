import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { RailEditor } from './RailEditor';
import { defaultPlanning } from './planning-types';
import type { PlanningConstraints, PlanningInput, PlanningPlace } from './planning-types';

afterEach(cleanup);
const station = (id: string): PlanningPlace => ({ candidate_id: id, name: id, category: 'play', kind: 'place', city: '杭州市', run_id: null, source: 'user', poi: { id, name: id, city: '杭州市', location: [120, 30], address: `${id}具体地址`, type: '交通设施服务;火车站', adcode: null, entrance_location: null, read_at: '2026-10-15T01:00:00Z', api_version: 'v5', missing_fields: [] }, sources: [], mentions: [], evidence: [], activities: [] });
const input: PlanningInput = { request_version: 1, input_hash: 'a'.repeat(64), request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'walking', transport_source: 'user', budget_cny: null, participants: 1, preferences: '' }, planning: defaultPlanning, origin: null, selected_ids: [], selected: [], missing: [], active_plan_id: null, station_candidates: [station('甲站'), station('乙站')] };

it('keeps a draft unknown until all user-confirmed fields exist, then clears confirmation when a fact changes', async () => {
  const changes = vi.fn();
  function Editor() { const [planning, set] = useState<PlanningConstraints>(structuredClone(defaultPlanning)); return <RailEditor input={input} planning={planning} disabled={false} onChange={change => { changes(change); set(value => ({ ...value, ...change })); }} />; }
  render(<Editor />);
  expect(changes).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('铁路出发日期'), { target: { value: '2026-10-15' } });
  await userEvent.click(screen.getByRole('button', { name: '添加待定铁路段' }));
  const group = within(screen.getByRole('group', { name: '铁路段 1 · 2026-10-15' }));
  expect(group.getByRole('checkbox').matches(':disabled')).toBe(true);
  expect(changes.mock.lastCall![0].rail_segments[0]).toMatchObject({ confirmed: false, source: 'user', cost_cny: null, departure_at: null, arrival_at: null });
  await userEvent.selectOptions(group.getByLabelText('出发火车站'), '甲站');
  await userEvent.selectOptions(group.getByLabelText('到达火车站'), '乙站');
  await userEvent.type(group.getByLabelText('用户确认车次'), '用户确认G123');
  fireEvent.change(group.getByLabelText('铁路出发（本机时区输入）'), { target: { value: '2026-10-15T23:00' } });
  fireEvent.change(group.getByLabelText('铁路到达（本机时区输入，跨日填实际日期）'), { target: { value: '2026-10-16T07:00' } });
  await userEvent.click(group.getByRole('checkbox'));
  expect(changes.mock.lastCall![0].rail_segments[0]).toMatchObject({ confirmed: true, departure_at: new Date('2026-10-15T23:00').toISOString(), arrival_at: new Date('2026-10-16T07:00').toISOString() });
  fireEvent.change(group.getByLabelText('进站与候车预留（分钟）'), { target: { value: '40' } });
  expect(changes.mock.lastCall![0].rail_segments[0]).toMatchObject({ confirmed: false, boarding_buffer_minutes: 40 });
  expect(group.getByText(/待确认铁路：不能据此声明能赶上/)).toBeTruthy();
  await userEvent.click(group.getByRole('button', { name: '移除此铁路段' }));
  expect(changes.mock.lastCall![0].rail_segments).toEqual([]);
});

it('renders archived railway fields read-only and never interprets a missing price as zero', () => {
  const onChange = vi.fn();
  render(<RailEditor input={input} planning={defaultPlanning} disabled onChange={onChange} />);
  expect(screen.getByLabelText('铁路出发日期').matches(':disabled')).toBe(true);
  expect(screen.getByText(/未确认时保存待定草案/)).toBeTruthy();
  expect(onChange).not.toHaveBeenCalled();
});
