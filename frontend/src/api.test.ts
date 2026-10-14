import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';
import type { Workspace } from './api';

afterEach(() => vi.unstubAllGlobals());
it('omits the UI all-category sentinel and server-owned map confirmation fields at the API boundary', async () => {
  const send = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', send);
  await api.mapAction('one', 1, 2, 'refresh', { category: 'all', city: null });
  expect(JSON.parse(send.mock.calls[0][1].body)).toEqual({ expected_version: 1, expected_workspace_version: 2, action: 'refresh', city: null });
  const workspace: Workspace = { active_tab: 'map', category: 'all', map_center: null, map_zoom: 12, map_visible_ids: [], selected_ids: [], map_initialized: true, confirmed_origin: { name: '起点', location: [120, 30], poi_id: null, source: 'map', request_origin: '用户起点' } };
  await api.workspace('one', 2, workspace);
  const body = JSON.parse(send.mock.calls[1][1].body);
  expect(body.workspace).not.toHaveProperty('map_initialized');
  expect(body.workspace).not.toHaveProperty('confirmed_origin');
  expect(body.workspace.map_visible_ids).toEqual([]);
});
