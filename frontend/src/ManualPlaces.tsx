import { useSyncExternalStore } from 'react';
import type { Candidate } from './discovery-types';
import type { TripSession } from './session';

const categories: Record<Candidate['category'], string> = { eat: '餐饮', play: '游玩', stay: '住宿' };

export function ManualPlaces({ session, archived }: { session: TripSession; archived: boolean }) {
  const manual = useSyncExternalStore(session.subscribeManualPlace, session.getManualPlaceSnapshot);
  const { snapshot, busy } = useSyncExternalStore(session.subscribePlaces, session.getPlacesSnapshot);
  const { value: workspace } = useSyncExternalStore(session.workspace.subscribe, session.workspace.getSnapshot);
  const { version } = useSyncExternalStore(session.request.subscribe, session.request.getSnapshot);
  const chosen = manual.search?.items.find(item => item.id === manual.poiId);
  const stale = Boolean(manual.search && manual.search.request_version !== version);
  const candidates = snapshot?.manual_candidates ?? [];
  return <section className="manual-places origin-editor" aria-label="用户指定候选地点">
    <h4>手动添加候选地点</h4>
    <p>输入城市和地点名，搜索高德后选择具体地址或分店。确认只保存“用户指定”候选；加入地图和加入行程分别操作，生成路线需前往规划页。</p>
    {archived && <p role="status">已归档，只读查看用户指定地点。</p>}
    <fieldset disabled={archived || busy || Boolean(manual.operation)}><legend className="sr-only">搜索用户指定地点</legend>
      <div className="origin-search-fields"><label>候选地点所在城市<input maxLength={100} value={manual.city} onChange={event => session.editManualPlace({ city: event.target.value })} /></label><label>候选地点名称<input maxLength={80} value={manual.query} onChange={event => session.editManualPlace({ query: event.target.value })} /></label><label>候选地点分类<select value={manual.category} onChange={event => session.editManualPlace({ category: event.target.value as Candidate['category'] })}>{Object.entries(categories).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label></div>
      <div className="origin-actions"><button disabled={manual.searching || !manual.query.trim() || !manual.city.trim()} onClick={() => { void session.searchManualPlace(); }}>{manual.searching ? '正在搜索候选地点…' : '搜索候选地点'}</button></div>
    </fieldset>
    {manual.search && <div className="origin-results" aria-label="候选地点搜索结果">{manual.search.items.map(item => <button key={item.id} disabled={archived || busy || stale || Boolean(manual.operation)} aria-pressed={manual.poiId === item.id} onClick={() => session.editManualPlace({ poiId: item.id })}><strong>{item.name || `名称未返回（ID ${item.id}）`}</strong><small>{item.city || '城市未知'} · {item.address || '地址未知'}</small><small>GCJ-02 {item.location.join(', ')} · POI {item.id}</small></button>)}{!manual.search.items.length && <p>未找到地点，请调整城市或地点名后重新搜索。</p>}</div>}
    {stale && !manual.operation && <p role="status">行程条件已改变，请重新搜索候选地点后再确认。</p>}
    {(chosen || manual.operation) && <div className="origin-confirm"><p>确认保存：<strong>{chosen?.name || manual.query}</strong> · {chosen?.city || manual.city} · {chosen?.address || '地址未知'} · {categories[manual.category]}</p><p>来源：用户指定。没有攻略或评价证据；停留时间、营业和费用未知，可在规划页手填。</p><button className="primary" disabled={archived || busy || stale && !manual.operation} onClick={() => { void session.confirmManualPlace(); }}>{busy ? '正在保存候选…' : manual.operation ? '重试同一候选保存' : '确认地点并保存候选'}</button>{manual.operation && !busy && <><p>保存结果尚未确认，重试将沿用同一操作。重新编辑前请先重试或重新读取地点核对。</p><button disabled={archived} onClick={() => session.continueEditingManualPlace()}>保留输入并重新编辑</button></>}</div>}
    {manual.error && <p className="inline-error" role="alert">{manual.error}</p>}
    {manual.savedName && <p role="status">已保存用户指定候选：{manual.savedName}。尚未自动加入地图或行程。</p>}
    <div className="candidate-list" aria-label="已保存的用户指定候选">{candidates.map(candidate => {
      const mapVisible = workspace.map_visible_ids.includes(candidate.id);
      const sharedVisible = (snapshot?.points ?? []).some(point => point.id === candidate.poi.id && point.candidate_ids.some(id => workspace.map_visible_ids.includes(id)));
      const selected = workspace.selected_ids.includes(candidate.id);
      return <article className="candidate-card" key={candidate.id}><div className="candidate-title"><div><span className="eyebrow">{categories[candidate.category]}</span><h4>{candidate.name}</h4></div><span className="badge">用户指定</span></div><p className="candidate-location">{candidate.city || '城市未知'} · {candidate.poi.address || '地址未知'}</p><p>用户手动添加，无攻略或评价证据。停留时间和费用默认未知，可在规划页手填；营业待核实。</p><small>高德 POI {candidate.poi.id} · GCJ-02 {candidate.poi.location.join(', ')}</small><div className="candidate-actions"><button disabled={archived || busy} aria-pressed={mapVisible || sharedVisible} onClick={() => { if (!mapVisible && sharedVisible) session.focusPlace(candidate.id); else void session.mapAction(mapVisible ? 'remove' : 'add', candidate.id); }}>{mapVisible ? '移出地图' : sharedVisible ? '定位并高亮地图点' : '加入地图'}</button><button disabled={archived || busy} aria-pressed={selected} onClick={() => { void session.mapAction(selected ? 'unselect' : 'select', candidate.id); }}>{selected ? '取消行程选择' : '加入行程'}</button></div><p className="candidate-location">地图{mapVisible ? '已加入' : sharedVisible ? '同地点已展示' : '未加入'} · 行程{selected ? '已选择' : '未选择'}</p></article>;
    })}</div>{!candidates.length && <p>还没有用户指定候选；无需攻略发现任务即可添加。</p>}
  </section>;
}
