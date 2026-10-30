import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from './App';
import { Settings } from './Settings';
import { api } from './api';
import type { AppSettings, SettingName } from './api';

const names: SettingName[] = ['xhs_mcp', 'rail_mcp', 'amap_web_key', 'amap_js_key', 'amap_js_secure_key', 'amap_mcp_url', 'model_base_url', 'model_api_key', 'model_id'];
const settings: AppSettings = {
  fields: Object.fromEntries(names.map(name => [name, { configured: true, value: name === 'model_id' ? 'vision' : '' }])) as AppSettings['fields'],
  revision: 'initial', restart_required: false, config_path: 'C:\\AppData\\EasyTrip\\.env', setup_required: false,
  checks: { model: { state: 'configured_not_probed', missing: [], invalid: [] } },
};
beforeEach(() => {
  vi.spyOn(api, 'settings').mockResolvedValue(structuredClone(settings));
  vi.spyOn(api, 'saveSettings').mockResolvedValue({ ...structuredClone(settings), revision: 'saved', restart_required: true });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.clear(); window.history.replaceState(null, '', '/'); });

it('edits all env settings, preserves omitted credentials, explicitly clears, and prompts restart', async () => {
  render(<Settings onClose={() => {}} />);
  await screen.findByDisplayValue('vision');
  expect(document.querySelectorAll('.settings-input input')).toHaveLength(9);
  const key = screen.getByLabelText(/^高德 Web 服务 Key/);
  expect((key as HTMLInputElement).type).toBe('password');
  expect((key as HTMLInputElement).value).toBe('');
  fireEvent.change(key, { target: { value: 'new-private-key' } });
  fireEvent.click(screen.getByRole('button', { name: '清除12306 MCP 地址' }));
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await screen.findByText(/设置已保存。请关闭并重新打开/);
  expect(api.saveSettings).toHaveBeenCalledExactlyOnceWith('initial', { amap_web_key: 'new-private-key', rail_mcp: '' });
  expect((key as HTMLInputElement).value).toBe('');
  expect(localStorage.length).toBe(0);
});

it('keeps typed changes on save failure and allows cancelling an explicit clear', async () => {
  vi.mocked(api.saveSettings).mockRejectedValue(new Error('设置未保存'));
  render(<Settings onClose={() => {}} />);
  await screen.findByDisplayValue('vision');
  fireEvent.click(screen.getByRole('button', { name: '清除小红书 MCP 地址' }));
  fireEvent.click(screen.getByRole('button', { name: '取消清除小红书 MCP 地址' }));
  const key = screen.getByLabelText(/^模型 API Key/);
  fireEvent.change(key, { target: { value: 'keep-draft' } });
  fireEvent.click(screen.getByRole('button', { name: '保存设置' }));
  await screen.findByRole('alert');
  expect((key as HTMLInputElement).value).toBe('keep-draft');
  expect(api.saveSettings).toHaveBeenCalledWith('initial', { model_api_key: 'keep-draft' });
});

it('opens from the empty workspace and retains settings drafts while switching back to trips', async () => {
  vi.spyOn(api, 'list').mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
  render(<App />);
  await waitFor(() => expect(screen.getByRole('button', { name: '新建行程' }).hasAttribute('disabled')).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  await screen.findByDisplayValue('vision');
  fireEvent.change(screen.getByLabelText(/^模型名称/), { target: { value: 'draft-model' } });
  fireEvent.click(screen.getByRole('button', { name: '返回行程' }));
  expect(screen.queryByRole('heading', { name: '设置' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '设置' }));
  await screen.findByRole('heading', { name: '设置' });
  expect((screen.getByLabelText(/^模型名称/) as HTMLInputElement).value).toBe('draft-model');
});

it('previews an env file and saves only after the explicit import action without showing credentials', async () => {
  const content = 'AGNES_KEY=private-imported-key\nAMAP_WEB_KEY=';
  const preview = { imported_fields: ['model_api_key' as const], ignored_fields: 0, skipped_empty: 1, checks: settings.checks };
  vi.spyOn(api, 'importSettings').mockResolvedValueOnce(preview).mockResolvedValueOnce({ ...preview, settings: { ...settings, restart_required: true } });
  render(<Settings onClose={() => {}} />);
  await screen.findByDisplayValue('vision');
  fireEvent.change(screen.getByLabelText('选择 .env 文件'), { target: { files: [new File([content], '.env', { type: 'text/plain' })] } });
  await screen.findByRole('button', { name: '确认导入并保存' });
  expect(api.importSettings).toHaveBeenCalledExactlyOnceWith('initial', content);
  expect(document.body.textContent).not.toContain('private-imported-key');
  fireEvent.click(screen.getByRole('button', { name: '确认导入并保存' }));
  await screen.findByText(/配置已导入并保存。关闭/);
  expect(api.importSettings).toHaveBeenLastCalledWith('initial', content, true);
  expect(screen.queryByRole('button', { name: '确认导入并保存' })).toBeNull();
  expect(localStorage.length).toBe(0);
});

it('cancels a preview and rejects oversized files before any upload', async () => {
  vi.spyOn(api, 'importSettings').mockResolvedValue({ imported_fields: ['model_id'], ignored_fields: 0, skipped_empty: 0, checks: settings.checks });
  render(<Settings onClose={() => {}} />);
  await screen.findByDisplayValue('vision');
  const input = screen.getByLabelText('选择 .env 文件');
  fireEvent.change(input, { target: { files: [new File(['MODEL_ID=preview'], '.env')] } });
  fireEvent.click(await screen.findByRole('button', { name: '取消导入' }));
  expect(api.importSettings).toHaveBeenCalledTimes(1);
  fireEvent.change(input, { target: { files: [new File(['x'.repeat(65537)], '.env')] } });
  await screen.findByRole('alert');
  expect(api.importSettings).toHaveBeenCalledTimes(1);
});

it('checks edited values without saving and names the incomplete service', async () => {
  vi.spyOn(api, 'checkSettings').mockResolvedValue({ checks: { model: { state: 'incomplete', missing: ['model_base_url'], invalid: [] } } });
  render(<Settings onClose={() => {}} />);
  await screen.findByDisplayValue('vision');
  fireEvent.change(screen.getByLabelText(/^模型 API Key/), { target: { value: 'draft-secret' } });
  fireEvent.click(screen.getByRole('button', { name: '检查当前配置' }));
  await screen.findByText('配置未齐');
  expect(screen.getByText('待填：模型服务地址')).toBeTruthy();
  expect(api.checkSettings).toHaveBeenCalledWith('initial', { model_api_key: 'draft-secret' });
  expect(api.saveSettings).not.toHaveBeenCalled();
  expect((screen.getByLabelText('选择 .env 文件') as HTMLInputElement).disabled).toBe(true);
});

it('opens first-use guidance from the desktop bootstrap and permits a persistent skip', async () => {
  window.history.replaceState(null, '', '/?setup=1');
  vi.spyOn(api, 'list').mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
  vi.spyOn(api, 'completeSetup').mockResolvedValue({ setup_required: false });
  render(<App />);
  await screen.findByRole('heading', { name: '欢迎使用 EasyTrip' });
  await screen.findByDisplayValue('vision');
  fireEvent.click(screen.getByRole('button', { name: '稍后设置，先建行程' }));
  await waitFor(() => expect(screen.queryByRole('heading', { name: '欢迎使用 EasyTrip' })).toBeNull());
  expect(api.completeSetup).toHaveBeenCalledTimes(1);
  expect(window.location.search).toBe('');
  expect(api.saveSettings).not.toHaveBeenCalled();
});
