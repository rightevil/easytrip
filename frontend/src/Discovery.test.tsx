import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from './api';
import type { Message, Run, Trip } from './api';
import { Discovery } from './Discovery';
import { defaultDiscoveryBudget } from './discovery-types';
import type { Candidate, CandidatePage, Evidence, Source } from './discovery-types';
import { TripSession } from './session';

const timestamp = '2026-10-12T01:00:00Z';
const run = (tripId: string, overrides: Partial<Run> = {}): Run => ({
  id: `run-${tripId}`, trip_id: tripId, kind: 'discover', request_version: 1, generation: 1,
  status: 'completed', stage: 'candidates_ready', params: {}, budget: { ...defaultDiscoveryBudget },
  checkpoint: { completed_stages: [] },
  usage: { steps: 6, elapsed_seconds: 10, external_calls: 2, model_tokens: 500, images: 0 },
  result: null, error_code: null, stop_reason: null, created_at: timestamp, updated_at: timestamp, ...overrides,
});
const trip = (id: string, runs: Run[] = []): Trip => ({
  id, name: id, status: 'draft', request_version: 1, workspace_version: 1,
  request: { destination: null, origin: null, start_at: null, duration_minutes: null, transport: 'mixed', transport_source: 'default', budget_cny: null, participants: 1, preferences: '' },
  workspace: { active_tab: 'overview', category: 'all', map_center: null, map_zoom: 12, selected_ids: [], map_visible_ids: [] },
  created_at: timestamp, updated_at: timestamp, messages: [], runs,
});
const candidate = (tripId: string, overrides: Partial<Candidate> = {}): Candidate => ({
  id: `candidate-${tripId}`, trip_id: tripId, run_id: `run-${tripId}`, request_version: 1,
  name: '有来源的湖畔方向', kind: 'direction', city: '湖州市', direction: '湖畔', category: 'play',
  mention_ids: ['mention-one'], evidence_ids: ['evidence-one'], match_status: 'unmatched',
  summary: '来源提及湖畔，当前交通与价格待核实。', uncertainty: ['图片待核验'],
  stats: { author_count: 2, positive_count: 1, negative_count: 1, uncertain_count: 1 }, ...overrides,
});
const page = (...items: Candidate[]): CandidatePage => ({ items, total: items.length, limit: 20, offset: 0 });
const source = (overrides: Partial<Source> = {}): Source => ({ id: 'source-one', trip_id: 'one', run_id: 'run-one', request_version: 1, platform: 'xiaohongshu', kind: 'note', external_id: 'original-one', parent_source_id: null, title: '湖畔笔记', text: '湖州市湖畔有安静步道，开放时间待确认。', images: [], published_at: null, completeness: 'partial', read_status: 'success', error_code: null, dedupe_confidence: 'high', first_read_at: timestamp, last_read_at: timestamp, ...overrides });
const sessions: TripSession[] = [];
function session(value: Trip) { const created = new TripSession(value, () => {}); sessions.push(created); return created; }
beforeEach(() => {
  vi.spyOn(api, 'candidates').mockResolvedValue(page());
  vi.spyOn(api, 'places').mockImplementation(async (id, runId) => ({ trip: trip(id), run_id: runId ?? null, request_version: 1, candidates: [], matches: [], points: [] }));
  vi.spyOn(api, 'gaps').mockResolvedValue({ items: [] });
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
});
afterEach(() => { cleanup(); sessions.splice(0).forEach(value => value.dispose()); vi.restoreAllMocks(); });

describe('source-grounded discovery', () => {
  it('keeps atomic message retries on the original operation, version and budget after an uncertain response', async () => {
    const current = session(trip('one'));
    current.request.update({ name: 'one', request: { ...current.trip.request, origin: '杭州' } });
    vi.spyOn(api, 'update').mockImplementation(async (id, version, draft) => ({ ...trip(id), ...draft, request_version: version + 1 }));
    const saved: Message = { id: 'message-one', trip_id: 'one', role: 'user', status: 'pending', content: '想在水边休息', created_at: timestamp, discovery_run: run('one') };
    vi.spyOn(api, 'message').mockRejectedValueOnce(new ApiError('响应中断', 0, 'network_error')).mockResolvedValueOnce(saved);
    current.editMessage(saved.content);
    await current.sendMessage(true);
    expect(current.getChatSnapshot()).toMatchObject({ content: saved.content, retryPending: true, discoveryRequested: true });
    current.setDiscoveryBudget({ ...defaultDiscoveryBudget, target_notes: 10 });
    current.accept({ ...current.trip, request_version: 3 });
    await current.sendMessage(true);
    expect(api.update).toHaveBeenCalledTimes(1);
    const calls = vi.mocked(api.message).mock.calls;
    expect(calls[0]).toEqual(calls[1]);
    expect(calls[0][3]).toEqual({ expected_version: 2, budget: defaultDiscoveryBudget });
    expect(current.getChatSnapshot()).toMatchObject({ content: '', retryPending: false, messages: [saved] });
    expect(current.getRunSnapshot().runs.map(item => item.id)).toEqual(['run-one']);
    expect(current.request.getSnapshot().version).toBe(3);
  });

  it('never claims discovery started when only a message was confirmed saved', async () => {
    const current = session(trip('one'));
    current.editMessage('想发呆');
    vi.spyOn(api, 'message').mockResolvedValue({ id: 'message-one', trip_id: 'one', role: 'user', status: 'pending', content: '想发呆', created_at: timestamp });
    await current.sendMessage(true);
    expect(current.getChatSnapshot()).toMatchObject({ content: '', retryPending: false, error: '消息已保存，但服务未返回发现任务，尚未确认启动。' });
    expect(current.getRunSnapshot().runs).toEqual([]);
  });

  it('returns to A before the discovery POST finishes and shares its busy state and confirmed result', async () => {
    const one = session(trip('one'));
    const two = session(trip('two'));
    let finish!: (value: Run) => void;
    vi.spyOn(api, 'startDiscovery').mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const view = render(<Discovery key="one" session={one} requestVersion={1} category="all" archived={false} />);
    await userEvent.click(screen.getByRole('button', { name: '发现攻略' }));
    await waitFor(() => expect(api.startDiscovery).toHaveBeenCalledTimes(1));
    view.rerender(<Discovery key="two" session={two} requestVersion={1} category="all" archived={false} />);
    view.rerender(<Discovery key="one-again" session={one} requestVersion={1} category="all" archived={false} />);
    expect((screen.getByRole('button', { name: '正在提交…' }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finish(run('one')); });
    await screen.findByText('本轮完成');
    expect(api.startDiscovery).toHaveBeenCalledTimes(1);
    expect(two.trip.runs).toEqual([]);
  });

  it('discards late candidate responses even when A is reopened before its old read finishes', async () => {
    const one = session(trip('one', [run('one')]));
    const two = session(trip('two', [run('two')]));
    let oldRead!: (value: CandidatePage) => void;
    vi.mocked(api.candidates).mockReturnValueOnce(new Promise(resolve => { oldRead = resolve; })).mockResolvedValueOnce(page(candidate('two', { name: 'B 的来源候选' }))).mockResolvedValueOnce(page(candidate('one', { name: 'A 的最新候选' })));
    const view = render(<Discovery key="one" session={one} requestVersion={1} category="all" archived={false} />);
    await waitFor(() => expect(api.candidates).toHaveBeenCalledTimes(1));
    view.rerender(<Discovery key="two" session={two} requestVersion={1} category="all" archived={false} />);
    await screen.findByRole('heading', { name: 'B 的来源候选' });
    view.rerender(<Discovery key="one-again" session={one} requestVersion={1} category="all" archived={false} />);
    await screen.findByRole('heading', { name: 'A 的最新候选' });
    await act(async () => { oldRead(page(candidate('one', { name: 'A 的过时页面' }))); });
    expect(screen.queryByRole('heading', { name: 'A 的过时页面' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'A 的最新候选' })).toBeTruthy();
  });

  it('adopts a direction only after explicit confirmation and protects the new version against an old GET', async () => {
    const current = session(trip('one', [run('one')]));
    vi.mocked(api.candidates).mockResolvedValue(page(candidate('one')));
    const next = { ...trip('one'), request: { ...current.trip.request, destination: '湖州市' }, request_version: 2 };
    vi.spyOn(api, 'adoptDirection').mockResolvedValue(next);
    const view = render(<Discovery session={current} requestVersion={1} category="all" archived={false} />);
    await screen.findByRole('heading', { name: '有来源的湖畔方向' });
    expect(current.trip.request.destination).toBeNull();
    expect(api.adoptDirection).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '采用这个方向' }));
    expect(api.adoptDirection).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '确认采用并保存' }));
    await waitFor(() => expect(current.request.getSnapshot().version).toBe(2));
    expect(api.adoptDirection).toHaveBeenCalledWith('one', 'candidate-one', 'run-one', expect.any(String), 1);
    act(() => current.accept(trip('one')));
    expect(current.request.getSnapshot()).toMatchObject({ version: 2, value: { request: { destination: '湖州市' } } });
    view.rerender(<Discovery session={current} requestVersion={2} category="all" archived={false} />);
    expect((screen.getByRole('button', { name: '采用这个方向' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('retains current conditions on direction conflict and does not apply a late historical retry over newer conditions', async () => {
    const current = session(trip('one', [run('one')]));
    vi.spyOn(api, 'adoptDirection').mockRejectedValueOnce(new ApiError('条件已经更新', 409, 'version_conflict')).mockResolvedValueOnce({ ...trip('one'), request: { ...trip('one').request, destination: '湖州市' }, request_version: 2 });
    await current.adoptDirection(candidate('one'));
    expect(current.trip.request.destination).toBeNull();
    expect(current.getRunSnapshot().error).toBe('条件已经更新');
    current.accept({ ...trip('one'), request: { ...trip('one').request, destination: '宁波市' }, request_version: 3 });
    await current.adoptDirection(candidate('one'));
    expect(vi.mocked(api.adoptDirection).mock.calls[0]).toEqual(vi.mocked(api.adoptDirection).mock.calls[1]);
    expect(current.request.getSnapshot()).toMatchObject({ version: 3, value: { request: { destination: '宁波市' } } });
  });

  it('starts a fresh budget-confirmed round after done partial and preserves that operation on retry', async () => {
    const previous = run('one', { status: 'partial', stage: 'done', stop_reason: 'time_budget_exhausted' });
    const current = session(trip('one', [previous]));
    vi.spyOn(api, 'runAction');
    vi.spyOn(api, 'startDiscovery').mockRejectedValueOnce(new ApiError('响应中断', 0, 'network_error')).mockResolvedValueOnce(run('one', { id: 'run-new' }));
    render(<Discovery session={current} requestVersion={1} category="all" archived={false} />);
    expect(screen.queryByRole('button', { name: '恢复发现任务' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '继续一轮（新增预算）' }));
    expect(api.startDiscovery).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '确认新增预算并继续' }));
    await screen.findByRole('button', { name: '重试发现任务' });
    act(() => current.setDiscoveryBudget({ ...defaultDiscoveryBudget, target_notes: 5 }));
    await userEvent.click(screen.getByRole('button', { name: '重试发现任务' }));
    await waitFor(() => expect(api.startDiscovery).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.startDiscovery).mock.calls[0]).toEqual(vi.mocked(api.startDiscovery).mock.calls[1]);
    expect(vi.mocked(api.startDiscovery).mock.calls[0]).toEqual(['one', expect.any(String), 1, defaultDiscoveryBudget, 'run-one']);
    expect(api.runAction).not.toHaveBeenCalled();
    expect(current.getRunSnapshot().runs.find(item => item.id === 'run-one')?.usage).toEqual(previous.usage);
  });

  it('locates image evidence in its original run snapshot and ignores its late source after changing rounds', async () => {
    const currentRun = run('one', { id: 'new-run', request_version: 2, created_at: '2026-10-12T02:00:00Z' });
    const current = session({ ...trip('one', [currentRun, run('one')]), request_version: 2 });
    vi.mocked(api.candidates).mockImplementation(async (_id, runId) => page(candidate('one', { run_id: runId, request_version: runId === 'new-run' ? 2 : 1 })));
    const imageEvidence: Evidence = { id: 'evidence-one', trip_id: 'one', run_id: 'new-run', request_version: 2, source_id: 'source-one', mention_id: 'mention-one', object_text: '湖畔', dimension: '景观', stance: 'unknown', nature: 'unknown', quote: '图中有湖畔步道', link_status: 'uncertain', model_id: 'Agnes', schema_version: 'p2-v1', citation_kind: 'image', image_index: 2, image_observation: { source_id: 'source-one', image_index: 2, text: '图中有湖畔步道，具体位置待核验', uncertain: true } };
    vi.spyOn(api, 'evidence').mockResolvedValue({ items: [imageEvidence], sources: [] });
    let finish!: (value: Source) => void;
    vi.spyOn(api, 'source').mockReturnValue(new Promise(resolve => { finish = resolve; }));
    render(<Discovery session={current} requestVersion={2} category="all" archived={false} />);
    await userEvent.click(await screen.findByRole('button', { name: '查看来源与证据' }));
    await screen.findByText(/原图第 2 张/);
    expect(screen.getByText('图中有湖畔步道，具体位置待核验')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '查看原文' }));
    expect(api.source).toHaveBeenCalledWith('one', 'source-one', 'new-run');
    await userEvent.selectOptions(screen.getByLabelText('查看发现轮次'), 'run-one');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await act(async () => { finish({ id: 'source-one', trip_id: 'one', run_id: 'new-run', request_version: 2, platform: 'xiaohongshu', kind: 'note', external_id: 'original-one', parent_source_id: null, title: '新轮原始正文', text: '新轮正文不该出现在旧轮', images: [], published_at: null, completeness: 'partial', read_status: 'success', error_code: null, dedupe_confidence: 'high', first_read_at: timestamp, last_read_at: timestamp }); });
    expect(screen.queryByText('新轮正文不该出现在旧轮')).toBeNull();
  });

  it('keeps generic food as a source hint without presenting a located place or an adoptable direction', async () => {
    const current = session(trip('one', [run('one')]));
    vi.mocked(api.candidates).mockResolvedValue(page(candidate('one', { kind: 'generic', category: 'eat', name: '麦虾' })));
    render(<Discovery session={current} requestVersion={1} category="all" archived={false} />);
    await screen.findByRole('heading', { name: '麦虾' });
    expect(screen.getByText('泛称线索')).toBeTruthy();
    expect(screen.queryByText('待定位')).toBeNull();
    expect(screen.queryByRole('button', { name: '采用这个方向' })).toBeNull();
    expect(screen.getByRole('button', { name: '查看来源与证据' })).toBeTruthy();
  });

  it('shows actual coverage and unknown charges with an honest service pause', () => {
    const paused = run('one', { status: 'paused', stage: 'fetching', stop_reason: 'source_login_or_access_required', usage: { steps: 9, elapsed_seconds: 22, external_calls: 6, model_tokens: 1200, images: 3, searches: 2, model_calls: 2, notes_obtained: 4, notes_analyzed: 2, notes_failed: 1, comments_obtained: 40, comments_selected: 20, comments_analyzed: 15, comments_judged: 12, judgement_rows: 19, images_obtained: 8, unknown_cost_calls: 1 }, trip_usage: { external_calls: 9, model_tokens: 1800, images: 5, unknown_cost_calls: 2, cost_cny: null } });
    render(<Discovery session={session(trip('one', [paused]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    expect(coverage.getByText('4 / 30')).toBeTruthy();
    expect(coverage.getByText('40 / 15')).toBeTruthy();
    expect(coverage.getByText('12 / 28')).toBeTruthy();
    expect(coverage.getByText('8 / 3')).toBeTruthy();
    expect(coverage.getByText(/费用未知/)).toBeTruthy();
    expect(screen.getByText(/小红书登录或访问受限/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '恢复发现任务' })).toBeTruthy();
    expect(screen.queryByText('当地没有地点')).toBeNull();
  });

  it('locates a mention-only candidate without converting its original text into a positive judgement', async () => {
    const current = session(trip('one', [run('one')]));
    vi.mocked(api.candidates).mockResolvedValue(page(candidate('one', { evidence_ids: [], stats: { author_count: 1, positive_count: 0, negative_count: 0, uncertain_count: 0 } })));
    vi.spyOn(api, 'evidence').mockResolvedValue({ items: [], sources: [source({ images: ['', 'javascript:alert(1)', 'https://example.test/original-three.jpg'] })] });
    vi.spyOn(api, 'source');
    render(<Discovery session={current} requestVersion={1} category="all" archived={false} />);
    await userEvent.click(await screen.findByRole('button', { name: '查看来源与证据' }));
    const original = await screen.findByRole('region', { name: '候选提及的原始来源' });
    expect(within(original).getByText(/不代表赞成评价/)).toBeTruthy();
    expect(screen.getByText(/尚无可显示的评价证据/)).toBeTruthy();
    expect(screen.getByText('正面 0')).toBeTruthy();
    await userEvent.click(within(original).getByRole('button', { name: '查看提及原文' }));
    expect(screen.getByText(source().text, { selector: 'blockquote' })).toBeTruthy();
    expect(screen.getByText('原图第 1 张：图片链接不可用')).toBeTruthy();
    expect(screen.getByText('原图第 2 张：图片链接不可用')).toBeTruthy();
    expect(screen.getByRole('link', { name: '原图第 3 张（外部链接）' }).getAttribute('href')).toBe('https://example.test/original-three.jpg');
    expect(screen.queryByRole('img')).toBeNull();
    expect(api.source).not.toHaveBeenCalled();
  });

  it('rejects an original mention source from another run before showing its text', async () => {
    vi.mocked(api.candidates).mockResolvedValue(page(candidate('one', { evidence_ids: [] })));
    vi.spyOn(api, 'evidence').mockResolvedValue({ items: [], sources: [source({ run_id: 'another-run', text: '其它任务私有正文' })] });
    render(<Discovery session={session(trip('one', [run('one')]))} requestVersion={1} category="all" archived={false} />);
    await userEvent.click(await screen.findByRole('button', { name: '查看来源与证据' }));
    await screen.findByText('提及来源归属不一致，已阻止展示。');
    expect(screen.queryByText('其它任务私有正文')).toBeNull();
    expect(screen.queryByRole('region', { name: '候选提及的原始来源' })).toBeNull();
  });

  it('uses unique image coverage and requires a new run after uncertain model retries', () => {
    const paused = run('one', { status: 'paused', stage: 'extracting', stop_reason: 'model_retry_requires_new_run', usage: { steps: 10, elapsed_seconds: 40, external_calls: 4, model_tokens: 2000, reserved_tokens: 4000, images: 8, images_obtained: 5, images_analyzed_unique: 2, images_unanalyzed: 3, unknown_cost_calls: 1 } });
    render(<Discovery session={session(trip('one', [paused]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    expect(coverage.getByText('5 / 8')).toBeTruthy();
    expect(coverage.getByText('2 / 3')).toBeTruthy();
    expect(coverage.getByText(/未结清预留 token 4,000/)).toBeTruthy();
    expect(screen.getByText(/重复提交图片存在额外用量风险/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: '恢复发现任务' })).toBeNull();
    expect(screen.getByRole('button', { name: '继续一轮（新增预算）' })).toBeTruthy();
  });

  it('can resume a source pause when the image allocation is zero and no image submission is required', () => {
    const paused = run('one', { status: 'paused', stage: 'fetching', stop_reason: 'source_login_or_access_required', budget: { ...defaultDiscoveryBudget, max_images: 0 } });
    render(<Discovery session={session(trip('one', [paused]))} requestVersion={1} category="all" archived={false} />);
    expect(screen.getByRole('button', { name: '恢复发现任务' })).toBeTruthy();
  });

  it.each(['model_result_unknown', 'model_request_failed'])('requires a budget-confirmed new round after %s and preserves unknown charges', async stopReason => {
    const paused = run('one', { status: 'paused', stage: stopReason === 'model_request_failed' ? 'understanding' : 'extracting', stop_reason: stopReason, usage: { steps: 4, elapsed_seconds: 357, external_calls: 4, model_calls: 4, model_tokens: 27458, reserved_tokens: 32768, images: 0, unknown_cost_calls: 1, unknown_result_calls: 1 }, result: { model: { model_id: 'Agnes', schema_version: 'p2-v1', semantic_quality: 'not_validated' } } });
    vi.spyOn(api, 'runAction');
    vi.spyOn(api, 'startDiscovery').mockResolvedValue(run('one', { id: 'new-round' }));
    render(<Discovery session={session(trip('one', [paused]))} requestVersion={1} category="all" archived={false} />);
    expect(screen.getByText(/模型调用结果未知，可能已执行，费用尚未确认/)).toBeTruthy();
    expect(screen.getByText(/本轮不能恢复，请确认新增预算后继续一轮/)).toBeTruthy();
    expect(screen.getByText(/未结清预留 token 32,768/)).toBeTruthy();
    expect(screen.getByText('本轮候选未经逐条核实，请结合原文核对。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '恢复发现任务' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '继续一轮（新增预算）' }));
    expect(api.startDiscovery).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: '确认新增预算并继续' }));
    await waitFor(() => expect(api.startDiscovery).toHaveBeenCalledWith('one', expect.any(String), 1, defaultDiscoveryBudget, 'run-one'));
    expect(api.runAction).not.toHaveBeenCalled();
  });

  it('distinguishes reused sources from zero new analysis and shows every partial reason without claiming semantic acceptance', () => {
    const partial = run('one', {
      status: 'partial', stage: 'done',
      usage: { steps: 3, elapsed_seconds: 8, external_calls: 1, model_tokens: 500, images: 0, notes_obtained: 0, notes_analyzed: 0, comments_obtained: 0, comments_analyzed: 0, comments_judged: 0 },
      result: {
        coverage: { notes_reused: 3, comments_reused: 9, partial_reasons: ['note_target_not_reached'] },
        partial_reasons: ['note_target_not_reached', 'comments_partly_unanalysed', 'insufficient_exploration_directions'],
        model: { model_id: 'Agnes', schema_version: 'p2-v1', semantic_quality: 'not_validated' },
      },
    });
    render(<Discovery session={session(trip('one', [partial]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    expect(coverage.getByText('0 / 30')).toBeTruthy();
    expect(coverage.getByText('沿用笔记：已分析 / 总数').closest('div')?.querySelector('strong')?.textContent).toBe('未知 / 3');
    expect(coverage.getByText('沿用评论：已分析 / 总数').closest('div')?.querySelector('strong')?.textContent).toBe('未知 / 9');
    expect(coverage.getByText('沿用评论：已判 / 未判').closest('div')?.querySelector('strong')?.textContent).toBe('未知 / 未知');
    expect(coverage.getByText('沿用图片：已分析 / 总数 / 未分析（去重）').closest('div')?.querySelector('strong')?.textContent).toBe('未知 / 未知 / 未知');
    expect(coverage.getByText(/历史记录未保存的计数保持未知/)).toBeTruthy();
    const reasons = coverage.getByText(/未完成项：/).textContent!;
    expect(reasons).toContain('本轮新增取得的笔记未达到目标数量。');
    expect(reasons).toContain('本轮选取的部分评论尚未完成分析。');
    expect(reasons).toContain('有来源的探索方向不足两个，暂无法充分比较。');
    expect(reasons.match(/本轮新增取得的笔记未达到目标数量/g)).toHaveLength(1);
    expect(coverage.getByText('本轮候选未经逐条核实，请结合原文核对。')).toBeTruthy();
    expect(screen.queryByRole('button', { name: '恢复发现任务' })).toBeNull();
  });

  it.each([undefined, 0, 2])('shows rejected activity counts only when recorded (%s) and preserves semantic uncertainty', rejected => {
    const partial = run('one', {
      status: 'partial', stage: 'done',
      result: {
        coverage: { activities_rejected: rejected },
        partial_reasons: ['activities_partly_unanalysed'],
        model: { model_id: 'Agnes', schema_version: 'p2-v1', semantic_quality: 'not_validated' },
      },
    });
    render(<Discovery session={session(trip('one', [partial]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    expect(coverage.getByText(/部分活动引用未通过校验，已保留有效评论；相关活动未纳入候选。/)).toBeTruthy();
    if (rejected === undefined) expect(coverage.queryByText('引用校验未通过活动')).toBeNull();
    else expect(coverage.getByText('引用校验未通过活动').closest('div')?.querySelector('strong')?.textContent).toBe(String(rejected));
    expect(coverage.getByText('本轮候选未经逐条核实，请结合原文核对。')).toBeTruthy();
  });

  it('shows first analysis of reused sources separately from zero new collection and actual charged usage', () => {
    const analyzed = run('one', {
      status: 'partial', stage: 'done',
      usage: { steps: 4, elapsed_seconds: 90, external_calls: 1, model_calls: 1, model_tokens: 32000, images: 3, notes_obtained: 0, notes_analyzed: 0, comments_obtained: 0, comments_analyzed: 0, comments_judged: 0, images_obtained: 0, images_analyzed_unique: 0, images_unanalyzed: 0 },
      result: { coverage: { notes_reused: 20, notes_reused_analyzed: 1, comments_reused: 301, comments_reused_analyzed: 20, comments_reused_judged: 12, images_reused_obtained: 215, images_reused_analyzed_unique: 3, images_reused_unanalyzed: 212 } },
    });
    render(<Discovery session={session(trip('one', [analyzed]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    const metric = (label: string) => coverage.getByText(label).closest('div')?.querySelector('strong')?.textContent;
    expect(metric('本轮新增笔记 / 目标')).toBe('0 / 30');
    expect(metric('取得 / 已分析评论')).toBe('0 / 0');
    expect(metric('取得图片 / 提交次数')).toBe('0 / 3');
    expect(metric('已分析 / 未分析图片（去重）')).toBe('0 / 0');
    expect(metric('沿用笔记：已分析 / 总数')).toBe('1 / 20');
    expect(metric('沿用评论：已分析 / 总数')).toBe('20 / 301');
    expect(metric('沿用评论：已判 / 未判')).toBe('12 / 289');
    expect(metric('沿用图片：已分析 / 总数 / 未分析（去重）')).toBe('3 / 215 / 212');
    expect(coverage.getByText(/已知模型 token 32,000/)).toBeTruthy();
    expect(coverage.getByText(/沿用数量不计作新增采集或收费次数/)).toBeTruthy();
  });

  it.each([undefined, 0])('does not invent reused image completion when its remaining count is %s', remaining => {
    const historical = run('one', { result: { coverage: { images_reused_obtained: 1, images_reused_analyzed_unique: 1, images_reused_unanalyzed: remaining, comments_reused_judged: 0 } } });
    render(<Discovery session={session(trip('one', [historical]))} requestVersion={1} category="all" archived={false} />);
    const coverage = within(screen.getByRole('region', { name: '实际采集与分析覆盖' }));
    expect(coverage.getByText('沿用图片：已分析 / 总数 / 未分析（去重）').closest('div')?.querySelector('strong')?.textContent).toBe(`1 / 1 / ${remaining === undefined ? '未知' : '0'}`);
    expect(coverage.getByText('沿用评论：已判 / 未判').closest('div')?.querySelector('strong')?.textContent).toBe('0 / 未知');
  });
});
