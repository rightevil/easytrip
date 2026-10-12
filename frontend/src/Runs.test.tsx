import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from './api';
import type { Run, Trip } from './api';
import { Runs } from './Runs';
import { TripSession } from './session';

const run = (tripId: string, status: Run['status'] = 'pending'): Run => ({ id: `run-${tripId}`, trip_id: tripId, kind: 'prepare_workspace', request_version: 1, generation: 1, status, stage: 'validate_request', params: {}, budget: { max_steps: 3, max_seconds: 30 }, checkpoint: { completed_stages: [] }, usage: { steps: 0, elapsed_seconds: 0, external_calls: 0, model_tokens: 0, images: 0 }, result: null, error_code: null, stop_reason: null, created_at: '2026-10-12T01:00:00Z', updated_at: '2026-10-12T01:00:00Z' });
const trip = (id: string, runs: Run[] = []): Trip => ({ id, name: id, status: 'draft', request_version: 1, workspace_version: 1, request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' }, workspace: { active_tab: 'overview', category: 'all', map_center: null, map_zoom: 12, selected_ids: [], map_visible_ids: [] }, created_at: '2026-10-12T01:00:00Z', updated_at: '2026-10-12T01:00:00Z', messages: [], runs });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('bounded local task UI', () => {
  it('saves current edits before starting and reuses an operation id after uncertain failure', async () => {
    const session = new TripSession(trip('one'), () => {});
    session.request.update({ name: '已修改', request: session.trip.request });
    let finishSave!: (trip: Trip) => void;
    const saved = new Promise<Trip>(resolve => { finishSave = resolve; });
    vi.spyOn(api, 'update').mockReturnValue(saved);
    const completed = { ...run('one', 'completed'), stage: 'done' as const, request_version: 2, checkpoint: { completed_stages: ['validate_request', 'read_workspace', 'summarize'] } };
    vi.spyOn(api, 'startRun').mockRejectedValueOnce(new ApiError('响应中断', 0, 'network_error')).mockResolvedValueOnce(completed);
    render(<Runs session={session} requestVersion={1} archived={false} />);
    await userEvent.click(screen.getByRole('button', { name: '开始准备' }));
    expect(api.startRun).not.toHaveBeenCalled();
    await act(async () => { finishSave({ ...trip('one'), name: '已修改', request_version: 2 }); });
    await screen.findByRole('button', { name: '重试准备' });
    await userEvent.click(screen.getByRole('button', { name: '重试准备' }));
    await waitFor(() => expect(api.startRun).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.startRun).mock.calls[0]).toEqual(vi.mocked(api.startRun).mock.calls[1]);
    expect(vi.mocked(api.startRun).mock.calls[0][2]).toBe(2);
    session.dispose();
  });

  it('stops the previous trip poll and ignores its late result after switching', async () => {
    const one = new TripSession(trip('one', [run('one')]), () => {});
    const two = new TripSession(trip('two'), () => {});
    let finish!: (run: Run) => void;
    vi.spyOn(api, 'getRun').mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const view = render(<Runs key="one" session={one} requestVersion={1} archived={false} />);
    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith('run-one', 'one'));
    view.rerender(<Runs key="two" session={two} requestVersion={1} archived={false} />);
    await act(async () => { finish({ ...run('one', 'completed'), stage: 'done' }); });
    expect(screen.queryByText('准备完成')).toBeNull();
    expect(screen.getByText('还没有任务记录。准备完成的内容会保留在当前行程。')).toBeTruthy();
    expect(two.trip.runs).toEqual([]);
    one.dispose(); two.dispose();
  });

  it('returns to the starting trip before its POST response and shares submission state and result', async () => {
    const one = new TripSession(trip('one'), () => {});
    const two = new TripSession(trip('two'), () => {});
    let complete!: (run: Run) => void;
    vi.spyOn(api, 'startRun').mockReturnValue(new Promise(resolve => { complete = resolve; }));
    const view = render(<Runs key="one" session={one} requestVersion={1} archived={false} />);
    await userEvent.click(screen.getByRole('button', { name: '开始准备' }));
    await waitFor(() => expect(api.startRun).toHaveBeenCalledTimes(1));
    view.rerender(<Runs key="two" session={two} requestVersion={1} archived={false} />);
    view.rerender(<Runs key="one-returned" session={one} requestVersion={1} archived={false} />);
    expect((screen.getByRole('button', { name: '正在提交…' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { complete({ ...run('one', 'completed'), stage: 'done' }); });
    await screen.findByText('准备完成');
    expect(screen.getByRole('button', { name: '开始准备' })).toBeTruthy();
    expect(api.startRun).toHaveBeenCalledTimes(1);
    expect(two.trip.runs).toEqual([]);
    one.dispose(); two.dispose();
  });

  it('uses current generations for cancel/resume and rejects older poll responses', async () => {
    const session = new TripSession(trip('one', [run('one')]), () => {});
    let oldPoll!: (run: Run) => void;
    vi.spyOn(api, 'getRun').mockReturnValueOnce(new Promise(resolve => { oldPoll = resolve; })).mockResolvedValue({ ...run('one', 'completed'), generation: 3, updated_at: '2026-10-12T01:00:03Z' });
    vi.spyOn(api, 'runAction').mockResolvedValueOnce({ ...run('one', 'cancelled'), generation: 2, updated_at: '2026-10-12T01:00:02Z' }).mockResolvedValueOnce({ ...run('one', 'completed'), generation: 3, stage: 'done', updated_at: '2026-10-12T01:00:03Z' });
    render(<Runs session={session} requestVersion={1} archived={false} />);
    await userEvent.click(screen.getByRole('button', { name: '取消任务' }));
    await screen.findByText('已取消');
    await act(async () => { oldPoll(run('one', 'running')); });
    expect(screen.getByText('已取消')).toBeTruthy();
    expect(api.runAction).toHaveBeenNthCalledWith(1, 'run-one', 'cancel', 'one', 1);
    await userEvent.click(screen.getByRole('button', { name: '恢复任务' }));
    await screen.findByText('准备完成');
    expect(api.runAction).toHaveBeenNthCalledWith(2, 'run-one', 'resume', 'one', 2);
    session.dispose();
  });

  it('keeps old-version and exhausted tasks visible without offering unsafe resume', () => {
    const old = { ...run('one', 'recoverable'), request_version: 1 };
    const exhausted = { ...run('one', 'partial'), id: 'exhausted', request_version: 2, usage: { ...run('one').usage, steps: 3 }, stop_reason: 'step_budget_exhausted' };
    const session = new TripSession(trip('one', [old, exhausted]), () => {});
    render(<Runs session={session} requestVersion={2} archived={false} />);
    expect(screen.getByText('旧条件记录')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '恢复任务' })).toBeNull();
    expect(screen.getByText(/本轮预算已用尽/)).toBeTruthy();
    session.dispose();
  });
});
