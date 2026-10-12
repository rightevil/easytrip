import { useEffect, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import type { Run } from './api';
import type { TripSession } from './session';

const statuses: Record<Run['status'], string> = {
  pending: '等待开始', running: '准备中', recoverable: '重启后可恢复', completed: '准备完成', partial: '部分完成', failed: '准备失败', cancelled: '已取消', stale: '旧条件记录',
};
const stageLabels: Record<Run['stage'], string> = { validate_request: '检查行程条件', read_workspace: '读取已保存内容', summarize: '汇总工作区', done: '完成' };
const active = (run: Run) => run.status === 'pending' || run.status === 'running';
const resumable = (run: Run) => ['recoverable', 'cancelled', 'failed', 'partial'].includes(run.status);
const outOfBudget = (run: Run) => run.usage.steps >= run.budget.max_steps || run.usage.elapsed_seconds >= run.budget.max_seconds;
const missingLabels: Record<string, string> = { origin: '出发地', start_at: '出发时间', duration_minutes: '可用时长' };
const errorMessage = (error: unknown) => error instanceof Error ? error.message : '任务操作未完成，请重试。';

export function Runs({ session, requestVersion, archived }: { session: TripSession; requestVersion: number; archived: boolean }) {
  const { runs, busy, error, operation } = useSyncExternalStore(session.subscribeRuns, session.getRunSnapshot);
  const [pollError, setPollError] = useState<string | null>(null);
  const tripId = session.trip.id;
  const polling = runs.filter(active).map(run => `${run.id}:${run.generation}`).join('|');

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const ids = session.trip.runs.filter(active).map(run => run.id);
    if (!ids.length) return;
    const poll = async () => {
      try {
        const results = await Promise.all(ids.map(id => api.getRun(id, tripId)));
        if (!alive) return;
        results.forEach(run => session.acceptRun(run)); setPollError(null);
      } catch (reason) { if (alive) setPollError(`进度暂时无法读取。${errorMessage(reason)}`); }
      if (alive) timer = setTimeout(() => { void poll(); }, 1000);
    };
    void poll();
    return () => { alive = false; clearTimeout(timer); };
    // Switching trips or generations invalidates the pending poll response.
  }, [session, tripId, polling]);

  return <section className="panel run-panel" aria-labelledby="runs-title"><div className="panel-heading"><div><span className="section-number">03</span><h2 id="runs-title">准备工作区</h2></div><button className="subtle" disabled={archived || busy || runs.some(active)} onClick={() => { void session.startRun(); }}>{busy ? '正在提交…' : operation ? '重试准备' : '开始准备'}</button></div><p className="run-description">检查行程条件并汇总已保存内容。本地任务，不会搜索攻略、调用模型或生成路线。</p>
    {error && <div className="message-error" role="alert"><p>{error}</p>{operation && <><p>已发出的请求仍可能完成。重试会沿用同一操作；开始新的准备会生成另一份任务。</p><button className="text-button" disabled={busy} onClick={() => session.forgetRunOperation()}>放弃重试，开始新的准备</button></>}</div>}
    {pollError && <p className="inline-error" role="alert">{pollError}</p>}
    {!runs.length && <p className="run-empty">还没有任务记录。准备完成的内容会保留在当前行程。</p>}
    <div className="run-list">{runs.map(run => {
      const old = run.request_version !== requestVersion || run.status === 'stale';
      const completed = run.checkpoint.completed_stages?.length ?? 0;
      return <article key={run.id} className={`run-card ${old ? 'old-run' : ''}`} aria-label={`准备工作区，${old ? '旧条件记录' : statuses[run.status]}`}><div className="run-top"><strong>{old ? '旧条件记录' : statuses[run.status]}</strong><time dateTime={run.created_at}>{new Date(run.created_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time><span className="badge">条件版本 {run.request_version}</span></div><div className="run-progress"><progress max={3} value={Math.min(completed, 3)} aria-label="工作区准备进度" /><span>{completed}/3 · {stageLabels[run.stage]}</span></div>
        {old && <p className="run-note">行程条件已更新，这份记录保留供查看，不能用于当前条件。</p>}
        {run.status === 'completed' && !old && <p className="run-note">已检查条件并读取保存内容。候选发现、地图和正式方案仍待接入。</p>}
        {run.result && <div className="run-result"><p>{run.result.summary}</p>{run.result.conditions.missing.length > 0 && <p>待补充：{run.result.conditions.missing.map(field => missingLabels[field] ?? field).join('、')}。目的地允许暂空。</p>}<p>已保存想法 {run.result.workspace.messages_count} 条 · 已选地点 {run.result.workspace.selected_count} 个</p></div>}
        {run.status === 'recoverable' && !old && <p className="run-note">上次执行因服务重启中断，可从已保存的检查点继续。</p>}
        {(run.status === 'partial' || run.status === 'failed') && <p className="run-note">任务未完全完成，已保留检查点。{outOfBudget(run) ? '本轮预算已用尽，可以开始新的准备。' : '可以查看记录，或尝试恢复。'}</p>}
        {run.status === 'cancelled' && <p className="run-note">取消状态已确认；恢复时从已保存的检查点继续。</p>}
        <div className="run-bottom"><span>外部调用 {run.usage.external_calls} · 模型 token {run.usage.model_tokens}</span><div>{active(run) && <button disabled={busy} onClick={() => { void session.controlRun(run.id, 'cancel'); }}>取消任务</button>}{!old && resumable(run) && !archived && !outOfBudget(run) && <button disabled={busy} onClick={() => { void session.controlRun(run.id, 'resume'); }}>恢复任务</button>}</div></div>
      </article>;
    })}</div></section>;
}
