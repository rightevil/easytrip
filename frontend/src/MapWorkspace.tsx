import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { api } from './api';
import { AMapCanvas } from './AMapCanvas';
import type { MapPoint } from './AMapCanvas';
import type { Coordinate } from './amap';
import type { OriginSearch, Poi } from './places-types';
import type { TripSession } from './session';

export function MapWorkspace({ session, archived }: { session: TripSession; archived: boolean }) {
  const { value: workspace } = useSyncExternalStore(session.workspace.subscribe, session.workspace.getSnapshot);
  const { value: draft, version } = useSyncExternalStore(session.request.subscribe, session.request.getSnapshot);
  const { snapshot, busy, reading, error, focus } = useSyncExternalStore(session.subscribePlaces, session.getPlacesSnapshot);
  const [picking, setPicking] = useState(false);
  const [originName, setOriginName] = useState('');
  const [query, setQuery] = useState(draft.request.origin ?? '');
  const [city, setCity] = useState(draft.request.destination ?? '');
  const [search, setSearch] = useState<OriginSearch | null>(null);
  const [originOption, setOriginOption] = useState<Poi | null>(null);
  const [picked, setPicked] = useState<Coordinate | null>(null);
  const [searching, setSearching] = useState(false);
  const [originError, setOriginError] = useState<string | null>(null);
  const [refreshConfirm, setRefreshConfirm] = useState(false);
  const searchSequence = useRef(0);
  useEffect(() => { if (!snapshot) void session.readPlaces(); }, [session]);
  useEffect(() => { setSearch(null); setOriginOption(null); setPicked(null); setPicking(false); setSearching(false); setQuery(draft.request.origin ?? ''); searchSequence.current++; }, [draft.request.origin, version]);
  useEffect(() => () => { searchSequence.current++; }, []);
  const origin = workspace.confirmed_origin?.request_origin === draft.request.origin ? workspace.confirmed_origin : null;
  const visible = (snapshot?.points ?? []).filter(point => point.candidate_ids.some(id => workspace.map_visible_ids.includes(id)) && (!workspace.map_city || point.city === workspace.map_city)
    && (workspace.category === 'all' || point.categories.includes(workspace.category)));
  const points: MapPoint[] = visible.map(point => ({ id: point.id, name: point.name, location: point.location }));
  if (origin) {
    const sharedPoint = origin.poi_id ? points.find(point => point.id === origin.poi_id) : null;
    if (sharedPoint) sharedPoint.origin = true;
    else points.push({ id: `origin:${origin.poi_id ?? 'map'}`, name: origin.name, location: origin.location, origin: true });
  }
  const focusedPoiId = snapshot?.matches.find(match => match.candidate_id === focus?.candidate_id)?.poi?.id;
  const focusedPoint = focus ? visible.find(point => point.candidate_ids.includes(focus.candidate_id) || point.id === focusedPoiId) : null;
  const cities = [...new Set([...(snapshot?.points ?? []).map(point => point.city), ...(snapshot?.candidates ?? []).map(candidate => candidate.city)].filter((value): value is string => Boolean(value)))].sort();
  const searchOrigin = async () => {
    if (!query.trim() || query.trim().length > 80 || !city.trim() || searching || busy) return;
    setSearching(true); setOriginError(null); setSearch(null); setOriginOption(null); setPicked(null); setPicking(false);
    let sequence = searchSequence.current;
    try {
      await session.flush();
      sequence = ++searchSequence.current;
      setSearching(true);
      const result = await api.searchOrigin(session.trip.id, query.trim(), city.trim(), session.request.getSnapshot().version);
      if (sequence !== searchSequence.current) return;
      if (result.request_version !== session.request.getSnapshot().version || result.request_origin !== session.request.getSnapshot().value.request.origin) throw new Error('起点条件已改变，请重新搜索。');
      setSearch(result);
    } catch (reason) { if (sequence === searchSequence.current) setOriginError(reason instanceof Error ? reason.message : '起点搜索失败。'); }
    finally { if (sequence === searchSequence.current) setSearching(false); }
  };
  const confirmOrigin = async () => {
    if (originOption && search) {
      if (await session.confirmOrigin({ source: 'amap', search_id: search.search_id, poi_id: originOption.id })) { setOriginOption(null); setSearch(null); }
    } else if (picked && originName.trim()) {
      if (await session.confirmOrigin({ source: 'map', name: originName.trim(), location: picked })) { setPicked(null); setPicking(false); }
    }
  };
  return <section className="map-workspace" aria-label="正式候选地图">
    <p className="discovery-note">只绘制已唯一匹配或人工确认的地点。P2 语义质量仍未验证；地点核对不代表推荐或排程已经可靠。</p>
    <div className="map-toolbar"><label>城市图层<select disabled={busy} value={workspace.map_city ?? ''} onChange={event => session.workspace.update({ ...session.workspace.getSnapshot().value, map_city: event.target.value || null })}><option value="">全部城市</option>{cities.map(value => <option key={value} value={value}>{value}</option>)}</select></label><span>{visible.length} 个地点 · {workspace.selected_ids.length} 项加入行程</span><button disabled={archived || busy || !snapshot} onClick={() => setRefreshConfirm(value => !value)}>刷新此筛选的 Top 8</button></div>
    <p className="discovery-note">Top 8 刷新使用当前条件最新发现轮次中的地点，并应用当前类别和城市筛选。</p>
    {refreshConfirm && <div className="map-refresh-confirm"><p>将地图候选整组替换为当前条件最新发现轮次中，此类别、城市下已确认的前 8 个地点。出发点保留，行程选择不变。</p><button disabled={busy} onClick={() => setRefreshConfirm(false)}>取消刷新</button><button disabled={archived || busy} onClick={() => { void session.mapAction('refresh').then(saved => { if (saved) setRefreshConfirm(false); }); }}>确认刷新 Top 8</button></div>}
    <AMapCanvas points={points} center={workspace.map_center} zoom={workspace.map_zoom} focus={focusedPoint && focus ? { id: focusedPoint.id, sequence: focus.sequence } : null} picking={picking && !archived && !busy}
      onViewport={(map_center, map_zoom) => { if (!archived && !busy) { const current = session.workspace.getSnapshot().value; session.workspace.update({ ...current, map_center, map_zoom }); } }}
      onSelect={id => { const point = visible.find(item => item.id === id); if (point?.candidate_ids[0]) session.focusPlace(point.candidate_ids[0]); }}
      onPick={location => { setPicked(location); setOriginOption(null); setSearch(null); setOriginError(null); }} />
    {reading && <p className="discovery-note" role="status">正在恢复已保存的地图点…</p>}
    {error && <div className="message-error" role="alert"><p>{error}</p><button disabled={busy} onClick={() => { void session.readPlaces(snapshot?.run_id ?? undefined); }}>重新读取地点</button></div>}
    {!visible.length && <p className="discovery-note">当前图层尚无已确认地点。未匹配、歧义与泛称线索仍保留在候选面板。</p>}
    {visible.length > 0 && <div className="map-point-list" aria-label="已展示的地图地点">{visible.map(point => <button key={point.id} aria-pressed={focusedPoint?.id === point.id} onClick={() => session.focusPlace(point.candidate_ids[0])}><strong>{point.name}</strong><small>{point.address || '地址未知'} · {point.activity_ids.length} 个活动</small></button>)}</div>}
    <details className="origin-editor"><summary>{origin ? `已确认出发点：${origin.name}` : '确认出发点坐标'}</summary><p>行程中填写的出发地：{draft.request.origin || '尚未填写'}。文字不会自动推测为坐标或用户位置。</p>{origin && <p>已确认：{origin.name} · GCJ-02 {origin.location.join(', ')}（{origin.source === 'amap' ? '高德地点' : '地图点选'}）</p>}<fieldset disabled={archived || busy || searching}><legend className="sr-only">选择并确认出发点</legend><div className="origin-search-fields"><label>搜索出发地点<input maxLength={80} value={query} onChange={event => setQuery(event.target.value)} /></label><label>起点所在城市<input maxLength={100} value={city} onChange={event => setCity(event.target.value)} placeholder="填写城市，缩小同名地点范围" /></label></div><div className="origin-actions"><button disabled={!query.trim() || query.trim().length > 80 || !city.trim()} onClick={() => { void searchOrigin(); }}>{searching ? '正在搜索…' : '搜索起点'}</button><button onClick={() => { setPicking(value => !value); setSearch(null); setOriginOption(null); setPicked(null); }}>{picking ? '取消地图点选' : '在地图上点选起点'}</button></div></fieldset>{originError && <p className="inline-error" role="alert">{originError}</p>}
      {search && <div className="origin-results">{search.items.map(item => <button disabled={archived || busy} key={item.id} aria-pressed={originOption?.id === item.id} onClick={() => { setOriginOption(item); setOriginName(item.name ?? ''); }}><strong>{item.name || `名称未返回（ID ${item.id}）`}</strong><small>{item.city} · {item.address || '地址未知'} · {item.location.join(', ')}</small></button>)}{!search.items.length && <p>未找到起点；可调整名称、城市或在地图上点选。</p>}</div>}
      {(originOption || picked) && <div className="origin-confirm"><p>将“{draft.request.origin || '尚未填写的出发地'}”对应为以下起点，确认后保存坐标：</p>{originOption ? <p><strong>{originOption.name || `名称未返回（ID ${originOption.id}）`}</strong> · {originOption.address} · GCJ-02 {originOption.location.join(', ')}</p> : <><label>点选起点名称<input disabled={busy} maxLength={200} value={originName} onChange={event => setOriginName(event.target.value)} /></label><small>GCJ-02 {picked?.join(', ')}</small></>}<button className="primary" disabled={archived || busy || !originName.trim()} onClick={() => { void confirmOrigin(); }}>确认这个出发点并保存</button></div>}
    </details>
  </section>;
}
