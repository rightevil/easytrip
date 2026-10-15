import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import type { UsageReport } from './discovery-types';

const labels: Record<string, string> = { model: '模型', xhs: '小红书', amap: '高德', rail: '铁路', unknown: '历史未分类' };
const count = (value: number | null | undefined) => value == null ? '未知' : value.toLocaleString('zh-CN');

export function Usage({ tripId }: { tripId: string }) {
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);
  useEffect(() => {
    sequence.current += 1; setReport(null); setError(null); setLoading(false);
    return () => { sequence.current += 1; };
  }, [tripId]);
  const load = async () => {
    const requested = ++sequence.current;
    setLoading(true); setError(null);
    try {
      const result = await api.usage(tripId);
      if (sequence.current === requested) setReport(result);
    } catch (reason) {
      if (sequence.current === requested) setError(reason instanceof Error ? reason.message : '用量读取失败，请重试。');
    } finally { if (sequence.current === requested) setLoading(false); }
  };
  return <section className="panel run-panel" aria-labelledby="usage-title">
    <div className="panel-heading"><h2 id="usage-title">调用用量</h2><button disabled={loading} onClick={() => { void load(); }}>{loading ? '正在读取…' : '读取最新用量'}</button></div>
    <p>人民币费用未核定。以下仅统计本应用已记账的业务调用；历史手动查询、地图底图与协议握手不在完整统计范围内，不代表账号账单。</p>
    {error && <p role="alert">{error} 当前用量未知。</p>}
    {report && !error && <>
      <p>已记录外部调用 {count(report.external_calls)} · 模型 token {count(report.model_tokens)} · 图片提交 {count(report.images)} 次 · 缓存命中 {count(report.cache_hits)} 次。</p>
      <p>失败 {count(report.failed_calls)} 次 · 结果未知 {count(report.unknown_result_calls)} 次 · 模型用量不完整 {count(report.unknown_cost_calls)} 次 · 在途 {report.actions.filter(action => action.status === 'started').length} 次。</p>
      <p>已记录任务与查询耗时 {count(report.elapsed_seconds)} 秒，按已记录阶段和请求累计，中断缺失时长未补算。</p>
      <ul>{Object.entries(report.providers).map(([provider, usage]) => <li key={provider}>{labels[provider] ?? provider}：{count(usage.external_calls)} 次业务调用</li>)}</ul>
      {report.runs.some(run => run.interrupted_stage_time_unknown) && <p>部分中断阶段耗时未知，不能作为完整耗时。</p>}
      <p>本次读取后产生的调用需重新读取。<a href={`/api/trips/${encodeURIComponent(tripId)}/usage`} target="_blank" rel="noreferrer">查看脱敏用量报告</a></p>
    </>}
  </section>;
}
