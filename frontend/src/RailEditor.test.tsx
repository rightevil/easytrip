import { useState } from 'react';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { RailEditor } from './RailEditor';
import { defaultPlanning } from './planning-types';
import type { PlanningConstraints, PlanningInput, PlanningPlace, RailSearchResult, RailSegment } from './planning-types';

afterEach(cleanup);
const station = (id: string): PlanningPlace => ({ candidate_id: id, name: id, category: 'play', kind: 'place', city: '杭州市', run_id: null, source: 'user', poi: { id, name: id, city: '杭州市', location: [120, 30], address: `${id}具体地址`, type: '交通设施服务;火车站', adcode: null, entrance_location: null, read_at: '2026-10-15T01:00:00Z', api_version: 'v5', missing_fields: [] }, sources: [], mentions: [], evidence: [], activities: [] });
const input: PlanningInput = { request_version: 1, input_hash: 'a'.repeat(64), request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'walking', transport_source: 'user', budget_cny: null, participants: 1, preferences: '' }, planning: defaultPlanning, origin: null, selected_ids: [], selected: [], missing: [], active_plan_id: null, station_candidates: [station('甲站'), station('乙站')] };

it('keeps a draft unknown until all user-confirmed fields exist, then clears confirmation when a fact changes', async () => {
  const changes = vi.fn();
  function Editor() { const [planning, set] = useState<PlanningConstraints>(structuredClone(defaultPlanning)); return <RailEditor input={input} planning={planning} disabled={false} tripId="trip" requestVersion={1} saved onSearch={vi.fn()} onChange={change => { changes(change); set(value => ({ ...value, ...change })); }} />; }
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
  render(<RailEditor input={input} planning={defaultPlanning} disabled tripId="trip" requestVersion={1} saved onSearch={vi.fn()} onChange={onChange} />);
  expect(screen.getByLabelText('铁路出发日期').matches(':disabled')).toBe(true);
  expect(screen.getByText(/未确认时保存待定草案/)).toBeTruthy();
  expect(onChange).not.toHaveBeenCalled();
});

const rail: RailSegment = { id: 'rail-one', date: '2026-10-15', departure_station_id: '甲站', arrival_station_id: '乙站', train_number: 'G100', departure_at: '2026-10-15T22:00:00+08:00', arrival_at: '2026-10-15T23:00:00+08:00', confirmed: true, source: 'user', boarding_buffer_minutes: 30, arrival_buffer_minutes: 20, cost_cny: 88 };
const searchResult = (): RailSearchResult => ({ source: '12306_mcp', trip_id: 'trip', request_version: 1, date: rail.date, departure_station_id: '甲站', arrival_station_id: '乙站', queried_at: '2026-10-15T09:00:00+08:00', departure_station: { name: '甲', code: 'AAA' }, arrival_station: { name: '乙', code: 'BBB' }, matched_count: 1, excluded_other_station_count: 3, items: [{ train_number: 'D123', departure_at: '2026-10-15T23:45:00+08:00', arrival_at: '2026-10-16T01:15:00+08:00', duration_minutes: 90, seats: [{ name: '二等座', remaining: '有', price_cny: 73 }, { name: '无座', remaining: '无', price_cny: null }] }] });

it('queries real options and selects a cross-midnight draft without confirming it or reusing an old price', async () => {
  const changes = vi.fn();
  const onSearch = vi.fn().mockResolvedValue(searchResult());
  function Editor() {
    const [planning, setPlanning] = useState<PlanningConstraints>({ ...defaultPlanning, rail_segments: [rail] });
    return <RailEditor input={input} planning={planning} disabled={false} tripId="trip" requestVersion={1} saved onSearch={onSearch} onChange={change => { changes(change); setPlanning(value => ({ ...value, ...change })); }} />;
  }
  render(<Editor />);
  await userEvent.click(screen.getByRole('button', { name: '查询 12306 车次与余票' }));
  expect(onSearch).toHaveBeenCalledWith(rail);
  expect(screen.getByText(/跨日到达/)).toBeTruthy();
  expect(screen.getByText(/匹配所选两站 1 班/)).toBeTruthy();
  expect(screen.getByText(/无座：无 · 票价未知/)).toBeTruthy();
  expect(changes).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole('button', { name: '选用 D123 为待确认草稿' }));
  expect(changes.mock.lastCall![0].rail_segments[0]).toMatchObject({ train_number: 'D123', departure_at: searchResult().items[0].departure_at, arrival_at: searchResult().items[0].arrival_at, confirmed: false, source: 'user', cost_cny: null });
  expect(screen.queryByRole('button', { name: '选用 D123 为待确认草稿' })).toBeNull();
  await userEvent.click(screen.getByRole('checkbox'));
  expect(changes.mock.lastCall![0].rail_segments[0].confirmed).toBe(true);
});

it('rejects a late result after station or version changes and blocks duplicate pending queries', async () => {
  let finish!: (value: RailSearchResult) => void;
  const onSearch = vi.fn(() => new Promise<RailSearchResult>(resolve => { finish = resolve; }));
  const onChange = vi.fn();
  const planning = { ...defaultPlanning, rail_segments: [rail] };
  const props = { input, planning, disabled: false, tripId: 'trip', requestVersion: 1, saved: true, onSearch, onChange };
  const view = render(<RailEditor {...props} />);
  await userEvent.click(screen.getByRole('button', { name: '查询 12306 车次与余票' }));
  expect(screen.getByRole('button', { name: '正在查询 12306…' }).matches(':disabled')).toBe(true);
  view.rerender(<RailEditor {...props} planning={{ ...planning, rail_segments: [{ ...rail, date: '2026-10-16' }] }} requestVersion={2} />);
  await act(async () => { finish(searchResult()); });
  expect(screen.queryByRole('button', { name: '选用 D123 为待确认草稿' })).toBeNull();
  expect(onSearch).toHaveBeenCalledTimes(1);
  expect(onChange).not.toHaveBeenCalled();
});

it('hides query choices after a saved version or actual station POI changes', async () => {
  const props = { input, planning: { ...defaultPlanning, rail_segments: [rail] }, disabled: false, tripId: 'trip', requestVersion: 1, saved: true, onSearch: vi.fn().mockResolvedValue(searchResult()), onChange: vi.fn() };
  const view = render(<RailEditor {...props} />);
  await userEvent.click(screen.getByRole('button', { name: '查询 12306 车次与余票' }));
  expect(screen.getByRole('button', { name: '选用 D123 为待确认草稿' })).toBeTruthy();
  view.rerender(<RailEditor {...props} requestVersion={2} />);
  expect(screen.queryByRole('button', { name: '选用 D123 为待确认草稿' })).toBeNull();
  view.rerender(<RailEditor {...props} />);
  const changed = structuredClone(input);
  changed.station_candidates![0].poi.name = '已变更的具体车站';
  view.rerender(<RailEditor {...props} input={changed} />);
  expect(screen.queryByRole('button', { name: '选用 D123 为待确认草稿' })).toBeNull();
});

it('shows service failures and an exact-station empty result without inventing a replacement', async () => {
  const onSearch = vi.fn().mockRejectedValueOnce(new Error('12306 MCP 暂不可用')).mockResolvedValueOnce({ ...searchResult(), items: [], matched_count: 0, excluded_other_station_count: 4 });
  const onChange = vi.fn();
  render(<RailEditor input={input} planning={{ ...defaultPlanning, rail_segments: [rail] }} disabled={false} tripId="trip" requestVersion={1} saved onSearch={onSearch} onChange={onChange} />);
  await userEvent.click(screen.getByRole('button', { name: '查询 12306 车次与余票' }));
  expect(screen.getByRole('alert').textContent).toBe('12306 MCP 暂不可用');
  await userEvent.click(screen.getByRole('button', { name: '查询 12306 车次与余票' }));
  expect(screen.getByText(/上游返回其他车站的车次，本次未返回所选两站车次/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: /选用.*为待确认草稿/ })).toBeNull();
  expect(onChange).not.toHaveBeenCalled();
});

it('keeps archived search controls read only', async () => {
  const onSearch = vi.fn();
  render(<RailEditor input={input} planning={{ ...defaultPlanning, rail_segments: [rail] }} disabled tripId="trip" requestVersion={1} saved onSearch={onSearch} onChange={vi.fn()} />);
  const button = screen.getByRole('button', { name: '查询 12306 车次与余票' });
  expect(button.matches(':disabled')).toBe(true);
  await userEvent.click(button);
  expect(onSearch).not.toHaveBeenCalled();
});
