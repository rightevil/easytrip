import { api } from './api';
import type { Message, Run, Trip, TripRequest, Workspace } from './api';
import { Autosave } from './autosave';
import { defaultDiscoveryBudget } from './discovery-types';
import type { Candidate, DiscoveryBudget } from './discovery-types';
import type { MapAction, OriginConfirmation, PlacesSnapshot } from './places-types';

interface PlacesState { snapshot: PlacesSnapshot | null; busy: boolean; reading: boolean; error: string | null; focus: { candidate_id: string; sequence: number } | null }

interface ChatState {
  messages: Message[];
  content: string;
  sending: boolean;
  retryPending: boolean;
  error: string | null;
  discoveryRequested: boolean;
}
interface RunState {
  runs: Run[];
  busy: boolean;
  operation: { id: string; version: number; kind: 'prepare_workspace' | 'discover'; budget?: DiscoveryBudget; parentRunId?: string } | null;
  error: string | null;
  discoveryBudget: DiscoveryBudget;
}

export class TripSession {
  trip: Trip;
  request: Autosave<{ name: string; request: TripRequest }>;
  workspace: Autosave<Workspace>;
  private chat: ChatState;
  private chatListeners = new Set<() => void>();
  private messageOperation: { id: string; content: string; discovery?: { expected_version: number; budget: DiscoveryBudget } } | null = null;
  private directionOperations = new Map<string, { id: string; version: number; runId: string }>();
  private tasks: RunState;
  private taskListeners = new Set<() => void>();
  private disposed = false;
  private places: PlacesState = { snapshot: null, busy: false, reading: false, error: null, focus: null };
  private placesListeners = new Set<() => void>();
  private placesSequence = 0;
  constructor(trip: Trip, private onSaved: (trip: Trip) => void) {
    this.trip = trip;
    this.chat = { messages: trip.messages, content: '', sending: false, retryPending: false, error: null, discoveryRequested: false };
    this.tasks = { runs: trip.runs, busy: false, operation: null, error: null, discoveryBudget: { ...defaultDiscoveryBudget } };
    this.request = new Autosave({ name: trip.name, request: trip.request }, trip.request_version, async (draft, version) => {
      const saved = await api.update(trip.id, version, draft);
      this.trip = { ...this.trip, name: saved.name, request: saved.request, request_version: saved.request_version, updated_at: saved.updated_at };
      const workspaceState = this.workspace.getSnapshot();
      if (!this.workspace.dirty && saved.workspace_version > workspaceState.version) {
        this.workspace.resolve(saved.workspace, saved.workspace_version, false);
        this.trip = { ...this.trip, workspace: saved.workspace, workspace_version: saved.workspace_version };
      }
      onSaved(this.trip);
      return { value: { name: saved.name, request: saved.request }, version: saved.request_version };
    });
    this.workspace = new Autosave(trip.workspace, trip.workspace_version, async (workspace, version) => {
      const saved = await api.workspace(trip.id, version, workspace);
      this.trip = { ...this.trip, workspace: saved.workspace, workspace_version: saved.workspace_version, updated_at: saved.updated_at };
      onSaved(this.trip);
      return { value: saved.workspace, version: saved.workspace_version };
    });
  }
  get dirty() { return this.request.dirty || this.workspace.dirty; }
  getPlacesSnapshot = () => this.places;
  subscribePlaces = (listener: () => void) => { this.placesListeners.add(listener); return () => { this.placesListeners.delete(listener); }; };
  private updatePlaces(change: Partial<PlacesState>) { this.places = { ...this.places, ...change }; this.placesListeners.forEach(listener => listener()); }
  private async acceptPlaces(snapshot: PlacesSnapshot) {
    if (snapshot.trip.id !== this.trip.id || snapshot.candidates.some(item => item.trip_id !== this.trip.id)) throw new Error('地点归属不一致，已阻止应用。');
    const sequence = this.placesSequence;
    if (this.workspace.getSnapshot().status === 'saving') {
      try { await this.workspace.flush(); } catch { /* The authoritative snapshot can rebase pending viewport edits. */ }
    }
    if (sequence !== this.placesSequence || this.disposed) return;
    const saved = snapshot.trip;
    const local = this.workspace.getSnapshot();
    if (saved.workspace_version >= local.version) {
      const dirty = this.workspace.dirty;
      this.workspace.resolve(saved.workspace, saved.workspace_version, false);
      if (dirty) this.workspace.update({ ...saved.workspace, active_tab: local.value.active_tab, category: local.value.category, map_center: local.value.map_center, map_zoom: local.value.map_zoom, map_city: local.value.map_city });
      this.trip = { ...this.trip, workspace: saved.workspace, workspace_version: saved.workspace_version, updated_at: saved.updated_at };
    }
    this.updatePlaces({ snapshot });
    if (!this.disposed) this.onSaved(this.trip);
  }
  async readPlaces(runId?: string) {
    if (this.places.busy) return;
    const sequence = ++this.placesSequence;
    this.updatePlaces({ reading: true, error: null });
    try {
      const snapshot = await api.places(this.trip.id, runId);
      if (sequence === this.placesSequence && !this.disposed) await this.acceptPlaces(snapshot);
    } catch (error) { if (sequence === this.placesSequence) this.updatePlaces({ error: error instanceof Error ? error.message : '地点读取失败。' }); }
    finally { if (sequence === this.placesSequence) this.updatePlaces({ reading: false }); }
  }
  private async changePlaces(send: (version: number, workspaceVersion: number) => Promise<PlacesSnapshot>) {
    if (this.places.busy) return false;
    this.placesSequence++;
    this.updatePlaces({ busy: true, error: null, reading: false });
    try {
      await this.flush();
      await this.acceptPlaces(await send(this.request.getSnapshot().version, this.workspace.getSnapshot().version));
      return true;
    } catch (error) { this.updatePlaces({ error: error instanceof Error ? error.message : '地点操作未完成，请重新读取核对。' }); return false; }
    finally { this.updatePlaces({ busy: false }); }
  }
  matchPlaces(runId: string) { return this.changePlaces((version, workspaceVersion) => api.matchPlaces(this.trip.id, version, workspaceVersion, runId)); }
  confirmPlace(candidateId: string, poiId: string) { return this.changePlaces((version, workspaceVersion) => api.confirmPlace(this.trip.id, candidateId, version, workspaceVersion, poiId)); }
  async mapAction(action: MapAction, candidateId?: string) {
    const saved = await this.changePlaces((version, workspaceVersion) => api.mapAction(this.trip.id, version, workspaceVersion, action, { ...(candidateId ? { candidate_id: candidateId } : {}), ...(action === 'refresh' ? { category: this.workspace.getSnapshot().value.category, city: this.workspace.getSnapshot().value.map_city ?? null } : {}) }));
    if (saved && candidateId && action === 'add') this.focusPlace(candidateId);
    return saved;
  }
  focusPlace(candidateId: string) { this.updatePlaces({ focus: { candidate_id: candidateId, sequence: (this.places.focus?.sequence ?? 0) + 1 } }); }
  async confirmOrigin(confirmation: OriginConfirmation) {
    const success = await this.changePlaces(async (version, workspaceVersion) => {
      const saved = await api.confirmOrigin(this.trip.id, version, workspaceVersion, confirmation);
      if (saved.id !== this.trip.id) throw new Error('起点归属不一致，已阻止应用。');
      return { ...(this.places.snapshot ?? { run_id: null, request_version: version, candidates: [], matches: [], points: [] }), trip: saved };
    });
    if (success) await this.readPlaces(this.places.snapshot?.run_id ?? undefined);
    return success;
  }
  get composer() { return this.chat.content; }
  getChatSnapshot = () => this.chat;
  subscribeChat = (listener: () => void) => {
    this.chatListeners.add(listener);
    return () => { this.chatListeners.delete(listener); };
  };
  private updateChat(change: Partial<ChatState>) {
    this.chat = { ...this.chat, ...change };
    this.chatListeners.forEach(listener => listener());
  }
  editMessage(content: string) {
    if (!this.chat.sending && !this.chat.retryPending) this.updateChat({ content });
  }
  continueEditingMessage() {
    if (this.chat.sending) return;
    this.messageOperation = null;
    this.updateChat({ retryPending: false, error: null, discoveryRequested: false });
  }
  async sendMessage(discover = false) {
    if (!this.chat.content.trim() || this.chat.sending || discover && this.tasks.busy) return;
    this.updateChat({ sending: true, retryPending: true, error: null, discoveryRequested: this.messageOperation ? Boolean(this.messageOperation.discovery) : discover });
    try {
      if (!this.messageOperation) {
        if (discover) await this.flush();
        this.messageOperation = { id: crypto.randomUUID(), content: this.chat.content.trim(), ...(discover ? { discovery: { expected_version: this.request.getSnapshot().version, budget: structuredClone(this.tasks.discoveryBudget) } } : {}) };
      }
      const saved = await api.message(this.trip.id, this.messageOperation.id, this.messageOperation.content, this.messageOperation.discovery);
      if (saved.trip_id !== this.trip.id) throw new Error('消息归属不一致，已阻止展示。');
      const messages = this.trip.messages.some(item => item.id === saved.id) ? this.trip.messages : [...this.trip.messages, saved];
      this.trip = { ...this.trip, messages };
      const requestedDiscovery = Boolean(this.messageOperation.discovery);
      if (saved.discovery_run) this.acceptRun(saved.discovery_run);
      this.messageOperation = null;
      this.updateChat({ messages, content: '', sending: false, retryPending: false, discoveryRequested: false, error: requestedDiscovery && !saved.discovery_run ? '消息已保存，但服务未返回发现任务，尚未确认启动。' : null });
      if (!this.disposed) this.onSaved(this.trip);
    } catch (error) {
      this.updateChat({ sending: false, error: error instanceof Error ? error.message : '消息尚未保存，请重试。' });
    }
  }
  getRunSnapshot = () => this.tasks;
  subscribeRuns = (listener: () => void) => {
    this.taskListeners.add(listener);
    return () => { this.taskListeners.delete(listener); };
  };
  private updateTasks(change: Partial<RunState>) {
    this.tasks = { ...this.tasks, ...change };
    this.taskListeners.forEach(listener => listener());
  }
  setDiscoveryBudget(discoveryBudget: DiscoveryBudget) {
    this.updateTasks({ discoveryBudget });
  }
  acceptRun(next: Run) {
    if (next.trip_id !== this.trip.id) return;
    const current = this.tasks.runs.find(run => run.id === next.id);
    if (current && (current.generation > next.generation || current.generation === next.generation && current.updated_at > next.updated_at)) return;
    const runs = current ? this.tasks.runs.map(run => run.id === next.id ? next : run) : [next, ...this.tasks.runs];
    this.trip = { ...this.trip, runs };
    this.updateTasks({ runs });
  }
  async startRun() {
    if (this.tasks.busy) return;
    this.updateTasks({ busy: true, error: null });
    try {
      await this.flush();
      if (this.tasks.operation?.kind === 'discover') throw new Error('请先处理发现任务的重试，或放弃该次重试。');
      const operation = this.tasks.operation ?? { id: crypto.randomUUID(), version: this.request.getSnapshot().version, kind: 'prepare_workspace' as const };
      this.updateTasks({ operation });
      this.acceptRun(await api.startRun(this.trip.id, operation.id, operation.version));
      this.updateTasks({ operation: null });
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '任务尚未提交，请重试。' });
    } finally { this.updateTasks({ busy: false }); }
  }
  async startDiscovery(parentRunId?: string) {
    if (this.tasks.busy || this.chat.sending) return;
    this.updateTasks({ busy: true, error: null });
    try {
      await this.flush();
      if (this.tasks.operation?.kind === 'prepare_workspace') throw new Error('请先处理工作区准备的重试，或放弃该次重试。');
      const operation = this.tasks.operation ?? { id: crypto.randomUUID(), version: this.request.getSnapshot().version, kind: 'discover' as const, budget: structuredClone(this.tasks.discoveryBudget), parentRunId };
      this.updateTasks({ operation });
      this.acceptRun(await api.startDiscovery(this.trip.id, operation.id, operation.version, operation.budget!, operation.parentRunId));
      this.updateTasks({ operation: null });
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '发现任务尚未提交，请重试。' });
    } finally { this.updateTasks({ busy: false }); }
  }
  async adoptDirection(candidate: Candidate) {
    if (this.tasks.busy) return;
    this.updateTasks({ busy: true, error: null });
    try {
      await this.flush();
      let operation = this.directionOperations.get(candidate.id);
      if (!operation) {
        operation = { id: crypto.randomUUID(), version: this.request.getSnapshot().version, runId: candidate.run_id };
        this.directionOperations.set(candidate.id, operation);
      }
      const saved = await api.adoptDirection(this.trip.id, candidate.id, operation.runId, operation.id, operation.version);
      if (saved.id !== this.trip.id) throw new Error('行程归属不一致，已阻止应用。');
      if (saved.request_version >= this.request.getSnapshot().version && !this.request.dirty) {
        this.request.resolve({ name: saved.name, request: saved.request }, saved.request_version, false);
        this.trip = { ...this.trip, name: saved.name, request: saved.request, request_version: saved.request_version, status: saved.status, updated_at: saved.updated_at };
      } else this.updateTasks({ error: '方向采用已保存为历史版本，当前的新条件或编辑仍保留。请重新读取核对。' });
      this.directionOperations.delete(candidate.id);
      if (!this.disposed) this.onSaved(this.trip);
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '方向尚未采用，请重试。' });
    } finally { this.updateTasks({ busy: false }); }
  }
  async controlRun(runId: string, action: 'cancel' | 'resume') {
    if (this.tasks.busy) return;
    this.updateTasks({ busy: true, error: null });
    try {
      if (action === 'resume') await this.flush();
      const run = this.tasks.runs.find(item => item.id === runId);
      if (!run) throw new Error('任务记录不存在，请重新打开行程。');
      this.acceptRun(await api.runAction(runId, action, this.trip.id, run.generation));
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '任务操作未完成，请重试。' });
      try { this.acceptRun(await api.getRun(runId, this.trip.id)); } catch { /* Keep the last verified status while disconnected. */ }
    } finally { this.updateTasks({ busy: false }); }
  }
  forgetRunOperation() {
    if (!this.tasks.busy) this.updateTasks({ operation: null, error: null });
  }
  async flush() { await Promise.all([this.request.flush(), this.workspace.flush()]); }
  async retry() { await Promise.all([this.request.retry(), this.workspace.retry()]); }
  accept(trip: Trip) {
    // Messages only append in P1; an older detail response must not erase a confirmed save.
    const messages = [...trip.messages, ...this.chat.messages.filter(saved => !trip.messages.some(item => item.id === saved.id))].sort((a, b) => a.created_at.localeCompare(b.created_at));
    const requestCurrent = trip.request_version >= this.request.getSnapshot().version;
    const workspaceCurrent = trip.workspace_version >= this.workspace.getSnapshot().version;
    this.trip = { ...trip, messages, ...(!requestCurrent ? { name: this.trip.name, request: this.trip.request, request_version: this.trip.request_version } : {}), ...(!workspaceCurrent ? { workspace: this.trip.workspace, workspace_version: this.trip.workspace_version } : {}) };
    this.updateChat({ messages });
    trip.runs.forEach(run => this.acceptRun(run));
    this.trip = { ...this.trip, runs: this.tasks.runs };
    if (requestCurrent) this.request.resolve({ name: trip.name, request: trip.request }, trip.request_version, false);
    if (workspaceCurrent) this.workspace.resolve(trip.workspace, trip.workspace_version, false);
  }
  dispose() { this.disposed = true; this.request.dispose(); this.workspace.dispose(); this.chatListeners.clear(); this.taskListeners.clear(); this.placesListeners.clear(); }
}
