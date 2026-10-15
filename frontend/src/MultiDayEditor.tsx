import { useState } from 'react';
import type { DayWindow, PlanningConstraints, PlanningInput } from './planning-types';
import { validCoordinate } from './amap';

const toLocal = (value: string | null) => value ? new Date(new Date(value).getTime() - new Date(value).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
const toIso = (value: string) => value ? new Date(value).toISOString() : null;
const followingDate = (value: string) => { const date = new Date(`${value}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + 1); return date.toISOString().slice(0, 10); };

export function MultiDayEditor({ planning, input, disabled, onChange }: { planning: PlanningConstraints; input: PlanningInput | null; disabled: boolean; onChange: (change: Partial<PlanningConstraints>) => void }) {
  const [firstDate, setFirstDate] = useState('');
  const windows = planning.day_windows ?? [];
  const stays = planning.overnight_stays ?? [];
  const hotels = [...new Map([...(input?.lodging_candidates ?? []), ...(input?.lodgings ?? [])].filter(place => place.category === 'stay' && validCoordinate(place.poi.location)).map(place => [place.candidate_id, place])).values()];
  const changeWindow = (index: number, changes: Partial<DayWindow>) => onChange({ day_windows: windows.map((day, i) => i === index ? { ...day, ...changes } : day) });
  const removeLast = () => {
    const remaining = windows.slice(0, -1);
    const nights = remaining.slice(0, -1).map(day => day.date);
    onChange({ day_windows: remaining, overnight_stays: stays.filter(stay => nights.includes(stay.date)), activity_constraints: planning.activity_constraints.map(constraint => constraint.date === windows.at(-1)?.date ? { ...constraint, date: null } : constraint) });
  };
  const railUsesLastDate = planning.rail_segments?.some(rail => rail.date === windows.at(-1)?.date || rail.arrival_at && new Intl.DateTimeFormat('sv-SE', { timeZone: planning.timezone }).format(new Date(rail.arrival_at)) === windows.at(-1)?.date);
  const lastProtected = railUsesLastDate || planning.activity_constraints.some(constraint => constraint.date === windows.at(-1)?.date && (constraint.locked || constraint.state === 'completed'));
  return <fieldset className="planning-multiday" disabled={disabled}><legend>多日窗口与逐晚酒店（可选）</legend>
    <p className="discovery-note">单日行程可保持此处为空。多日请逐日明确窗口并为活动分配日期；空时间保持未知，不自动推定酒店入住、退房或行李寄存时间。规划时区：{planning.timezone}。</p>
    {!windows.length ? <div className="planning-progress-actions"><label>首日日期<input type="date" value={firstDate} onChange={event => setFirstDate(event.target.value)} /></label><button disabled={!firstDate} onClick={() => onChange({ day_windows: [{ date: firstDate, start_at: null, end_at: null }] })}>添加首日日窗</button></div> : <>
      {windows.map((day, index) => <fieldset className="planning-day-window" key={day.date}><legend>{day.date} 日窗</legend><div className="planning-fields"><label>当日开始（本机时区输入）<input type="datetime-local" value={toLocal(day.start_at)} onChange={event => changeWindow(index, { start_at: toIso(event.target.value) })} /></label><label>当日结束（本机时区输入）<input type="datetime-local" value={toLocal(day.end_at)} onChange={event => changeWindow(index, { end_at: toIso(event.target.value) })} /></label></div>{(!day.start_at || !day.end_at) && <p role="status">{day.date} 日窗时间未知，补齐前保留草案。</p>}</fieldset>)}
      <div className="planning-progress-actions"><button disabled={windows.length >= 14} onClick={() => onChange({ day_windows: [...windows, { date: followingDate(windows.at(-1)!.date), start_at: null, end_at: null }] })}>添加下一日日窗</button><button disabled={lastProtected} onClick={removeLast}>移除末日日窗及其日期分配和失效住宿绑定</button></div>
      {windows.length === 1 && <p>当前仅有一个日期窗口；继续添加相邻日期以规划多日行程。</p>}
      {windows.slice(0, -1).map(day => {
        const binding = stays.find(stay => stay.date === day.date);
        const hotel = hotels.find(place => place.candidate_id === binding?.candidate_id);
        return <fieldset className="planning-night" key={day.date}><legend>{day.date} 晚住宿</legend><label>本晚酒店候选<select value={binding?.candidate_id ?? ''} onChange={event => onChange({ overnight_stays: [...stays.filter(stay => stay.date !== day.date), ...(event.target.value ? [{ date: day.date, candidate_id: event.target.value }] : [])] })}><option value="">缺少本晚酒店（尚未选择）</option>{binding && !hotel && <option value={binding.candidate_id}>已绑定地点未返回，请重新核对</option>}{hotels.map(place => <option key={place.candidate_id} value={place.candidate_id}>{place.name} · {place.city || '城市未知'} · {place.poi.address || '地址未知'}</option>)}</select></label>{hotel ? <p>{hotel.name} · {hotel.poi.address || '地址未知'} · {hotel.source === 'user' ? '用户指定，无攻略或评价证据' : `${hotel.sources.length} 个原文来源，仍需核实`}。</p> : <p role="status">{day.date} 晚缺少已核对酒店，仅显示本晚缺口，不沿用其他晚的选择。</p>}<p className="discovery-note">选择只绑定这一晚，不代表已预订。房态、当晚房价、入住、退房和行李寄存均待核实。可在“地点与地图”手动添加住宿分类候选。</p></fieldset>;
      })}
    </>}
  </fieldset>;
}
