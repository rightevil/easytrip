import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api } from './api';
import type { Trip, TripRequest, TripSummary, Workspace } from './api';
import type { Autosave, SaveState } from './autosave';
import { TripSession } from './session';

const labels = { saved: '已保存', dirty: '等待保存', saving: '保存中', error: '保存失败', conflict: '发生版本冲突' };
const transports: [TripRequest['transport'], string][] = [['mixed', '灵活组合'], ['walking', '步行'], ['transit', '公共交通'], ['bicycling', '骑行'], ['electrobike', '电动车'], ['driving', '自驾']];
const tabs: [Workspace['active_tab'], string][] = [['overview', '行程概览'], ['map', '地点与地图'], ['plan', '时间安排']];
const categories: [Workspace['category'], string][] = [['all', '全部'], ['play', '游玩'], ['eat', '餐饮'], ['stay', '住宿']];
const message = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请重试。';
const dateLabel = (value: string) => new Date(value).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' });
const localDateTime = (value: string | null) => {
  if (!value) return '';
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
function useAutosave<T>(save: Autosave<T>) { return useSyncExternalStore(save.subscribe, save.getSnapshot); }

export function App() {
  const [items, setItems] = useState<TripSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [active, setActive] = useState<TripSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [leave, setLeave] = useState<(() => Promise<void>) | null>(null);
  const [deleting, setDeleting] = useState(false);
  const sessions = useRef(new Map<string, TripSession>());
  const operation = useRef<string | null>(null);
  const loadSequence = useRef(0);
  const busyRef = useRef(false);
  const activeRef = useRef(active);
  activeRef.current = active;

  const upsert = useCallback((trip: Trip) => {
    setItems(previous => {
      const found = previous.find(item => item.id === trip.id);
      return found ? previous.map(item => item.id === trip.id ? trip : item) : [trip, ...previous];
    });
  }, []);
  const makeSession = useCallback((trip: Trip) => {
    const session = new TripSession(trip, upsert);
    sessions.current.set(trip.id, session);
    return session;
  }, [upsert]);
  const refreshList = useCallback(async (archived = includeArchived, append = false) => {
    const result = await api.list(archived, append ? items.length : 0);
    setItems(previous => append ? [...previous, ...result.items.filter(item => !previous.some(existing => existing.id === item.id))] : result.items);
    setTotal(result.total);
  }, [includeArchived, items.length]);

  useEffect(() => {
    let alive = true;
    void api.list().then(async result => {
      if (!alive) return;
      setItems(result.items); setTotal(result.total);
      const remembered = localStorage.getItem('easytrip.active');
      const initial = result.items.find(item => item.id === remembered) ?? result.items[0];
      if (initial) {
        const trip = await api.get(initial.id);
        if (alive) setActive(makeSession(trip));
      }
    }).catch(reason => { if (alive) setError(message(reason)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; sessions.current.forEach(session => session.dispose()); };
  }, [makeSession]);
  useEffect(() => {
    if (active) localStorage.setItem('easytrip.active', active.trip.id);
    const warn = (event: BeforeUnloadEvent) => {
      if ([...sessions.current.values()].some(session => session.dirty || session.composer)) { event.preventDefault(); }
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [active]);

  const select = async (id: string) => {
    const sequence = ++loadSequence.current;
    const trip = await api.get(id);
    if (sequence !== loadSequence.current) return;
    const cached = sessions.current.get(id);
    if (cached && !cached.dirty) cached.accept(trip);
    setActive(cached ?? makeSession(trip));
  };
  const guarded = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true);
    setError(null);
    const current = activeRef.current;
    try { await current?.flush(); }
    catch (reason) { setError(message(reason)); setLeave(() => action); busyRef.current = false; setBusy(false); return; }
    try { await action(); }
    catch (reason) { setError(message(reason)); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const create = () => guarded(async () => {
    operation.current ??= crypto.randomUUID();
    const trip = await api.create(operation.current);
    operation.current = null;
    upsert(trip); setTotal(value => value + 1); setActive(makeSession(trip));
  });
  const archive = () => guarded(async () => {
    if (!active) return;
    const trip = await api.archive(active.trip.id, active.request.getSnapshot().version, active.trip.status !== 'archived');
    active.accept(trip); upsert(trip); setActive(null);
    await refreshList();
  });
  const remove = () => guarded(async () => {
    if (!active) return;
    await api.remove(active.trip.id, active.request.getSnapshot().version);
    active.dispose(); sessions.current.delete(active.trip.id); setActive(null); setDeleting(false);
    localStorage.removeItem('easytrip.active'); await refreshList();
  });
  const resolveLeave = async (saveFirst: boolean) => {
    const action = leave;
    if (!action) return;
    setBusy(true); setError(null);
    try {
      if (saveFirst) await active?.retry();
      await action(); setLeave(null);
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  };

  return <div className="shell">
    <a className="skip-link" href="#workspace">跳到行程工作区</a>
    <aside className="sidebar" aria-label="行程列表">
      <a className="brand" href="/" aria-label="EasyTrip 首页"><span className="brand-mark" aria-hidden="true">e<span>↗</span></span><span>EasyTrip<small>轻松一点，出发吧</small></span></a>
      <div className="sidebar-heading"><span className="eyebrow">我的行程</span><span className="count">{total}</span></div>
      <button className="primary new-trip" disabled={busy || loading} onClick={create}><span aria-hidden="true">＋</span> 新建行程</button>
      <label className="archive-filter"><input type="checkbox" checked={includeArchived} onChange={event => {
        const value = event.target.checked;
        setIncludeArchived(value); setLoading(true);
        void api.list(value).then(result => { setItems(result.items); setTotal(result.total); }).catch(reason => setError(message(reason))).finally(() => setLoading(false));
      }} />显示归档行程</label>
      <nav className="trip-list" aria-label="选择行程">
        {loading && <p className="muted sidebar-note" role="status">正在读取行程…</p>}
        {!loading && items.filter(trip => includeArchived || trip.status !== 'archived').map(trip => <button key={trip.id} disabled={busy} className={`trip-card ${active?.trip.id === trip.id ? 'active' : ''}`} aria-current={active?.trip.id === trip.id ? 'page' : undefined} onClick={() => { if (active?.trip.id !== trip.id) void guarded(() => select(trip.id)); }}>
          <span className="trip-icon" aria-hidden="true">↗</span><span className="trip-info"><strong>{trip.name}</strong><small>{trip.request?.destination || '目的地待定'} · {dateLabel(trip.updated_at)}{trip.status === 'archived' ? ' · 已归档' : ''}</small></span><span className="trip-arrow" aria-hidden="true">›</span>
        </button>)}
        {!loading && !items.length && <p className="muted sidebar-note">还没有行程。<br />从一个想法开始。</p>}
        {items.length < total && <button className="text-button" disabled={loading || busy} onClick={() => { void refreshList(includeArchived, true).catch(reason => setError(message(reason))); }}>加载更多行程</button>}
      </nav>
      <div className="sidebar-footer"><span className="local-dot" aria-hidden="true" />本机保存<span className="muted">SQLite</span><a href="/p0/map" target="_blank" rel="noreferrer">P0 地图验证 ↗</a></div>
    </aside>
    <main id="workspace" className="main" tabIndex={-1}>
      {error && <div className="global-error" role="alert"><span>{error}</span><button className="text-button" onClick={() => { setError(null); void refreshList().catch(reason => setError(message(reason))); }}>重新读取</button><button className="icon-button" aria-label="关闭提示" onClick={() => setError(null)}>×</button></div>}
      {active ? <TripView key={active.trip.id} session={active} archived={active.trip.status === 'archived'} onTrip={upsert} onArchive={archive} onDelete={() => setDeleting(true)} busy={busy} /> : <div className="welcome">
        <span className="eyebrow">给下一段旅程留一点空间</span><h1>从想去的地方，<br />或一个模糊的想法开始。</h1><p>保存你的出发地、时间和偏好，<br />随时回来继续准备。</p><button className="primary" onClick={create} disabled={busy || loading}>{loading ? '正在打开工作区…' : '创建我的第一份行程'}<span aria-hidden="true">↗</span></button><div className="welcome-orbit" aria-hidden="true"><span>出发</span><i /><b>✳</b></div>
      </div>}
    </main>
    {leave && <Modal title="当前编辑还未保存" onClose={() => setLeave(null)}><p>本地编辑仍在当前行程中。保存失败或版本冲突需要处理后才能可靠恢复。</p><div className="modal-actions"><button onClick={() => setLeave(null)}>留在当前行程</button><button disabled={busy} onClick={() => { void resolveLeave(false); }}>保留本地编辑并继续</button><button className="primary" disabled={busy} onClick={() => { void resolveLeave(true); }}>重试保存后继续</button></div></Modal>}
    {deleting && active && <Modal title="删除这份行程？" onClose={() => setDeleting(false)}><p>“{active.trip.name}”的条件、对话和任务记录会一并删除，此操作无法撤销。你也可以归档保留它。</p><div className="modal-actions"><button onClick={() => setDeleting(false)}>保留行程</button><button className="danger" disabled={busy} onClick={remove}>确认删除</button></div></Modal>}
  </div>;
}

function TripView({ session, archived, onTrip, onArchive, onDelete, busy }: { session: TripSession; archived: boolean; onTrip: (trip: Trip) => void; onArchive: () => void; onDelete: () => void; busy: boolean }) {
  const requestSave = useAutosave(session.request);
  const workspaceSave = useAutosave(session.workspace);
  const draft = requestSave.value;
  const workspace = workspaceSave.value;
  const [rename, setRename] = useState(false);
  const [resolveError, setResolveError] = useState<string | null>(null);
  const update = (field: keyof TripRequest, value: TripRequest[keyof TripRequest]) => session.request.update({ ...draft, request: { ...draft.request, [field]: value } });
  const resolve = async (kind: 'request' | 'workspace', keep: boolean) => {
    setResolveError(null);
    try {
      const latest = await api.get(session.trip.id);
      if (kind === 'request') {
        session.request.resolve({ name: latest.name, request: latest.request }, latest.request_version, keep);
        if (keep) await session.request.flush();
        else { session.trip = { ...session.trip, name: latest.name, request: latest.request, request_version: latest.request_version }; onTrip(session.trip); }
      } else {
        session.workspace.resolve(latest.workspace, latest.workspace_version, keep);
        if (keep) await session.workspace.flush();
      }
    } catch (reason) { setResolveError(message(reason)); }
  };
  const status = requestSave.status === 'conflict' || workspaceSave.status === 'conflict' ? 'conflict' : requestSave.status === 'error' || workspaceSave.status === 'error' ? 'error' : requestSave.status === 'saving' || workspaceSave.status === 'saving' ? 'saving' : session.dirty ? 'dirty' : 'saved';

  return <>
    <header className="workspace-header"><div><span className="eyebrow">TRIP WORKSPACE</span><div className="title-row"><h1>{draft.name}</h1><button className="icon-button" aria-label="重命名行程" disabled={archived} onClick={() => setRename(value => !value)}>✎</button>{archived && <span className="badge">已归档</span>}</div><p className="muted">目的地可以晚一点决定，先把想法放在这里。</p></div><div className="header-actions"><span className={`save-pill ${status}`} role="status"><span aria-hidden="true">{status === 'saved' ? '✓' : status === 'saving' ? '◌' : '·'}</span>{labels[status]}</span><button className="subtle" disabled={busy} onClick={onArchive}>{archived ? '恢复行程' : '归档'}</button><button className="icon-button" aria-label="删除行程" disabled={busy} onClick={onDelete}>×</button></div></header>
    {rename && <div className="rename-row"><label>行程名称<input autoFocus value={draft.name} maxLength={120} onChange={event => session.request.update({ ...draft, name: event.target.value })} onBlur={() => { if (draft.name.trim()) setRename(false); }} onKeyDown={event => { if (event.key === 'Enter' && draft.name.trim()) setRename(false); }} /></label><button onClick={() => setRename(false)}>完成</button></div>}
    <SaveNotice state={requestSave} kind="行程条件" retry={() => session.request.retry()} resolve={keep => resolve('request', keep)} />
    <SaveNotice state={workspaceSave} kind="工作区" retry={() => session.workspace.retry()} resolve={keep => resolve('workspace', keep)} />
    {resolveError && <p className="inline-error" role="alert">{resolveError}</p>}
    <div className="work-grid"><section className="conditions panel" aria-labelledby="conditions-title"><div className="panel-heading"><div><span className="section-number">01</span><h2 id="conditions-title">这次怎么出发</h2></div><span className="muted">可随时修改</span></div><fieldset disabled={archived}><legend className="sr-only">行程条件</legend>
      <label>目的地 <span className="optional">可不填</span><input value={draft.request.destination ?? ''} placeholder="想去哪？也可以先留白" maxLength={200} onChange={event => update('destination', event.target.value || null)} /></label>
      <label>从哪里出发<input value={draft.request.origin ?? ''} placeholder="城市、车站或地址" maxLength={200} onChange={event => update('origin', event.target.value || null)} /></label>
      <div className="field-pair"><label>出发日期与时间<input type="datetime-local" value={localDateTime(draft.request.start_at)} onChange={event => update('start_at', event.target.value ? new Date(event.target.value).toISOString() : null)} /></label><label>可用时长（小时）<input type="number" min="0.5" step="0.5" max="336" value={draft.request.duration_minutes === null ? '' : draft.request.duration_minutes / 60} placeholder="例如 8" onChange={event => update('duration_minutes', event.target.value ? Math.round(event.target.valueAsNumber * 60) : null)} /></label></div>
      <label>交通方式<select value={draft.request.transport} onChange={event => session.request.update({ ...draft, request: { ...draft.request, transport: event.target.value as TripRequest['transport'], transport_source: 'user' } })}>{transports.map(([key, label]) => <option key={key} value={key}>{label}{key === 'mixed' && draft.request.transport_source === 'default' ? '（默认）' : ''}</option>)}</select></label>
      <div className="field-pair"><label>总预算（元）<input type="number" min="0" max="10000000" step="1" value={draft.request.budget_cny ?? ''} placeholder="不限预算" onChange={event => update('budget_cny', event.target.value ? event.target.valueAsNumber : null)} /></label><label>同行人数<input type="number" min="1" max="100" step="1" value={draft.request.participants || ''} onChange={event => update('participants', event.target.value ? event.target.valueAsNumber : 0)} /></label></div>
      <label>偏好与特别安排<textarea rows={3} maxLength={4000} value={draft.request.preferences} placeholder="比如少走路、爱逛书店、带孩子，或不想太赶……" onChange={event => update('preferences', event.target.value)} /></label>
    </fieldset><div className="form-foot"><span>条件自动保存</span><span>版本 {requestSave.version}</span></div></section>
    <div className="workspace-content"><section className="panel exploration" aria-label="行程工作区"><div className="tabs" role="tablist" aria-label="工作区页面">{tabs.map(([key, label]) => <button role="tab" id={`tab-${key}`} aria-controls="workspace-panel" aria-selected={workspace.active_tab === key} key={key} onClick={() => session.workspace.update({ ...workspace, active_tab: key })}>{label}</button>)}</div><div className="workspace-panel" id="workspace-panel" role="tabpanel" aria-labelledby={`tab-${workspace.active_tab}`}>
      {workspace.active_tab === 'overview' && <><div className="summary-banner"><span className="eyebrow">下一段旅程</span><h2>{draft.request.destination || '目的地，还可以慢慢想'}</h2><p>{draft.request.origin ? `从${draft.request.origin}出发` : '出发地待补充'}<span> · </span>{draft.request.start_at ? new Date(draft.request.start_at).toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '时间待定'}</p></div><div className="section-subheading"><h3>候选地点</h3><CategoryFilter value={workspace.category} onChange={category => session.workspace.update({ ...workspace, category })} /></div><Empty icon="✳" title="把想去的地方，留给下一步" description="候选发现尚未接入。当前可以先保存条件和想法，地点确认后将在这里展示。" /></>}
      {workspace.active_tab === 'map' && <><div className="section-subheading"><h3>地点与地图</h3><CategoryFilter value={workspace.category} onChange={category => session.workspace.update({ ...workspace, category })} /></div><div className="map-empty"><div className="map-grid" aria-hidden="true" /><Empty icon="⌖" title="地图尚未接入" description="确认地点后，这里会展示本行程的地图和路线。" /></div><MapPreferences workspace={workspace} onChange={value => session.workspace.update(value)} /><a className="text-link" href="/p0/map" target="_blank" rel="noreferrer">打开独立的 P0 地图验证 ↗</a></>}
      {workspace.active_tab === 'plan' && <><div className="section-subheading"><h3>行程时间表</h3><span className="badge">尚未接入</span></div><Empty icon="≋" title="为旅程留一点空白" description="时间安排与方案生成尚未接入。已保存的条件会作为后续规划的起点。" /></>}
    </div></section><Chat session={session} archived={archived} onTrip={onTrip} /></div></div>
  </>;
}

function SaveNotice<T>({ state, kind, retry, resolve }: { state: SaveState<T>; kind: string; retry: () => Promise<void>; resolve: (keep: boolean) => Promise<void> }) {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (state.status !== 'error' && state.status !== 'conflict') return null;
  const run = async (action: () => Promise<void>) => { setWorking(true); setError(null); try { await action(); } catch (reason) { setError(message(reason)); } finally { setWorking(false); } };
  return <div className="save-notice" role="alert"><div><strong>{kind}{labels[state.status]}</strong><p>{state.status === 'conflict' ? '另一处修改了同一份数据。本地编辑仍保留；重新保存会把本地编辑应用到最新版本。' : state.error}</p>{error && <p>{error}</p>}</div><div className="notice-actions">{state.status === 'conflict' ? <><button disabled={working} onClick={() => { void run(() => resolve(false)); }}>放弃本地编辑，加载最新</button><button disabled={working} className="primary" onClick={() => { void run(() => resolve(true)); }}>重新保存本地编辑</button></> : <button disabled={working} onClick={() => { void run(retry); }}>重试保存</button>}</div></div>;
}
function CategoryFilter({ value, onChange }: { value: Workspace['category']; onChange: (value: Workspace['category']) => void }) {
  return <div className="category-filter" role="group" aria-label="候选类别筛选">{categories.map(([key, label]) => <button key={key} aria-pressed={value === key} onClick={() => onChange(key)}>{label}</button>)}</div>;
}
function MapPreferences({ workspace, onChange }: { workspace: Workspace; onChange: (value: Workspace) => void }) {
  const [longitude, setLongitude] = useState(workspace.map_center?.[0].toString() ?? '');
  const [latitude, setLatitude] = useState(workspace.map_center?.[1].toString() ?? '');
  const [zoom, setZoom] = useState(workspace.map_zoom.toString());
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setLongitude(workspace.map_center?.[0].toString() ?? ''); setLatitude(workspace.map_center?.[1].toString() ?? ''); setZoom(workspace.map_zoom.toString()); }, [workspace.map_center, workspace.map_zoom]);
  const submit = (event: FormEvent) => {
    event.preventDefault(); setError(null);
    if (Boolean(longitude) !== Boolean(latitude)) { setError('请同时填写经纬度，或同时留空。'); return; }
    onChange({ ...workspace, map_center: longitude ? [Number(longitude), Number(latitude)] : null, map_zoom: Number(zoom) });
  };
  return <details className="map-preferences"><summary>预设地图视野</summary><p className="muted">视野保存在当前行程，待地图接入后使用。</p><form onSubmit={submit}><label>经度<input type="number" min="-180" max="180" step="any" value={longitude} onChange={event => setLongitude(event.target.value)} /></label><label>纬度<input type="number" min="-90" max="90" step="any" value={latitude} onChange={event => setLatitude(event.target.value)} /></label><label>缩放<input required type="number" min="3" max="20" step="0.5" value={zoom} onChange={event => setZoom(event.target.value)} /></label><button type="submit">保存视野</button></form>{error && <p className="inline-error" role="alert">{error}</p>}</details>;
}
function Chat({ session, archived, onTrip }: { session: TripSession; archived: boolean; onTrip: (trip: Trip) => void }) {
  const [content, setContent] = useState(session.composer);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [messages, setMessages] = useState(session.trip.messages);
  const send = async (event: FormEvent) => {
    event.preventDefault();
    if (!content.trim() || sending) return;
    setSending(true); setError(null);
    session.messageOperation ??= { id: crypto.randomUUID(), content: content.trim() };
    try {
      const saved = await api.message(session.trip.id, session.messageOperation.id, session.messageOperation.content);
      const next = session.trip.messages.some(item => item.id === saved.id) ? session.trip.messages : [...session.trip.messages, saved];
      session.trip = { ...session.trip, messages: next };
      setMessages(next); onTrip(session.trip);
      session.composer = ''; session.messageOperation = null; setContent('');
    } catch (reason) { setError(message(reason)); }
    finally { setSending(false); }
  };
  return <section className="panel chat" aria-labelledby="chat-title"><div className="panel-heading"><div><span className="section-number">02</span><h2 id="chat-title">把想法记下来</h2></div><span className="badge">解析待接入</span></div><div className="chat-history" aria-label="已保存的对话">{messages.length ? messages.map(item => <article className="chat-message" key={item.id}><div><span>{item.role === 'user' ? '你' : '系统'}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</time><span className="message-state">待处理</span></div><p>{item.content}</p></article>) : <p className="chat-empty">想避开人潮，还是只想找个地方发呆？<br />先记下来，后续规划再慢慢展开。</p>}</div><form className="composer" onSubmit={send}><label className="sr-only" htmlFor="message-content">行程想法</label><textarea id="message-content" rows={2} value={content} disabled={archived || sending || Boolean(session.messageOperation)} maxLength={4000} placeholder="记下这次旅行的想法……" onChange={event => { session.composer = event.target.value; setContent(event.target.value); }} /><div><span className="muted">未发送草稿在本次会话中保留，切换行程不会丢失。保存后可重开恢复。</span><button className="primary" type="submit" disabled={archived || sending || !content.trim()}>{sending ? '正在保存…' : error ? '重试保存' : '保存想法'}<span aria-hidden="true">↗</span></button></div></form>{error && <div className="message-error" role="alert"><p>{error} 草稿仍保留。</p><button className="text-button" onClick={() => { session.messageOperation = null; setError(null); }}>继续编辑消息</button></div>}</section>;
}
function Empty({ icon, title, description }: { icon: string; title: string; description: string }) { return <div className="empty-state"><span className="empty-symbol" aria-hidden="true">{icon}</span><h3>{title}</h3><p>{description}</p></div>; }
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); const element = dialog.current; return () => { element?.close(); }; }, []);
  return <dialog ref={dialog} className="modal" aria-labelledby="modal-title" onCancel={event => { event.preventDefault(); onClose(); }}><div className="modal-title"><h2 id="modal-title">{title}</h2><button className="icon-button" aria-label="关闭对话框" onClick={onClose}>×</button></div>{children}</dialog>;
}
