import { afterEach, expect, it, vi } from 'vitest';
import { api } from './api';
import type { Workspace } from './api';
import { defaultPlanningBudget } from './planning-types';

afterEach(() => vi.unstubAllGlobals());
it('queries the server railway MCP with only saved conditions and concrete station IDs', async () => {
  const send = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', send);
  const query = { expected_version: 7, date: '2026-10-15', departure_station_id: 'departure', arrival_station_id: 'arrival' };
  await api.searchRail('trip / one', query);
  expect(send.mock.calls[0][0]).toBe('/api/trips/trip%20%2F%20one/rail/search');
  expect(send.mock.calls[0][1].method).toBe('POST');
  expect(JSON.parse(send.mock.calls[0][1].body)).toEqual(query);
});
it('searches manual places with encoded city and query and confirms only a chosen POI and operation', async () => {
  const send = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', send);
  await api.searchManualPlace('trip / one', '店 A&B', '杭州市', 7);
  const operation = { operation_id: 'same-operation', expected_version: 7, expected_workspace_version: 9, search_id: 'search', poi_id: 'poi-one', category: 'play' as const };
  await api.confirmManualPlace('trip / one', operation);
  expect(send.mock.calls[0][0]).toBe(`/api/trips/trip%20%2F%20one/places/manual/search?query=${encodeURIComponent('店 A&B')}&city=${encodeURIComponent('杭州市')}&expected_version=7`);
  expect(send.mock.calls[1][0]).toBe('/api/trips/trip%20%2F%20one/places/manual/confirm');
  expect(JSON.parse(send.mock.calls[1][1].body)).toEqual(operation);
});
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

it('starts bounded planning and adopts only a named server plan with its input signature', async () => {
  const send = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', send);
  await api.startPlanning('trip / one', 'operation', 7, defaultPlanningBudget, 'parent');
  expect(send.mock.calls[0][0]).toBe('/api/trips/trip%20%2F%20one/runs');
  expect(JSON.parse(send.mock.calls[0][1].body)).toEqual({ kind: 'plan', operation_id: 'operation', expected_version: 7, budget: defaultPlanningBudget, parent_plan_id: 'parent' });
  await api.adoptPlan('one', 'plan-one', 'adopt-operation', 7, 'a'.repeat(64));
  expect(JSON.parse(send.mock.calls[1][1].body)).toEqual({ operation_id: 'adopt-operation', expected_version: 7, input_hash: 'a'.repeat(64) });
});

it('uses the atomic planning replacement endpoint with a frozen operation and both versions', async () => {
  const send = vi.fn().mockImplementation(async () => new Response('{}', { status: 200 }));
  vi.stubGlobal('fetch', send);
  await api.planningReplacements('trip / one');
  const operation = { operation_id: 'same-operation', expected_version: 7, expected_workspace_version: 9, input_hash: 'a'.repeat(64), candidate_id: 'old', replacement_id: 'new' };
  await api.replacePlanningCandidate('trip / one', operation);
  expect(send.mock.calls[0][0]).toBe('/api/trips/trip%20%2F%20one/planning-replacements');
  expect(send.mock.calls[1][0]).toBe('/api/trips/trip%20%2F%20one/planning-replace');
  expect(JSON.parse(send.mock.calls[1][1].body)).toEqual(operation);
});
