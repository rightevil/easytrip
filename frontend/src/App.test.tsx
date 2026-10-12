import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { api, ApiError } from './api';
import type { Trip } from './api';

const trip = (id: string, name: string): Trip => ({
  id, name, status: 'draft', request_version: 1, workspace_version: 1,
  request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' },
  workspace: { active_tab: 'overview', category: 'all', map_center: null, map_zoom: 12, selected_ids: [], map_visible_ids: [] },
  created_at: '2026-10-12T00:00:00Z', updated_at: '2026-10-12T00:00:00Z', messages: [], runs: [],
});
let saved: Map<string, Trip>;
beforeEach(() => {
  localStorage.clear();
  saved = new Map([['one', trip('one', '第一份行程')], ['two', trip('two', '第二份行程')]]);
  vi.spyOn(api, 'list').mockImplementation(async () => ({ items: [...saved.values()], total: saved.size, offset: 0, limit: 50 }));
  vi.spyOn(api, 'get').mockImplementation(async id => structuredClone(saved.get(id)!));
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
async function openApp() { render(<App />); await screen.findByRole('heading', { name: '第一份行程' }); }

describe('real workspace behaviors', () => {
  it('saves conditions before switching and restores each trip independently', async () => {
    await openApp();
    fireEvent.change(screen.getByLabelText(/从哪里出发/), { target: { value: '杭州东站' } });
    await userEvent.click(screen.getByRole('button', { name: /第二份行程/ }));
    await screen.findByRole('heading', { name: '第二份行程' });
    expect((screen.getByLabelText(/从哪里出发/) as HTMLInputElement).value).toBe('');
    expect(api.update).toHaveBeenCalledWith('one', 1, expect.objectContaining({ request: expect.objectContaining({ origin: '杭州东站', destination: null }) }));
    await userEvent.click(screen.getByRole('button', { name: /第一份行程/ }));
    await screen.findByRole('heading', { name: '第一份行程' });
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
});
