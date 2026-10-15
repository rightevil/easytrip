import { api, ApiError } from './api';
import type { Message, Run, Trip, TripRequest, Workspace } from './api';
import { Autosave } from './autosave';
import { defaultDiscoveryBudget } from './discovery-types';
import type { Candidate, DiscoveryBudget } from './discovery-types';
import type { ManualPlaceConfirmation, ManualPlaceSearch, MapAction, OriginConfirmation, PlacesSnapshot } from './places-types';
import { defaultPlanningBudget } from './planning-types';
import type { Plan, PlanningBudget, PlanningReplacement } from './planning-types';
import { validCoordinate } from './amap';

interface PlacesState { snapshot: PlacesSnapshot | null; busy: boolean; reading: boolean; error: string | null; focus: { candidate_id: string; sequence: number } | null }
interface ManualPlaceState { query: string; city: string; category: Candidate['category']; search: ManualPlaceSearch | null; poiId: string | null; searching: boolean; operation: ManualPlaceConfirmation | null; error: string | null; savedName: string | null }

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
  operation: { id: string; version: number; kind: 'prepare_workspace' | 'discover' | 'plan'; budget?: DiscoveryBudget; planningBudget?: PlanningBudget; parentRunId?: string; parentPlanId?: string } | null;
  planAdoption: { planId: string; id: string; version: number; inputHash: string } | null;
  planningReplacement: PlanningReplacement | null;
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
  private placesRunId: string | undefined;
  private manualPlace: ManualPlaceState;
  private manualPlaceListeners = new Set<() => void>();
  private manualSearchSequence = 0;
  constructor(trip: Trip, private onSaved: (trip: Trip) => void) {
    this.trip = trip;
    this.manualPlace = { query: '', city: trip.request.destination ?? '', category: 'play', search: null, poiId: null, searching: false, operation: null, error: null, savedName: null };
    this.chat = { messages: trip.messages, content: '', sending: false, retryPending: false, error: null, discoveryRequested: false };
    this.tasks = { runs: trip.runs, busy: false, operation: null, planAdoption: null, planningReplacement: null, error: null, discoveryBudget: { ...defaultDiscoveryBudget } };
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
  private async acceptPlaces(snapshot: PlacesSnapshot, keepDisplayedRun = false) {
    if (snapshot.trip.id !== this.trip.id || [...snapshot.candidates, ...(snapshot.manual_candidates ?? [])].some(item => item.trip_id !== this.trip.id)) throw new Error('地点归属不一致，已阻止应用。');
    if (keepDisplayedRun && this.placesRunId && snapshot.run_id !== this.placesRunId) {
      const previous = this.places.snapshot?.run_id === this.placesRunId ? this.places.snapshot : null;
      snapshot = { ...snapshot, run_id: this.placesRunId, request_version: previous?.request_version ?? this.request.getSnapshot().version, candidates: previous?.candidates ?? [], matches: previous?.matches ?? [] };
    }
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
    runId ??= this.placesRunId;
    this.placesRunId = runId;
    const sequence = ++this.placesSequence;
    if (this.places.busy) return;
    this.updatePlaces({ reading: true, error: null });
    try {
      const snapshot = await api.places(this.trip.id, runId);
      if (sequence === this.placesSequence && !this.disposed) {
        if (runId && snapshot.run_id !== runId) throw new Error('地点来源任务不一致，已阻止应用。');
        await this.acceptPlaces(snapshot);
      }
    } catch (error) { if (sequence === this.placesSequence) this.updatePlaces({ error: error instanceof Error ? error.message : '地点读取失败。' }); }
    finally { if (sequence === this.placesSequence) this.updatePlaces({ reading: false }); }
  }
  private async changePlaces(send: (version: number, workspaceVersion: number) => Promise<PlacesSnapshot>) {
    if (this.places.busy || this.disposed) return false;
    this.placesSequence++;
    this.updatePlaces({ busy: true, error: null, reading: false });
    let displayedRun = this.placesRunId;
    try {
      await this.flush();
      if (this.disposed) return false;
      const snapshot = await send(this.request.getSnapshot().version, this.workspace.getSnapshot().version);
      if (this.disposed) return false;
      await this.acceptPlaces(snapshot, true);
      displayedRun = this.placesRunId;
      if (displayedRun && snapshot.run_id !== displayedRun) {
        try {
          const sequence = this.placesSequence;
          const refreshed = await api.places(this.trip.id, displayedRun);
          if (sequence === this.placesSequence && !this.disposed) {
            if (refreshed.run_id !== displayedRun) throw new Error('地点来源任务不一致，已阻止应用。');
            await this.acceptPlaces(refreshed);
          }
        } catch (error) { if (!this.disposed && displayedRun === this.placesRunId) this.updatePlaces({ error: `地点操作已保存，当前来源状态读取失败：${error instanceof Error ? error.message : '请重新读取核对。'}` }); }
      }
      return true;
    } catch (error) { this.updatePlaces({ error: error instanceof Error ? error.message : '地点操作未完成，请重新读取核对。' }); return false; }
    finally {
      this.updatePlaces({ busy: false });
      if (!this.disposed && displayedRun !== this.placesRunId) void this.readPlaces(this.placesRunId);
    }
  }
  matchPlaces(runId: string) { return this.changePlaces((version, workspaceVersion) => api.matchPlaces(this.trip.id, version, workspaceVersion, runId)); }
  confirmPlace(candidateId: string, poiId: string) { return this.changePlaces((version, workspaceVersion) => api.confirmPlace(this.trip.id, candidateId, version, workspaceVersion, poiId)); }
  getManualPlaceSnapshot = () => this.manualPlace;
  subscribeManualPlace = (listener: () => void) => { this.manualPlaceListeners.add(listener); return () => { this.manualPlaceListeners.delete(listener); }; };
  private updateManualPlace(change: Partial<ManualPlaceState>) { this.manualPlace = { ...this.manualPlace, ...change }; this.manualPlaceListeners.forEach(listener => listener()); }
  editManualPlace(change: Partial<Pick<ManualPlaceState, 'query' | 'city' | 'category' | 'poiId'>>) {
    if (this.manualPlace.operation || this.places.busy || this.disposed) return;
    if (change.query !== undefined || change.city !== undefined) {
      this.manualSearchSequence++;
      this.updateManualPlace({ ...change, search: null, poiId: null, searching: false, error: null, savedName: null });
    } else this.updateManualPlace({ ...change, error: null, savedName: null });
  }
  async searchManualPlace() {
    const { query: rawQuery, city: rawCity } = this.manualPlace;
    const query = rawQuery.trim(), city = rawCity.trim();
    if (!query || query.length > 80 || !city || city.length > 100 || this.manualPlace.searching || this.manualPlace.operation || this.places.busy || this.disposed) return;
    const sequence = ++this.manualSearchSequence;
    this.updateManualPlace({ searching: true, search: null, poiId: null, error: null, savedName: null });
    try {
      await this.flush();
      if (sequence !== this.manualSearchSequence || this.disposed) return;
      const result = await api.searchManualPlace(this.trip.id, query, city, this.request.getSnapshot().version);
      if (sequence !== this.manualSearchSequence || this.disposed) return;
      if (result.request_version !== this.request.getSnapshot().version || result.query !== query || result.city !== city) throw new Error('地点搜索条件已改变，请重新搜索。');
      this.updateManualPlace({ search: result });
    } catch (error) { if (sequence === this.manualSearchSequence && !this.disposed) this.updateManualPlace({ error: error instanceof Error ? error.message : '地点搜索失败，输入已保留。' }); }
    finally { if (sequence === this.manualSearchSequence && !this.disposed) this.updateManualPlace({ searching: false }); }
  }
  async confirmManualPlace() {
    if (this.disposed || this.places.busy) return false;
    const success = await this.changePlaces(async (version, workspaceVersion) => {
      let operation = this.manualPlace.operation;
      if (!operation) {
        const { search, poiId, category } = this.manualPlace;
        if (!search || search.request_version !== version || !search.items.some(item => item.id === poiId)) throw new Error('请选择当前搜索中的具体地点，条件改变后需要重新搜索。');
        operation = { operation_id: crypto.randomUUID(), expected_version: version, expected_workspace_version: workspaceVersion, search_id: search.search_id, poi_id: poiId!, category };
        this.updateManualPlace({ operation, error: null });
      }
      const snapshot = await api.confirmManualPlace(this.trip.id, operation);
      if (!(snapshot.manual_candidates ?? []).some(item => item.id === snapshot.candidate_id && item.poi.id === operation.poi_id && item.category === operation.category)) throw new Error('手动候选保存结果未确认，请重试同一操作。');
      return snapshot;
    });
    if (this.disposed) return false;
    if (success) {
      const selected = this.manualPlace.search?.items.find(item => item.id === this.manualPlace.poiId);
      this.updateManualPlace({ search: null, poiId: null, operation: null, error: null, savedName: selected?.name ?? this.manualPlace.query.trim() });
    } else this.updateManualPlace({ error: this.places.error ?? '候选尚未确认保存，输入已保留。' });
    return success;
  }
  continueEditingManualPlace() {
    if (!this.places.busy && !this.disposed) this.updateManualPlace({ operation: null, error: null });
  }
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
      if (this.tasks.operation && this.tasks.operation.kind !== 'prepare_workspace') throw new Error('请先处理其他任务的重试，或明确放弃该次重试。');
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
      if (this.tasks.operation && this.tasks.operation.kind !== 'discover') throw new Error('请先处理其他任务的重试，或明确放弃该次重试。');
      const operation = this.tasks.operation ?? { id: crypto.randomUUID(), version: this.request.getSnapshot().version, kind: 'discover' as const, budget: structuredClone(this.tasks.discoveryBudget), parentRunId };
      this.updateTasks({ operation });
      this.acceptRun(await api.startDiscovery(this.trip.id, operation.id, operation.version, operation.budget!, operation.parentRunId));
      this.updateTasks({ operation: null });
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '发现任务尚未提交，请重试。' });
    } finally { this.updateTasks({ busy: false }); }
  }
  async startPlanning(parentPlanId?: string) {
    if (this.tasks.busy || this.chat.sending) return;
    this.updateTasks({ busy: true, error: null });
    try {
      if (this.tasks.planningReplacement) throw new Error('请先处理项目替换的重试，或明确放弃该次重试。');
      await this.flush();
      if (this.tasks.operation && this.tasks.operation.kind !== 'plan') throw new Error('请先处理其他任务的重试，或明确放弃该次重试。');
      const operation = this.tasks.operation ?? { id: crypto.randomUUID(), version: this.request.getSnapshot().version, kind: 'plan' as const, planningBudget: { ...defaultPlanningBudget }, parentPlanId };
      this.updateTasks({ operation });
      const run = await api.startPlanning(this.trip.id, operation.id, operation.version, operation.planningBudget!, operation.parentPlanId);
      if (run.trip_id !== this.trip.id || run.kind !== 'plan') throw new Error('规划任务归属不一致，已阻止应用。');
      this.acceptRun(run);
      this.updateTasks({ operation: null });
      return true;
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '规划任务尚未提交，请重试。' });
      return false;
    } finally { this.updateTasks({ busy: false }); }
  }
  async adoptPlan(plan: Plan) {
    if (this.tasks.busy) return false;
    this.updateTasks({ busy: true, error: null });
    try {
      if (this.tasks.planningReplacement) throw new Error('请先处理项目替换的重试，或明确放弃该次重试。');
      await this.flush();
      if (plan.trip_id !== this.trip.id) throw new Error('方案不属于当前行程。');
      if (this.tasks.planAdoption && this.tasks.planAdoption.planId !== plan.id) throw new Error('请先处理上一份方案的采用重试。');
      let operation = this.tasks.planAdoption;
      if (!operation) {
        const input = await api.planningInput(this.trip.id);
        if (input.trip_id && input.trip_id !== this.trip.id || input.request_version !== this.request.getSnapshot().version || input.input_hash !== plan.input_hash || this.request.dirty || this.workspace.dirty) throw new Error('规划条件或所选地点已变化，请重新生成方案。');
        operation = { planId: plan.id, id: crypto.randomUUID(), version: input.request_version, inputHash: input.input_hash };
        this.updateTasks({ planAdoption: operation });
      }
      const { trip: saved, plan: adopted, operation_applied } = await api.adoptPlan(this.trip.id, plan.id, operation.id, operation.version, operation.inputHash);
      if (saved.id !== this.trip.id || adopted.trip_id !== this.trip.id || adopted.id !== plan.id || saved.active_plan_id !== plan.id && operation_applied !== true) throw new Error('采用结果未确认，已阻止应用。');
      const request = this.request.getSnapshot();
      const workspace = this.workspace.getSnapshot();
      const requestCurrent = saved.request_version >= request.version && !this.request.dirty && request.status !== 'saving';
      const workspaceCurrent = saved.workspace_version >= workspace.version && !this.workspace.dirty && workspace.status !== 'saving';
      const planCurrent = saved.updated_at >= this.trip.updated_at;
      this.trip = { ...this.trip, active_plan_id: planCurrent ? saved.active_plan_id : this.trip.active_plan_id, updated_at: planCurrent ? saved.updated_at : this.trip.updated_at,
        ...(requestCurrent ? { name: saved.name, request: saved.request, request_version: saved.request_version } : {}),
        ...(workspaceCurrent ? { workspace: saved.workspace, workspace_version: saved.workspace_version } : {}) };
      if (requestCurrent) this.request.resolve({ name: saved.name, request: saved.request }, saved.request_version, false);
      if (workspaceCurrent) this.workspace.resolve(saved.workspace, saved.workspace_version, false);
      this.updateTasks({ planAdoption: null });
      if (!this.disposed) this.onSaved(this.trip);
      return true;
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '方案采用未完成，请重试。' });
      return false;
    } finally { this.updateTasks({ busy: false }); }
  }
  forgetPlanAdoption() {
    if (!this.tasks.busy) this.updateTasks({ planAdoption: null, error: null });
  }
  async replacePlanningCandidate(previousId: string, replacementId: string) {
    if (this.tasks.busy || this.places.busy) return false;
    const retrying = Boolean(this.tasks.planningReplacement);
    this.updateTasks({ busy: true, error: null });
    try {
      let operation = this.tasks.planningReplacement;
      if (operation && (operation.candidate_id !== previousId || operation.replacement_id !== replacementId)) throw new Error('请先处理原项目替换的重试。');
      if (!operation) {
        await this.flush();
        const constraint = this.request.getSnapshot().value.request.planning?.activity_constraints.find(item => item.candidate_id === previousId);
        if (constraint?.state === 'completed' || constraint?.locked || constraint?.required || constraint?.fixed_start_at) throw new Error('已完成、锁定、必去或固定预约项目不能在此替换。');
        const [input, replacements] = await Promise.all([api.planningInput(this.trip.id), api.planningReplacements(this.trip.id)]);
        const candidate = replacements.candidates.find(item => item.candidate_id === replacementId);
        if (input.trip_id && input.trip_id !== this.trip.id || replacements.trip_id !== this.trip.id || !candidate || !validCoordinate(candidate.poi.location)) throw new Error('替换项目没有本行程已核对的真实地点。');
        if (input.request_version !== this.request.getSnapshot().version || replacements.request_version !== input.request_version || replacements.workspace_version !== this.workspace.getSnapshot().version || this.request.dirty || this.workspace.dirty) throw new Error('条件或工作区已变化，请保存后重新读取。');
        const selected = this.workspace.getSnapshot().value.selected_ids;
        if (!selected.includes(previousId) || selected.includes(replacementId)) throw new Error('所选项目已变化，请重新读取。');
        operation = { operation_id: crypto.randomUUID(), expected_version: input.request_version, expected_workspace_version: replacements.workspace_version, input_hash: input.input_hash, candidate_id: previousId, replacement_id: replacementId };
        this.updateTasks({ planningReplacement: operation });
      }
      const { trip: saved, operation_applied } = await api.replacePlanningCandidate(this.trip.id, operation);
      if (saved.id !== this.trip.id || operation_applied !== true || saved.workspace_version <= operation.expected_workspace_version || !retrying && (!saved.workspace.selected_ids.includes(replacementId) || saved.workspace.selected_ids.includes(previousId))) throw new Error('替换结果未确认，请沿用同一操作重试并核对。');
      if (this.disposed) return false;
      if (this.workspace.getSnapshot().status === 'saving') {
        try { await this.workspace.flush(); } catch { /* Rebase only viewport edits after the confirmed selection change. */ }
      }
      const local = this.workspace.getSnapshot();
      if (saved.workspace_version > local.version || saved.workspace_version === local.version && saved.updated_at >= this.trip.updated_at) {
        const dirty = this.workspace.dirty;
        this.workspace.resolve(saved.workspace, saved.workspace_version, false);
        if (dirty) this.workspace.update({ ...saved.workspace, active_tab: local.value.active_tab, category: local.value.category, map_center: local.value.map_center, map_zoom: local.value.map_zoom, map_city: local.value.map_city });
        this.trip = { ...this.trip, workspace: saved.workspace, workspace_version: saved.workspace_version };
      }
      const request = this.request.getSnapshot();
      if (saved.request_version >= request.version && !this.request.dirty && request.status !== 'saving') {
        this.request.resolve({ name: saved.name, request: saved.request }, saved.request_version, false);
        this.trip = { ...this.trip, name: saved.name, request: saved.request, request_version: saved.request_version };
      }
      if (saved.updated_at >= this.trip.updated_at) this.trip = { ...this.trip, active_plan_id: saved.active_plan_id, updated_at: saved.updated_at };
      this.updateTasks({ planningReplacement: null });
      this.onSaved(this.trip);
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status >= 400 && error.status < 500) this.updateTasks({ planningReplacement: null });
      this.updateTasks({ error: error instanceof Error ? error.message : '替换结果未知，请沿用同一操作重试。' });
      return false;
    } finally { this.updateTasks({ busy: false }); }
  }
  forgetPlanningReplacement() {
    if (!this.tasks.busy) this.updateTasks({ planningReplacement: null, error: null });
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
    const planCurrent = trip.updated_at >= this.trip.updated_at;
    this.trip = { ...trip, messages, ...(!requestCurrent ? { name: this.trip.name, request: this.trip.request, request_version: this.trip.request_version } : {}), ...(!workspaceCurrent ? { workspace: this.trip.workspace, workspace_version: this.trip.workspace_version } : {}), ...(!planCurrent ? { active_plan_id: this.trip.active_plan_id, updated_at: this.trip.updated_at } : {}) };
    this.updateChat({ messages });
    trip.runs.forEach(run => this.acceptRun(run));
    this.trip = { ...this.trip, runs: this.tasks.runs };
    if (requestCurrent) this.request.resolve({ name: trip.name, request: trip.request }, trip.request_version, false);
    if (workspaceCurrent) this.workspace.resolve(trip.workspace, trip.workspace_version, false);
  }
  dispose() { this.disposed = true; this.manualSearchSequence++; this.request.dispose(); this.workspace.dispose(); this.chatListeners.clear(); this.taskListeners.clear(); this.placesListeners.clear(); this.manualPlaceListeners.clear(); }
}
