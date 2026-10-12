import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './api';
import { Autosave } from './autosave';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
afterEach(() => { vi.useRealTimers(); });

describe('Autosave protects user edits', () => {
  it('debounces edits and sends the expected saved version', async () => {
    vi.useFakeTimers();
    const send = vi.fn(async (value: string, version: number) => ({ value, version: version + 1 }));
    const editor = new Autosave<string>('', 4, send);
    editor.update('杭州'); editor.update('杭州一日');
    await vi.advanceTimersByTimeAsync(649);
    expect(send).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(send).toHaveBeenCalledExactlyOnceWith('杭州一日', 4);
    expect(editor.getSnapshot()).toMatchObject({ value: '杭州一日', version: 5, status: 'saved' });
    editor.dispose();
  });

  it('never lets an old response erase edits made while saving', async () => {
    const first = deferred<{ value: string; version: number }>();
    const second = deferred<{ value: string; version: number }>();
    const send = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const editor = new Autosave<string>('初稿', 1, send, 100000);
    editor.update('杭州'); const saving = editor.flush();
    editor.update('杭州 + 西湖');
    first.resolve({ value: '杭州', version: 2 });
    await vi.waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    expect(editor.getSnapshot().value).toBe('杭州 + 西湖');
    expect(send.mock.calls[1]).toEqual(['杭州 + 西湖', 2]);
    second.resolve({ value: '杭州 + 西湖', version: 3 }); await saving;
    expect(editor.getSnapshot()).toMatchObject({ version: 3, status: 'saved' });
    editor.dispose();
  });

  it('serializes concurrent flushes without duplicate writes', async () => {
    const first = deferred<{ value: string; version: number }>();
    const send = vi.fn().mockReturnValueOnce(first.promise).mockImplementation(async (value: string, version: number) => ({ value, version: version + 1 }));
    const editor = new Autosave<string>('初稿', 1, send, 100000);
    editor.update('杭州'); const saving = editor.flush();
    const leaving = editor.flush(); editor.update('南京');
    first.resolve({ value: '杭州', version: 2 }); await Promise.all([saving, leaving]);
    expect(send.mock.calls).toEqual([['杭州', 1], ['南京', 2]]);
    editor.dispose();
  });

  it('keeps failed edits, blocks leaving, and retries with the same version', async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError('磁盘写失败', 503, 'database_unavailable')).mockImplementation(async (value: string, version: number) => ({ value, version: version + 1 }));
    const editor = new Autosave<string>('初稿', 1, send, 100000);
    editor.update('未保存的行程');
    await expect(editor.flush()).rejects.toThrow('磁盘写失败');
    expect(editor.getSnapshot()).toMatchObject({ value: '未保存的行程', version: 1, status: 'error' });
    await expect(editor.flush()).rejects.toThrow('磁盘写失败');
    expect(send).toHaveBeenCalledTimes(1);
    await editor.retry();
    expect(send.mock.calls[1]).toEqual(['未保存的行程', 1]);
    expect(editor.getSnapshot().status).toBe('saved'); editor.dispose();
  });

  it('resolves conflicts only by explicitly loading or reapplying against latest', async () => {
    const send = vi.fn().mockRejectedValueOnce(new ApiError('版本冲突', 409, 'version_conflict', 9)).mockImplementation(async (value: string, version: number) => ({ value, version: version + 1 }));
    const editor = new Autosave<string>('初稿', 1, send, 100000);
    editor.update('本地编辑'); await expect(editor.flush()).rejects.toThrow();
    editor.update('继续本地编辑'); expect(editor.getSnapshot().status).toBe('conflict');
    await expect(editor.retry()).rejects.toThrow('请先处理版本冲突');
    editor.resolve('其他窗口编辑', 9, true); await editor.flush();
    expect(send.mock.calls[1]).toEqual(['继续本地编辑', 9]);
    editor.resolve('最新版本', 20, false);
    expect(editor.getSnapshot()).toMatchObject({ value: '最新版本', version: 20, status: 'saved' }); editor.dispose();
  });

  it('does not mislabel other 409 failures as a version conflict', async () => {
    const editor = new Autosave<string>('初稿', 1, async () => { throw new ApiError('行程已归档，请先恢复', 409, 'operation_conflict'); }, 100000);
    editor.update('本地编辑'); await expect(editor.flush()).rejects.toThrow('行程已归档');
    expect(editor.getSnapshot()).toMatchObject({ value: '本地编辑', version: 1, status: 'error' });
    editor.dispose();
  });

  it('separates sessions so a late save cannot overwrite another trip', async () => {
    const pending = deferred<{ value: string; version: number }>();
    const first = new Autosave<string>('第一份', 1, () => pending.promise, 100000);
    const second = new Autosave<string>('第二份', 1, async value => ({ value, version: 2 }), 100000);
    first.update('第一份的编辑'); const saving = first.flush();
    second.update('第二份的编辑'); await second.flush();
    pending.resolve({ value: '第一份的编辑', version: 2 }); await saving;
    expect(second.getSnapshot().value).toBe('第二份的编辑');
    first.dispose(); second.dispose();
  });
});
