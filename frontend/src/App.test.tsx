import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, ApiError } from './api';
import type { Message, Run, Trip } from './api';
import { defaultDiscoveryBudget } from './discovery-types';

const trip = (id: string, name: string): Trip => ({
  id, name, status: 'draft', request_version: 1, workspace_version: 1,
  request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' },
  workspace: { active_tab: 'overview', category: 'all', map_center: null, map_zoom: 12, selected_ids: [], map_visible_ids: [] },
  created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z', messages: [], runs: [],
});
vi.mock('./AMapCanvas', () => ({ AMapCanvas: () => <div data-testid="candidate-map" /> }));
let saved: Map<string, Trip>;
beforeEach(() => {
  localStorage.clear();
  saved = new Map([['one', trip('one', '第一份行程')], ['two', trip('two', '第二份行程')]]);
  vi.spyOn(api, 'list').mockImplementation(async () => ({ items: [...saved.values()], total: saved.size, offset: 0, limit: 50 }));
  vi.spyOn(api, 'get').mockImplementation(async id => structuredClone(saved.get(id)!));
  vi.spyOn(api, 'places').mockImplementation(async id => ({ trip: structuredClone(saved.get(id)!), run_id: null, request_version: saved.get(id)!.request_version, candidates: [], matches: [], points: [] }));
  vi.spyOn(api, 'update').mockImplementation(async (id, version, draft) => {
    const next = { ...saved.get(id)!, ...structuredClone(draft), request_version: version + 1 };
    saved.set(id, next); return structuredClone(next);
  });
  vi.spyOn(api, 'workspace').mockImplementation(async (id, version, workspace) => {
    const next = { ...saved.get(id)!, workspace: structuredClone(workspace), workspace_version: version + 1 };
    saved.set(id, next); return structuredClone(next);
  });
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
async function openApp() { render(<App />); await screen.findByRole('heading', { name: '第一份行程' }); await userEvent.click(screen.getByText('编辑行程条件')); }

describe('real workspace behaviors', () => {
  it('starts with a compact condition summary and places beside the map, retaining edits when collapsed', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: '第一份行程' });
    const editor = screen.getByText('编辑行程条件').closest('details')!;
    expect(editor.open).toBe(false);
    expect(screen.getByRole('region', { name: '当前行程条件摘要' })).toBeTruthy();
    expect(screen.getByRole('complementary', { name: '候选地点地图' })).toBeTruthy();
    expect(screen.getByTestId('candidate-map')).toBeTruthy();
    await userEvent.click(screen.getByText('编辑行程条件'));
    expect(editor.open).toBe(true);
    fireEvent.change(screen.getByLabelText(/从哪里出发/), { target: { value: '杭州东站' } });
    await userEvent.click(screen.getByText('编辑行程条件'));
    expect(editor.open).toBe(false);
    expect(screen.getByRole('region', { name: '当前行程条件摘要' }).textContent).toContain('杭州东站');
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    expect(api.update).toHaveBeenCalledWith('one', 1, expect.objectContaining({ request: expect.objectContaining({ origin: '杭州东站' }) }));
    expect(screen.getByText('编辑行程条件').closest('details')?.open).toBe(false);
  });
  it('saves conditions before switching and restores each trip independently', async () => {
    await openApp();
    fireEvent.change(screen.getByLabelText(/从哪里出发/), { target: { value: '杭州东站' } });
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    await userEvent.click(screen.getByText('编辑行程条件'));
    expect((screen.getByLabelText(/从哪里出发/) as HTMLInputElement).value).toBe('');
    expect(api.update).toHaveBeenCalledWith('one', 1, expect.objectContaining({ request: expect.objectContaining({ origin: '杭州东站', destination: null }) }));
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
    await userEvent.click(screen.getByText('编辑行程条件'));
    expect((screen.getByLabelText(/从哪里出发/) as HTMLInputElement).value).toBe('杭州东站');
  });

  it('retains failed local edits and requires explicit handling before a switch', async () => {
    vi.mocked(api.update).mockRejectedValueOnce(new ApiError('磁盘写失败', 503, 'database_unavailable'));
    await openApp();
    fireEvent.change(screen.getByLabelText(/目的地/), { target: { value: '南京' } });
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    expect(await screen.findByRole('heading', { name: '当前编辑还未保存' })).toBeTruthy();
    expect((screen.getByLabelText(/目的地/) as HTMLInputElement).value).toBe('南京');
    expect(screen.getByRole('heading', { name: '第一份行程' })).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '重试保存后继续' }));
    await screen.findByRole('heading', { name: '第二份行程' });
    expect(saved.get('one')?.request.destination).toBe('南京');
  });

  it('never archives after failed saving and keeps the local draft', async () => {
    vi.mocked(api.update).mockRejectedValueOnce(new ApiError('磁盘写失败', 503, 'database_unavailable'));
    vi.spyOn(api, 'archive').mockResolvedValue({ ...trip('one', '第一份行程'), status: 'archived' });
    await openApp();
    fireEvent.change(screen.getByLabelText(/目的地/), { target: { value: '南京未保存' } });
    await userEvent.click(screen.getByRole('button', { name: '归档' }));
    await screen.findByRole('heading', { name: '当前编辑还未保存' });
    expect(screen.queryByRole('button', { name: '保留本地编辑并继续' })).toBeNull();
    expect(api.archive).not.toHaveBeenCalled();
    expect((screen.getByLabelText(/目的地/) as HTMLInputElement).value).toBe('南京未保存');
  });

  it('restores the remembered trip even when it is outside the first page', async () => {
    const outside = trip('fifty-one', '分页外行程'); saved.set(outside.id, outside);
    localStorage.setItem('easytrip.active', outside.id);
    vi.mocked(api.list).mockResolvedValue({ items: [saved.get('one')!, saved.get('two')!], total: 51, offset: 0, limit: 50 });
    render(<App />);
    await screen.findByRole('heading', { name: '分页外行程' });
    expect(api.get).toHaveBeenCalledWith('fifty-one');
    expect(screen.getByRole('button', { name: /分页外行程/ })).toBeTruthy();
    expect(api.get).not.toHaveBeenCalledWith('one');
  });

  it('reapplies conflict edits against the latest version without discarding the draft', async () => {
    vi.mocked(api.update).mockRejectedValueOnce(new ApiError('版本冲突', 409, 'version_conflict', 5));
    await openApp();
    fireEvent.change(screen.getByLabelText(/目的地/), { target: { value: '成都' } });
    await screen.findByText('行程条件发生版本冲突');
    saved.set('one', { ...saved.get('one')!, request_version: 5 });
    await userEvent.click(screen.getByRole('button', { name: '重新保存本地编辑' }));
    await waitFor(() => expect(api.update).toHaveBeenLastCalledWith('one', 5, expect.objectContaining({ request: expect.objectContaining({ destination: '成都' }) })));
    expect((screen.getByLabelText(/目的地/) as HTMLInputElement).value).toBe('成都');
  });

  it('saves views separately and restores them on reopening', async () => {
    await openApp();
    await userEvent.click(screen.getByRole('tab', { name: '地点与地图' }));
    await userEvent.click(screen.getByRole('button', { name: '餐饮' }));
    await userEvent.click(screen.getByText('预设地图视野'));
    fireEvent.change(screen.getByLabelText('经度'), { target: { value: '120.15' } });
    fireEvent.change(screen.getByLabelText('纬度'), { target: { value: '30.25' } });
    await userEvent.click(screen.getByRole('button', { name: '保存视野' }));
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    expect(api.update).not.toHaveBeenCalled();
    cleanup(); localStorage.setItem('easytrip.active', 'one'); render(<App />);
    await screen.findByRole('heading', { name: '第一份行程' });
    expect(screen.getByRole('tab', { name: '地点与地图' }).getAttribute('aria-selected')).toBe('true');
    expect(screen.getByRole('button', { name: '餐饮' }).getAttribute('aria-pressed')).toBe('true');
    expect(saved.get('one')?.workspace.map_center).toEqual([120.15, 30.25]);
  });

  it('retries message saving with the same operation id and displays only saved messages', async () => {
    const sent = { id: 'message', trip_id: 'one', role: 'user', content: '想看秋色', status: 'pending', created_at: '2026-10-12T00:00:00Z' };
    vi.spyOn(api, 'message').mockRejectedValueOnce(new ApiError('网络故障', 0, 'network_error')).mockResolvedValueOnce(sent);
    await openApp();
    fireEvent.change(screen.getByLabelText('行程想法'), { target: { value: '想看秋色' } });
    await userEvent.click(screen.getByRole('button', { name: '保存想法' }));
    await screen.findByRole('button', { name: '重试保存' });
    expect(screen.queryByText('想看秋色', { selector: '.chat-message p' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '重试保存' }));
    await screen.findByText('想看秋色', { selector: '.chat-message p' });
    expect(vi.mocked(api.message).mock.calls[0]).toEqual(vi.mocked(api.message).mock.calls[1]);
    expect(screen.getByText('待处理')).toBeTruthy();
  });

  it('ignores a response for the first trip after the view changed', async () => {
    let complete!: (trip: Trip) => void;
    const pending = new Promise<Trip>(resolve => { complete = resolve; });
    vi.spyOn(api, 'message').mockImplementation(async () => { await pending; return { id: 'later', trip_id: 'one', role: 'user', content: '第一份的想法', status: 'pending', created_at: '2026-10-12T00:00:00Z' }; });
    await openApp();
    fireEvent.change(screen.getByLabelText('行程想法'), { target: { value: '第一份的想法' } });
    await userEvent.click(screen.getByRole('button', { name: '保存想法' }));
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    await act(async () => complete(saved.get('one')!));
    expect(screen.queryByText('第一份的想法', { selector: '.chat-message p' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
  });

  it('returns to the sending trip before its response and updates the current message view exactly once', async () => {
    const sent = { id: 'late-message', trip_id: 'one', role: 'user', content: '等待响应的想法', status: 'pending', created_at: '2026-10-12T00:00:00Z' };
    let complete!: (value: typeof sent) => void;
    vi.spyOn(api, 'message').mockReturnValue(new Promise(resolve => { complete = resolve; }));
    await openApp();
    fireEvent.change(screen.getByLabelText('行程想法'), { target: { value: sent.content } });
    await userEvent.click(screen.getByRole('button', { name: '保存想法' }));
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
    expect((screen.getByRole('button', { name: '正在保存…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText('行程想法') as HTMLTextAreaElement).disabled).toBe(true);
    await act(async () => { complete(sent); });
    await screen.findByText(sent.content, { selector: '.chat-message p' });
    expect((screen.getByLabelText('行程想法') as HTMLTextAreaElement).value).toBe('');
    expect((screen.getByRole('button', { name: '保存想法' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.message).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText(sent.content, { selector: '.chat-message p' })).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
    expect(screen.getAllByText(sent.content, { selector: '.chat-message p' })).toHaveLength(1);
    expect((screen.getByLabelText('行程想法') as HTMLTextAreaElement).value).toBe('');
  });

  it('reopens A before the atomic message response and shows its message and discovery exactly once', async () => {
    const discovery: Run = { id: 'discovery-one', trip_id: 'one', kind: 'discover', request_version: 2, generation: 1, status: 'completed', stage: 'done', params: {}, budget: defaultDiscoveryBudget, checkpoint: { completed_stages: [] }, usage: { steps: 6, elapsed_seconds: 1, external_calls: 2, model_tokens: 300, images: 0 }, result: null, error_code: null, stop_reason: null, created_at: '2026-10-12T01:00:00Z', updated_at: '2026-10-12T01:00:00Z' };
    const sent: Message = { id: 'discovery-message', trip_id: 'one', role: 'user', content: '想找安静的水边', status: 'pending', created_at: discovery.created_at, discovery_run_id: discovery.id, discovery_run: discovery };
    let finish!: (value: Message) => void;
    vi.spyOn(api, 'message').mockReturnValue(new Promise(resolve => { finish = resolve; }));
    vi.spyOn(api, 'candidates').mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
    vi.spyOn(api, 'gaps').mockResolvedValue({ items: [] });
    await openApp();
    fireEvent.change(screen.getByLabelText(/从哪里出发/), { target: { value: '杭州东站' } });
    fireEvent.change(screen.getByLabelText('行程想法'), { target: { value: sent.content } });
    await userEvent.click(screen.getByRole('button', { name: '保存并发现' }));
    await waitFor(() => expect(api.message).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.message).mock.calls[0][3]).toEqual({ expected_version: 2, budget: defaultDiscoveryBudget });
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
    expect((screen.getByRole('button', { name: '正在保存…' }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole('button', { name: '发现攻略' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finish(sent); });
    await screen.findByText('本轮完成');
    expect(screen.getAllByText(sent.content, { selector: '.chat-message p' })).toHaveLength(1);
    expect(screen.getByText('已关联发现任务')).toBeTruthy();
    expect((screen.getByLabelText('行程想法') as HTMLTextAreaElement).value).toBe('');
    expect((screen.getByRole('button', { name: '保存并发现' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.message).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    expect(screen.queryByText('本轮完成')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByText('本轮完成');
    expect(screen.getAllByText(sent.content, { selector: '.chat-message p' })).toHaveLength(1);
  });
});
