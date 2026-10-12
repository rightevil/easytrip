import { ApiError } from './api';

export type SaveStatus = 'saved' | 'dirty' | 'saving' | 'error' | 'conflict';
export interface SaveState<T> {
  value: T;
  version: number;
  status: SaveStatus;
  error: string | null;
}
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export class Autosave<T> {
  private saved: T;
  private state: SaveState<T>;
  private listeners = new Set<() => void>();
  private timer: ReturnType<typeof setTimeout> | undefined;
  private pending: Promise<void> | null = null;

  constructor(value: T, version: number, private send: (value: T, version: number) => Promise<{ value: T; version: number }>, private delay = 650) {
    this.saved = structuredClone(value);
    this.state = { value, version, status: 'saved', error: null };
  }
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  get dirty() { return !equal(this.state.value, this.saved); }
  private emit(next: Partial<SaveState<T>>) {
    this.state = { ...this.state, ...next };
    this.listeners.forEach(listener => listener());
  }
  update(value: T) {
    const conflicted = this.state.status === 'conflict';
    this.emit({ value, status: conflicted ? 'conflict' : this.pending ? 'saving' : this.dirtyAfter(value) ? 'dirty' : 'saved', error: conflicted ? this.state.error : null });
    clearTimeout(this.timer);
    if (!conflicted && !this.pending && this.dirty) this.timer = setTimeout(() => { void this.flush().catch(() => {}); }, this.delay);
  }
  private dirtyAfter(value: T) { return !equal(value, this.saved); }
  async flush(): Promise<void> {
    clearTimeout(this.timer);
    if (this.pending) { await this.pending; return this.flush(); }
    if (!this.dirty) return;
    if (this.state.status === 'error' || this.state.status === 'conflict') throw new Error(this.state.error ?? '保存未完成');
    const snapshot = structuredClone(this.state.value);
    const version = this.state.version;
    this.emit({ status: 'saving', error: null });
    this.pending = (async () => {
      try {
        const saved = await this.send(snapshot, version);
        this.saved = structuredClone(saved.value);
        const value = equal(this.state.value, snapshot) ? saved.value : this.state.value;
        this.emit({ value, version: saved.version, status: this.dirtyAfter(value) ? 'dirty' : 'saved', error: null });
      } catch (error) {
        this.emit({ status: error instanceof ApiError && error.status === 409 && error.code === 'version_conflict' ? 'conflict' : 'error', error: error instanceof Error ? error.message : '保存失败' });
        throw error;
      } finally {
        this.pending = null;
      }
    })();
    await this.pending;
    if (this.dirty) await this.flush();
  }
  async retry() {
    if (this.state.status === 'conflict') throw new Error('请先处理版本冲突');
    this.emit({ status: this.dirty ? 'dirty' : 'saved', error: null });
    await this.flush();
  }
  resolve(latest: T, version: number, keepLocal: boolean) {
    if (this.pending) throw new Error('请等待当前保存完成');
    clearTimeout(this.timer);
    this.saved = structuredClone(latest);
    const value = keepLocal ? this.state.value : latest;
    this.emit({ value, version, status: this.dirtyAfter(value) ? 'dirty' : 'saved', error: null });
  }
  dispose() { clearTimeout(this.timer); this.listeners.clear(); }
}
