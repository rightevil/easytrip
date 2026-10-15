import type { LodgingSupplement, Plan, PlanningInput, PlanningConstraints } from './planning-types';

export interface LodgingSelection {
  input: PlanningInput | null;
  planning: PlanningConstraints;
  disabled: boolean;
  onSelect: (value: LodgingSupplement) => void;
}

export function LodgingSuggestions({ plan, date, selection }: { plan: Plan; date: string; selection?: LodgingSelection }) {
  const hotels = plan.payload.supplements?.filter((place): place is LodgingSupplement => 'lodging_window' in place && place.lodging_window.date === date) ?? [];
  if (!hotels.length) return null;
  return <section className="planning-night" aria-label={`${date} 晚住宿建议`}><h5>{date} 晚住宿建议</h5>
    <p>候选与原文只支持位置和历史体验；历史价格不是当晚可订房价。房态、当晚房价、入住、退房和行李寄存条件均未知，选择后需重新校验接驳。</p>
    {hotels.map(hotel => {
      const available = selection?.input?.lodging_candidates?.some(place => place.candidate_id === hotel.candidate_id && place.poi.id === hotel.poi.id);
      const validNight = selection?.planning.day_windows?.slice(0, -1).some(day => day.date === date);
      const selected = selection?.planning.overnight_stays?.some(stay => stay.date === date && stay.candidate_id === hotel.candidate_id);
      return <article key={hotel.candidate_id}><h6>{hotel.name}</h6><p>{hotel.poi.city} · {hotel.poi.address || '地址未知'} · 高德 POI {hotel.poi.id}</p><p>{hotel.lodging_window.anchor_name}附近 · 只建议 {date} 晚</p>
        {hotel.source === 'user' && <p>用户指定，无攻略或评价证据。</p>}
        <details><summary>住宿原文与评价证据</summary>{hotel.sources.map((source, index) => <div key={index}><strong>{typeof source.title === 'string' ? source.title : '原文来源'}</strong><blockquote>{typeof source.text === 'string' ? source.text : '正文未知'}</blockquote></div>)}{hotel.evidence.map((evidence, index) => <blockquote key={index}>{typeof evidence.quote === 'string' ? evidence.quote : '引用未知'}</blockquote>)}{!hotel.sources.length && !hotel.evidence.length && <p>没有可读来源，不能据此声称推荐。</p>}</details>
        {selection && <button disabled={selection.disabled || !available || !validNight || selected} onClick={() => selection.onSelect(hotel)}>{selected ? '这一晚已选择此酒店' : '选择为这一晚酒店'}</button>}
      </article>;
    })}
  </section>;
}
