import { useEffect, useRef, useState } from 'react';
import type { PlanningConstraints, PlanningInput, RailSearchItem, RailSearchResult, RailSegment } from './planning-types';

const local = (value: string | null) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
const iso = (value: string) => value ? new Date(value).toISOString() : null;

export function RailEditor({ planning, input, disabled, tripId, requestVersion, saved, onSearch, onChange }: { planning: PlanningConstraints; input: PlanningInput | null; disabled: boolean; tripId: string; requestVersion: number; saved: boolean; onSearch: (rail: RailSegment) => Promise<RailSearchResult>; onChange: (changes: Partial<PlanningConstraints>) => void }) {
  const [date, setDate] = useState('');
  const rails = planning.rail_segments ?? [];
  const stations = input?.station_candidates ?? [];
  const update = (id: string, changes: Partial<RailSegment>, confirm = false) => onChange({ rail_segments: rails.map(rail => rail.id === id ? { ...rail, ...changes, confirmed: confirm } : rail) });
  return <fieldset className="planning-rail-editor" disabled={disabled}><legend>铁路班次与两端接驳</legend><p>选择两端具体车站后，可通过 12306 MCP 查询车次与余票。选中只填写待确认草稿，你仍需核对班次和完整时刻；未确认时保存待定草案。本功能不购票，也不保证准点。先在“地点与地图”手动添加具体火车站（可归为游玩候选），无需加入游玩选择。</p>
    <div className="planning-progress-actions"><label>铁路出发日期<input type="date" value={date} onChange={event => setDate(event.target.value)} /></label><button disabled={!date || rails.length >= 28} onClick={() => onChange({ rail_segments: [...rails, { id: crypto.randomUUID(), date, departure_station_id: null, arrival_station_id: null, train_number: null, departure_at: null, arrival_at: null, confirmed: false, source: 'user', boarding_buffer_minutes: 30, arrival_buffer_minutes: 20, cost_cny: null }] })}>添加待定铁路段</button></div>
    {rails.map((rail, index) => {
      const ready = Boolean(rail.train_number?.trim() && rail.departure_station_id && rail.arrival_station_id && rail.departure_station_id !== rail.arrival_station_id && rail.departure_at && rail.arrival_at && Date.parse(rail.arrival_at) > Date.parse(rail.departure_at));
      return <fieldset key={rail.id}><legend>铁路段 {index + 1} · {rail.date}</legend><div className="planning-fields">
        <label>此铁路段出发日期<input type="date" value={rail.date} onChange={event => update(rail.id, { date: event.target.value })} /></label>
        {(['departure_station_id', 'arrival_station_id'] as const).map((field, i) => <label key={field}>{i ? '到达火车站' : '出发火车站'}<select value={rail[field] ?? ''} onChange={event => update(rail.id, { [field]: event.target.value || null })}><option value="">待选择具体车站</option>{rail[field] && !stations.some(station => station.candidate_id === rail[field]) && <option value={rail[field]!}>原车站未返回，请重新核对</option>}{stations.map(station => <option key={station.candidate_id} value={station.candidate_id}>{station.poi.name} · {station.poi.city} · {station.poi.address || '地址未知'}</option>)}</select></label>)}
        <label>用户确认车次<input value={rail.train_number ?? ''} maxLength={120} onChange={event => update(rail.id, { train_number: event.target.value || null })} /></label>
        <label>铁路出发（本机时区输入）<input type="datetime-local" value={local(rail.departure_at)} onChange={event => update(rail.id, { departure_at: iso(event.target.value) })} /></label>
        <label>铁路到达（本机时区输入，跨日填实际日期）<input type="datetime-local" value={local(rail.arrival_at)} onChange={event => update(rail.id, { arrival_at: iso(event.target.value) })} /></label>
        <label>进站与候车预留（分钟）<input type="number" min="0" max="240" value={rail.boarding_buffer_minutes} onChange={event => update(rail.id, { boarding_buffer_minutes: Number(event.target.value) })} /></label>
        <label>出站与接驳预留（分钟）<input type="number" min="0" max="240" value={rail.arrival_buffer_minutes} onChange={event => update(rail.id, { arrival_buffer_minutes: Number(event.target.value) })} /></label>
        <label>已知铁路费用（元，空值未知）<input type="number" min="0" value={rail.cost_cny ?? ''} onChange={event => update(rail.id, { cost_cny: event.target.value === '' ? null : Number(event.target.value) })} /></label>
      </div><p>出发/到达均按完整日期保存。缓冲初值是程序预留，请根据车站条件核实并调整；费用不表示实时票价或已购票。</p>
        <RailSearch key={JSON.stringify([rail.departure_station_id, rail.arrival_station_id].map(id => stations.find(station => station.candidate_id === id)?.poi ?? null))} rail={rail} tripId={tripId} requestVersion={requestVersion} saved={saved} disabled={disabled} onSearch={onSearch} onSelect={item => update(rail.id, { train_number: item.train_number, departure_at: item.departure_at, arrival_at: item.arrival_at, cost_cny: null })} />
        <label><input type="checkbox" checked={rail.confirmed} disabled={!ready} onChange={event => update(rail.id, {}, event.target.checked)} />我已确认这两站、车次和完整时刻</label>
        {!rail.confirmed && <p role="status">待确认铁路：不能据此声明能赶上或已完成购票。</p>}
        <button onClick={() => onChange({ rail_segments: rails.filter(item => item.id !== rail.id) })}>移除此铁路段</button>
      </fieldset>;
    })}
  </fieldset>;
}

function RailSearch({ rail, tripId, requestVersion, saved, disabled, onSearch, onSelect }: { rail: RailSegment; tripId: string; requestVersion: number; saved: boolean; disabled: boolean; onSearch: (rail: RailSegment) => Promise<RailSearchResult>; onSelect: (item: RailSearchItem) => void }) {
  const [result, setResult] = useState<RailSearchResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const sequence = useRef(0);
  const criteria = `${tripId}|${rail.date}|${rail.departure_station_id}|${rail.arrival_station_id}`;
  const latest = useRef({ criteria, requestVersion, saved, disabled });
  latest.current = { criteria, requestVersion, saved, disabled };
  useEffect(() => { sequence.current++; setResult(null); setError(null); setLoading(false); return () => { sequence.current++; }; }, [criteria]);
  const validResult = result && result.trip_id === tripId && result.request_version === requestVersion && result.date === rail.date && result.departure_station_id === rail.departure_station_id && result.arrival_station_id === rail.arrival_station_id;
  const query = async () => {
    if (loading || disabled || !rail.date || !rail.departure_station_id || !rail.arrival_station_id || rail.departure_station_id === rail.arrival_station_id) return;
    const attempt = ++sequence.current;
    setLoading(true); setResult(null); setError(null);
    try {
      const value = await onSearch(rail);
      if (attempt !== sequence.current || latest.current.criteria !== criteria || latest.current.disabled) return;
      if (value.trip_id !== tripId || value.request_version !== latest.current.requestVersion || !latest.current.saved || value.date !== rail.date || value.departure_station_id !== rail.departure_station_id || value.arrival_station_id !== rail.arrival_station_id) throw new Error('查询条件已变化，请重新查询车次。');
      setResult(value);
    } catch (reason) { if (attempt === sequence.current) setError(reason instanceof Error ? reason.message : '铁路查询失败，请重试。'); }
    finally { if (attempt === sequence.current) setLoading(false); }
  };
  const select = (item: RailSearchItem) => {
    if (!validResult || !latest.current.saved || latest.current.disabled || latest.current.criteria !== criteria || latest.current.requestVersion !== result.request_version) return;
    onSelect(item);
    setResult(null);
  };
  const displayTime = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return <section aria-label="真实铁路车次查询"><button disabled={loading || !rail.date || !rail.departure_station_id || !rail.arrival_station_id || rail.departure_station_id === rail.arrival_station_id} onClick={() => { void query(); }}>{loading ? '正在查询 12306…' : '查询 12306 车次与余票'}</button>
    {error && <p role="alert" className="inline-error">{error}</p>}
    {validResult && <><p>来源：12306 MCP · {result.departure_station.name}（{result.departure_station.code}）→ {result.arrival_station.name}（{result.arrival_station.code}）· 查询于 {displayTime(result.queried_at)}（北京时间）</p><p>余票和参考票价以本次查询为准，购票时需重新核对。选择车次会清空已知铁路费用，请按实际费用重新填写。</p><p>匹配所选两站 {result.matched_count} 班，最多展示 20 班；已排除其他车站 {result.excluded_other_station_count} 班。</p>{!result.items.length && <p role="status">{result.excluded_other_station_count ? '上游返回其他车站的车次，本次未返回所选两站车次。' : '本次查询未返回所选两站车次。'}可调整日期或车站，也可保留手工填写的铁路段。</p>}{result.items.map(item => <article key={`${item.train_number}:${item.departure_at}`}><strong>{item.train_number}</strong><p>{displayTime(item.departure_at)} → {displayTime(item.arrival_at)}（北京时间）{item.departure_at.slice(0, 10) !== item.arrival_at.slice(0, 10) ? ' · 跨日到达' : ''} · {item.duration_minutes} 分钟</p><p>{item.seats.length ? item.seats.map(seat => `${seat.name}：${seat.remaining || '余票未知'} · ${seat.price_cny === null ? '票价未知' : `参考 ¥${seat.price_cny}`}`).join('；') : '座席与票价未知'}</p><button disabled={!saved} onClick={() => select(item)}>选用 {item.train_number} 为待确认草稿</button></article>)}</>}
  </section>;
}
