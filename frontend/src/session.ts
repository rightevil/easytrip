import { api } from './api';
import type { Message, Run, Trip, TripRequest, Workspace } from './api';
import { Autosave } from './autosave';

interface ChatState {
  messages: Message[];
  content: string;
  sending: boolean;
  retryPending: boolean;
  error: string | null;
}
interface RunState {
  runs: Run[];
  busy: boolean;
  operation: { id: string; version: number } | null;
  error: string | null;
}

export class TripSession {
  trip: Trip;
  request: Autosave<{ name: string; request: TripRequest }>;
  workspace: Autosave<Workspace>;
  private chat: ChatState;
  private chatListeners = new Set<() => void>();
  private messageOperation: { id: string; content: string } | null = null;
  private tasks: RunState;
  private taskListeners = new Set<() => void>();
  private disposed = false;
  constructor(trip: Trip, private onSaved: (trip: Trip) => void) {
    this.trip = trip;
    this.chat = { messages: trip.messages, content: '', sending: false, retryPending: false, error: null };
    this.tasks = { runs: trip.runs, busy: false, operation: null, error: null };
    this.request = new Autosave({ name: trip.name, request: trip.request }, trip.request_version, async (draft, version) => {
      const saved = await api.update(trip.id, version, draft);
      this.trip = { ...this.trip, name: saved.name, request: saved.request, request_version: saved.request_version, updated_at: saved.updated_at };
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
    this.updateChat({ retryPending: false, error: null });
  }
  async sendMessage() {
    if (!this.chat.content.trim() || this.chat.sending) return;
    this.messageOperation ??= { id: crypto.randomUUID(), content: this.chat.content.trim() };
    this.updateChat({ sending: true, retryPending: true, error: null });
    try {
      const saved = await api.message(this.trip.id, this.messageOperation.id, this.messageOperation.content);
      const messages = this.trip.messages.some(item => item.id === saved.id) ? this.trip.messages : [...this.trip.messages, saved];
      this.trip = { ...this.trip, messages };
      this.messageOperation = null;
      this.updateChat({ messages, content: '', sending: false, retryPending: false, error: null });
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
      const operation = this.tasks.operation ?? { id: crypto.randomUUID(), version: this.request.getSnapshot().version };
      this.updateTasks({ operation });
      this.acceptRun(await api.startRun(this.trip.id, operation.id, operation.version));
      this.updateTasks({ operation: null });
    } catch (error) {
      this.updateTasks({ error: error instanceof Error ? error.message : '任务尚未提交，请重试。' });
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
    this.trip = { ...trip, messages };
    this.updateChat({ messages });
    trip.runs.forEach(run => this.acceptRun(run));
    this.trip = { ...this.trip, runs: this.tasks.runs };
    this.request.resolve({ name: trip.name, request: trip.request }, trip.request_version, false);
    this.workspace.resolve(trip.workspace, trip.workspace_version, false);
  }
  dispose() { this.disposed = true; this.request.dispose(); this.workspace.dispose(); this.chatListeners.clear(); this.taskListeners.clear(); }
}
