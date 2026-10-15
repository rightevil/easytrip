import { useState } from 'react';
import type { PlanningConstraints, PlanningInput, RailSegment } from './planning-types';

const local = (value: string | null) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
const iso = (value: string) => value ? new Date(value).toISOString() : null;

export function RailEditor({ planning, input, disabled, onChange }: { planning: PlanningConstraints; input: PlanningInput | null; disabled: boolean; onChange: (changes: Partial<PlanningConstraints>) => void }) {
  const [date, setDate] = useState('');
  const rails = planning.rail_segments ?? [];
  const stations = input?.station_candidates ?? [];
  const update = (id: string, changes: Partial<RailSegment>, confirm = false) => onChange({ rail_segments: rails.map(rail => rail.id === id ? { ...rail, ...changes, confirmed: confirm } : rail) });
  return <fieldset className="planning-rail-editor" disabled={disabled}><legend>铁路班次与两端接驳</legend><p>只接收你明确确认的车站、班次和时刻。未确认时保存待定草案；本功能不查询余票、不购票，也不保证准点。先在“地点与地图”手动添加具体火车站（可归为游玩候选），无需加入游玩选择。</p>
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
        <label><input type="checkbox" checked={rail.confirmed} disabled={!ready} onChange={event => update(rail.id, {}, event.target.checked)} />我已确认这两站、车次和完整时刻</label>
        {!rail.confirmed && <p role="status">待确认铁路：不能据此声明能赶上或已完成购票。</p>}
        <button onClick={() => onChange({ rail_segments: rails.filter(item => item.id !== rail.id) })}>移除此铁路段</button>
      </fieldset>;
    })}
  </fieldset>;
}
