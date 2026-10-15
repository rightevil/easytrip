import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api, ApiError } from './api';
import type { Trip } from './api';
import { MapWorkspace } from './MapWorkspace';
import { ManualPlaces } from './ManualPlaces';
import type { ManualCandidate, ManualPlaceSearch, PlacesSnapshot, Poi } from './places-types';
import { TripSession } from './session';

vi.mock('./AMapCanvas', () => ({ AMapCanvas: () => <div /> }));
const timestamp = '2026-10-15T01:00:00Z';
const trip = (id = 'one'): Trip => ({ id, name: id, status: 'draft', request_version: 1, workspace_version: 1, request: { destination: '杭州市', origin: null, start_at: null, duration_minutes: null, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' }, workspace: { active_tab: 'map', category: 'all', map_center: null, map_zoom: 12, selected_ids: [], map_visible_ids: [], map_initialized: false, map_city: null }, messages: [], runs: [], created_at: timestamp, updated_at: timestamp });
const poi = (index: number): Poi => ({ id: `poi-${index}`, name: `同名咖啡${index}店`, location: [120 + index / 100, 30], address: `核实地址${index}`, city: '杭州市', adcode: '330100', type: '餐饮', entrance_location: null, read_at: timestamp, api_version: 'v5', missing_fields: [] });
const candidate = (id = 'manual-one', tripId = 'one'): ManualCandidate => ({ id, trip_id: tripId, source: 'user', run_id: null, request_version: 1, created_at: timestamp, name: '同名咖啡1店', city: '杭州市', category: 'play', kind: 'place', mention_ids: [], evidence_ids: [], source_ids: [], match_status: 'unique', poi: poi(1) });
const search: ManualPlaceSearch = { search_id: 'search-one', query: '同名咖啡', city: '杭州市', request_version: 1, items: [poi(0), poi(1)] };
let session: TripSession;
let snapshot: PlacesSnapshot;
beforeEach(() => {
  session = new TripSession(trip(), () => {});
  snapshot = { run_id: null, request_version: 1, candidates: [], manual_candidates: [], matches: [], points: [], trip: trip() };
  vi.spyOn(api, 'places').mockImplementation(async () => structuredClone(snapshot));
  vi.spyOn(api, 'searchManualPlace').mockResolvedValue(structuredClone(search));
  vi.spyOn(api, 'confirmManualPlace').mockImplementation(async () => { snapshot.manual_candidates = [candidate()]; return { ...structuredClone(snapshot), candidate_id: 'manual-one' }; });
  vi.spyOn(api, 'mapAction').mockImplementation(async (_id, _version, workspaceVersion, action, options) => {
    const id = options!.candidate_id!;
    if (action === 'add') snapshot.trip.workspace.map_visible_ids.push(id);
    if (action === 'remove') snapshot.trip.workspace.map_visible_ids = snapshot.trip.workspace.map_visible_ids.filter(value => value !== id);
    if (action === 'select') snapshot.trip.workspace.selected_ids.push(id);
    if (action === 'unselect') snapshot.trip.workspace.selected_ids = snapshot.trip.workspace.selected_ids.filter(value => value !== id);
    snapshot.trip.workspace_version = workspaceVersion + 1;
    return structuredClone(snapshot);
  });
  vi.spyOn(api, 'workspace').mockImplementation(async (_id, version, workspace) => { snapshot.trip = { ...snapshot.trip, workspace, workspace_version: version + 1 }; return structuredClone(snapshot.trip); });
  vi.spyOn(api, 'update').mockImplementation(async (_id, version, draft) => { snapshot.trip = { ...snapshot.trip, ...draft, request_version: version + 1 }; return structuredClone(snapshot.trip); });
  vi.spyOn(api, 'startPlanning'); vi.spyOn(api, 'startDiscovery');
});
afterEach(() => { cleanup(); session.dispose(); vi.restoreAllMocks(); });
const savedCard = () => within(screen.getByRole('heading', { name: '同名咖啡1店' }).closest('article')!);
const choose = async () => {
  await userEvent.type(screen.getByLabelText('候选地点名称'), '同名咖啡');
  await userEvent.click(screen.getByRole('button', { name: '搜索候选地点' }));
  await userEvent.click(await screen.findByRole('button', { name: /同名咖啡1店.*核实地址1/ }));
};

it('searches without a discovery run and saves only the explicitly chosen branch with unknown duration and cost', async () => {
  render(<MapWorkspace session={session} archived={false} />);
  expect((screen.getByLabelText('候选地点分类') as HTMLSelectElement).value).toBe('play');
  await choose();
  expect(api.searchManualPlace).toHaveBeenCalledWith('one', '同名咖啡', '杭州市', 1);
  expect(api.confirmManualPlace).not.toHaveBeenCalled();
  expect(screen.getByText(/没有攻略或评价证据；停留时间、营业和费用未知/)).toBeTruthy();
  await userEvent.click(screen.getByRole('button', { name: '确认地点并保存候选' }));
  expect(await screen.findByText(/已保存用户指定候选/)).toBeTruthy();
  expect(vi.mocked(api.confirmManualPlace).mock.calls[0][1]).toMatchObject({ expected_version: 1, expected_workspace_version: 1, search_id: 'search-one', poi_id: 'poi-1', category: 'play' });
  expect(vi.mocked(api.confirmManualPlace).mock.calls[0][1].operation_id).toMatch(/^[\da-f-]{36}$/);
  expect(savedCard().getByText('用户指定')).toBeTruthy();
  expect(savedCard().getByText(/用户手动添加，无攻略或评价证据/)).toBeTruthy();
  expect(savedCard().queryByRole('button', { name: /来源与证据/ })).toBeNull();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual([]);
  expect(session.workspace.getSnapshot().value.map_visible_ids).toEqual([]);
  expect(api.mapAction).not.toHaveBeenCalled(); expect(api.startPlanning).not.toHaveBeenCalled(); expect(api.startDiscovery).not.toHaveBeenCalled();
});

it('keeps a failed confirmation and its UUID across remounts and retries the same request without searching again', async () => {
  vi.mocked(api.confirmManualPlace).mockRejectedValueOnce(new Error('响应中断，保存结果未知'));
  const view = render(<MapWorkspace session={session} archived={false} />);
  await choose();
  await userEvent.selectOptions(screen.getByLabelText('候选地点分类'), 'eat');
  await userEvent.click(screen.getByRole('button', { name: '确认地点并保存候选' }));
  await screen.findByRole('button', { name: '重试同一候选保存' });
  const first = structuredClone(vi.mocked(api.confirmManualPlace).mock.calls[0][1]);
  view.unmount(); render(<MapWorkspace session={session} archived={false} />);
  expect((screen.getByLabelText('候选地点名称') as HTMLInputElement).value).toBe('同名咖啡');
  expect((screen.getByLabelText('候选地点分类') as HTMLSelectElement).value).toBe('eat');
  expect(screen.getByLabelText('候选地点名称').matches(':disabled')).toBe(true);
  vi.mocked(api.confirmManualPlace).mockImplementationOnce(async () => { snapshot.manual_candidates = [{ ...candidate(), category: 'eat' }]; return { ...structuredClone(snapshot), candidate_id: 'manual-one' }; });
  await userEvent.click(screen.getByRole('button', { name: '重试同一候选保存' }));
  await screen.findByText(/已保存用户指定候选/);
  expect(vi.mocked(api.confirmManualPlace).mock.calls[1][1]).toEqual(first);
  expect(api.searchManualPlace).toHaveBeenCalledTimes(1);
  expect(snapshot.manual_candidates).toHaveLength(1);
});

it('keeps map visibility and selection independent and restores a hidden selected manual candidate after reopening', async () => {
  snapshot.manual_candidates = [candidate()];
  const view = render(<MapWorkspace session={session} archived={false} />);
  await screen.findByRole('heading', { name: '同名咖啡1店' });
  await userEvent.click(savedCard().getByRole('button', { name: '加入地图' }));
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual([]);
  await userEvent.click(savedCard().getByRole('button', { name: '加入行程' }));
  await userEvent.click(savedCard().getByRole('button', { name: '移出地图' }));
  expect(session.workspace.getSnapshot().value.map_visible_ids).toEqual([]);
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual(['manual-one']);
  view.unmount(); session.dispose(); session = new TripSession(structuredClone(snapshot.trip), () => {});
  render(<MapWorkspace session={session} archived={false} />);
  await screen.findByRole('heading', { name: '同名咖啡1店' });
  expect(savedCard().getByText('地图未加入 · 行程已选择')).toBeTruthy();
  await userEvent.click(savedCard().getByRole('button', { name: '取消行程选择' }));
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual([]);
  expect(api.mapAction).toHaveBeenLastCalledWith('one', 1, 4, 'unselect', { candidate_id: 'manual-one' });
});

it('retains newly saved manual candidates when an older source run stays on screen', async () => {
  snapshot.run_id = 'history';
  await session.readPlaces('history');
  vi.mocked(api.confirmManualPlace).mockImplementationOnce(async () => ({ ...structuredClone(snapshot), run_id: 'latest', manual_candidates: [candidate()], candidate_id: 'manual-one' }));
  vi.mocked(api.places).mockRejectedValue(new Error('历史来源暂未读到'));
  render(<ManualPlaces session={session} archived={false} />);
  await choose();
  await userEvent.click(screen.getByRole('button', { name: '确认地点并保存候选' }));
  await screen.findByRole('heading', { name: '同名咖啡1店' });
  expect(session.getPlacesSnapshot().snapshot?.run_id).toBe('history');
  expect(session.getPlacesSnapshot().snapshot?.manual_candidates?.map(item => item.id)).toEqual(['manual-one']);
});

it('focuses a shared POI already drawn through a discovery alias without adding or selecting it', async () => {
  snapshot.manual_candidates = [candidate()];
  snapshot.trip.workspace.map_visible_ids = ['discovered-one'];
  snapshot.points = [{ ...poi(1), name: '地图地点', candidate_ids: ['discovered-one', 'manual-one'], activity_ids: ['activity-one'], source_ids: ['source-one'], categories: ['play'] }];
  render(<MapWorkspace session={session} archived={false} />);
  await screen.findByRole('heading', { name: '同名咖啡1店' });
  expect(savedCard().getByText('地图同地点已展示 · 行程未选择')).toBeTruthy();
  await userEvent.click(savedCard().getByRole('button', { name: '定位并高亮地图点' }));
  expect(session.getPlacesSnapshot().focus?.candidate_id).toBe('manual-one');
  expect(api.mapAction).not.toHaveBeenCalled();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual([]);
});

it('isolates a late search and draft when switching trips, preserving the first draft for return', async () => {
  let finish!: (value: ManualPlaceSearch) => void;
  vi.mocked(api.searchManualPlace).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const view = render(<ManualPlaces session={session} archived={false} />);
  await userEvent.type(screen.getByLabelText('候选地点名称'), '同名咖啡');
  await userEvent.click(screen.getByRole('button', { name: '搜索候选地点' }));
  await waitFor(() => expect(api.searchManualPlace).toHaveBeenCalled());
  const other = new TripSession(trip('two'), () => {});
  view.rerender(<ManualPlaces session={other} archived={false} />);
  await act(async () => { finish(structuredClone(search)); });
  expect((screen.getByLabelText('候选地点名称') as HTMLInputElement).value).toBe('');
  expect(screen.queryByRole('button', { name: /同名咖啡1店.*核实地址1/ })).toBeNull();
  view.rerender(<ManualPlaces session={session} archived={false} />);
  expect((screen.getByLabelText('候选地点名称') as HTMLInputElement).value).toBe('同名咖啡');
  expect(screen.getByRole('button', { name: /同名咖啡1店.*核实地址1/ })).toBeTruthy();
  other.dispose();
});

it('discards a late search after the query changes or the session closes', async () => {
  let finish!: (value: ManualPlaceSearch) => void;
  vi.mocked(api.searchManualPlace).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  session.editManualPlace({ query: '同名咖啡' });
  const pending = session.searchManualPlace();
  await waitFor(() => expect(api.searchManualPlace).toHaveBeenCalled());
  session.editManualPlace({ query: '另一个地点' });
  finish(structuredClone(search)); await pending;
  expect(session.getManualPlaceSnapshot().search).toBeNull();
  vi.mocked(api.searchManualPlace).mockClear().mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  const disposed = session.searchManualPlace();
  await waitFor(() => expect(api.searchManualPlace).toHaveBeenCalled());
  session.dispose(); finish({ ...search, query: '另一个地点' }); await disposed;
  expect(session.getManualPlaceSnapshot().search).toBeNull();
});

it('does not apply a manual confirmation after the trip session closes', async () => {
  let finish!: (value: PlacesSnapshot & { candidate_id: string }) => void;
  vi.mocked(api.confirmManualPlace).mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
  session.editManualPlace({ query: '同名咖啡' }); await session.searchManualPlace(); session.editManualPlace({ poiId: 'poi-1' });
  const pending = session.confirmManualPlace();
  await waitFor(() => expect(api.confirmManualPlace).toHaveBeenCalled());
  session.dispose(); finish({ ...snapshot, manual_candidates: [candidate()], candidate_id: 'manual-one' });
  expect(await pending).toBe(false);
  expect(session.getPlacesSnapshot().snapshot).toBeNull();
  expect(session.workspace.getSnapshot().value.selected_ids).toEqual([]);
});

it('keeps the draft when autosave fails and requires a fresh search after saved conditions change', async () => {
  render(<MapWorkspace session={session} archived={false} />);
  await choose();
  const draft = session.request.getSnapshot().value;
  act(() => session.request.update({ ...draft, name: '新名称' }));
  vi.mocked(api.update).mockRejectedValueOnce(new Error('条件尚未保存'));
  await userEvent.click(screen.getByRole('button', { name: '确认地点并保存候选' }));
  await screen.findByText('条件尚未保存', { selector: '.manual-places [role=alert]' });
  expect(api.confirmManualPlace).not.toHaveBeenCalled();
  expect((screen.getByLabelText('候选地点名称') as HTMLInputElement).value).toBe('同名咖啡');
  await act(async () => { await session.request.retry(); });
  expect((screen.getByRole('button', { name: '确认地点并保存候选' }) as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/行程条件已改变，请重新搜索候选地点/)).toBeTruthy();
});

it('preserves local workspace edits and confirmed selections on a CAS failure', async () => {
  snapshot.manual_candidates = [candidate()];
  await session.readPlaces();
  session.editManualPlace({ query: '同名咖啡' }); await session.searchManualPlace(); session.editManualPlace({ poiId: 'poi-1' });
  vi.mocked(api.confirmManualPlace).mockRejectedValue(new ApiError('版本冲突', 409, 'version_conflict'));
  expect(await session.confirmManualPlace()).toBe(false);
  expect(session.getPlacesSnapshot().snapshot?.manual_candidates).toHaveLength(1);
  expect(session.getManualPlaceSnapshot().operation).not.toBeNull();
  expect(session.getManualPlaceSnapshot().poiId).toBe('poi-1');
  expect(session.workspace.getSnapshot().version).toBe(1);
});

it('renders archived candidates read-only and rejects a candidate belonging to another trip', async () => {
  snapshot.manual_candidates = [candidate()];
  render(<MapWorkspace session={session} archived />);
  await screen.findByRole('heading', { name: '同名咖啡1店' });
  expect(screen.getByText('已归档，只读查看用户指定地点。')).toBeTruthy();
  expect(screen.getByLabelText('候选地点名称').matches(':disabled')).toBe(true);
  expect((savedCard().getByRole('button', { name: '加入行程' }) as HTMLButtonElement).disabled).toBe(true);
  snapshot.manual_candidates = [candidate('wrong', 'two')];
  await act(async () => { await session.readPlaces(); });
  expect(session.getPlacesSnapshot().snapshot?.manual_candidates?.[0].id).toBe('manual-one');
  expect(session.getPlacesSnapshot().error).toBe('地点归属不一致，已阻止应用。');
});
