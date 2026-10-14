export type Transport = 'mixed' | 'walking' | 'transit' | 'bicycling' | 'electrobike' | 'driving';
export interface TripRequest {
  destination: string | null;
  origin: string | null;
  start_at: string | null;
  duration_minutes: number | null;
  transport: Transport;
  transport_source: 'default' | 'user';
  budget_cny: number | null;
  participants: number;
  preferences: string;
}
export interface Workspace {
  active_tab: 'overview' | 'map' | 'plan';
  category: 'all' | 'eat' | 'play' | 'stay';
  map_center: [number, number] | null;
  map_zoom: number;
  selected_ids: string[];
  map_visible_ids: string[];
  map_initialized?: boolean;
  map_city?: string | null;
  confirmed_origin?: ConfirmedOrigin | null;
}
export interface Message {
  id: string;
  trip_id: string;
  role: string;
  content: string;
  status: string;
  created_at: string;
  discovery_run?: Run;
  discovery_run_id?: string | null;
}
export interface Run {
  id: string;
  trip_id: string;
  kind: 'prepare_workspace' | 'discover';
  status: 'pending' | 'running' | 'paused' | 'recoverable' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'stale';
  stage: 'validate_request' | 'read_workspace' | 'summarize' | 'done' | 'understanding' | 'discovering' | 'fetching' | 'extracting' | 'evaluating' | 'candidates_ready';
  request_version: number;
  generation: number;
  params: Record<string, unknown>;
  budget: { max_steps: number; max_seconds: number } & Partial<DiscoveryBudget>;
  checkpoint: { completed_stages?: string[]; understanding?: Understanding; [key: string]: unknown };
  usage: { steps: number; elapsed_seconds: number; external_calls: number; model_tokens: number; images: number; prompt_tokens?: number; completion_tokens?: number; reserved_tokens?: number; unknown_cost_calls?: number; unknown_result_calls?: number; interrupted_stage_time_unknown?: boolean; searches?: number; detail_calls?: number; model_calls?: number } & DiscoveryCoverage;
  trip_usage?: TripUsage;
  result: {
    conditions: { conditions_complete: boolean; missing: string[]; destination: string | null; transport: string };
    workspace: { messages_count: number; selected_count: number; map_visible_count: number; workspace_version: number; active_tab: string };
    summary: string;
    not_connected: string[];
  } | DiscoveryResult | null;
  error_code: string | null;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
}
export interface Trip {
  id: string;
  name: string;
  status: 'draft' | 'archived';
  request: TripRequest;
  request_version: number;
  workspace: Workspace;
  workspace_version: number;
  created_at: string;
  updated_at: string;
  messages: Message[];
  runs: Run[];
}
export interface TripList {
  items: TripSummary[];
  total: number;
  limit: number;
  offset: number;
}
export type TripSummary = Omit<Trip, 'messages' | 'runs'>;
export class ApiError extends Error {
  constructor(message: string, readonly status: number, readonly code: string, readonly currentVersion?: number) {
    super(message);
  }
}
async function call<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('无法连接本机服务。编辑仍保留，请检查服务后重试。', 0, 'network_error');
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    const message = typeof detail?.message === 'string' ? detail.message : typeof detail === 'string' ? detail : '操作未完成，请重试。';
    throw new ApiError(message, response.status, detail?.code ?? 'request_failed', detail?.current_version);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  list: (includeArchived = false, offset = 0) => call<TripList>(`/trips?include_archived=${includeArchived}&limit=50&offset=${offset}`),
  get: (id: string) => call<Trip>(`/trips/${encodeURIComponent(id)}`),
  create: (operation_id: string) => call<Trip>('/trips', 'POST', { operation_id }),
  update: (id: string, expected_version: number, draft: { name: string; request: TripRequest }) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}`, 'PATCH', { expected_version, ...draft }),
  workspace: (id: string, expected_version: number, workspace: Workspace) => {
    const { map_initialized: _initialized, confirmed_origin: _origin, ...editable } = workspace;
    return call<Trip>(`/trips/${encodeURIComponent(id)}/workspace`, 'PATCH', { expected_version, workspace: editable });
  },
  message: (id: string, operation_id: string, content: string, discovery?: { expected_version: number; budget: DiscoveryBudget }) =>
    call<Message>(`/trips/${encodeURIComponent(id)}/messages`, 'POST', { operation_id, content, ...(discovery ? { start_discovery: true, ...discovery } : {}) }),
  archive: (id: string, expected_version: number, archived: boolean) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}/archive`, 'POST', { expected_version, archived }),
  remove: (id: string, expected_version: number) => call<void>(`/trips/${encodeURIComponent(id)}?expected_version=${expected_version}`, 'DELETE'),
  startRun: (id: string, operation_id: string, expected_version: number) =>
    call<Run>(`/trips/${encodeURIComponent(id)}/runs`, 'POST', { operation_id, expected_version, kind: 'prepare_workspace' }),
  getRun: (id: string, trip_id: string) => call<Run>(`/runs/${encodeURIComponent(id)}?trip_id=${encodeURIComponent(trip_id)}`),
  runAction: (id: string, action: 'cancel' | 'resume', trip_id: string, generation: number) =>
    call<Run>(`/runs/${encodeURIComponent(id)}/${action}`, 'POST', { trip_id, generation }),
  startDiscovery: (id: string, operation_id: string, expected_version: number, budget: DiscoveryBudget, parent_run_id?: string) =>
    call<Run>(`/trips/${encodeURIComponent(id)}/runs`, 'POST', { operation_id, expected_version, kind: 'discover', budget, ...(parent_run_id ? { parent_run_id } : {}) }),
  candidates: (id: string, run_id: string, category: 'all' | 'eat' | 'play' | 'stay', offset = 0) =>
    call<CandidatePage>(`/trips/${encodeURIComponent(id)}/candidates?run_id=${encodeURIComponent(run_id)}&limit=20&offset=${offset}${category === 'all' ? '' : `&category=${category}`}`),
  evidence: (id: string, candidate_id: string) => call<CandidateEvidence>(`/trips/${encodeURIComponent(id)}/candidates/${encodeURIComponent(candidate_id)}/evidence`),
  source: (id: string, source_id: string, run_id: string) => call<Source>(`/trips/${encodeURIComponent(id)}/sources/${encodeURIComponent(source_id)}?run_id=${encodeURIComponent(run_id)}`),
  sources: (id: string, run_id: string) => call<{ items: Source[] }>(`/trips/${encodeURIComponent(id)}/sources?run_id=${encodeURIComponent(run_id)}`),
  gaps: (id: string, run_id: string) => call<{ items: Gap[] }>(`/trips/${encodeURIComponent(id)}/gaps?run_id=${encodeURIComponent(run_id)}`),
  adoptDirection: (id: string, candidate_id: string, run_id: string, operation_id: string, expected_version: number) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}/directions/${encodeURIComponent(candidate_id)}/adopt`, 'POST', { run_id, operation_id, expected_version }),
  places: (id: string, run_id?: string) => call<PlacesSnapshot>(`/trips/${encodeURIComponent(id)}/places${run_id ? `?run_id=${encodeURIComponent(run_id)}` : ''}`),
  matchPlaces: (id: string, expected_version: number, expected_workspace_version: number, run_id: string) =>
    call<PlacesSnapshot>(`/trips/${encodeURIComponent(id)}/places/match`, 'POST', { expected_version, expected_workspace_version, run_id, refresh_map: false }),
  confirmPlace: (id: string, candidate_id: string, expected_version: number, expected_workspace_version: number, poi_id: string) =>
    call<PlacesSnapshot>(`/trips/${encodeURIComponent(id)}/places/candidates/${encodeURIComponent(candidate_id)}/confirm`, 'POST', { expected_version, expected_workspace_version, poi_id }),
  mapAction: (id: string, expected_version: number, expected_workspace_version: number, action: MapAction, options: { candidate_id?: string; category?: Workspace['category']; city?: string | null } = {}) => {
    const { category, ...filters } = options;
    return call<PlacesSnapshot>(`/trips/${encodeURIComponent(id)}/places/map`, 'POST', { expected_version, expected_workspace_version, action, ...filters, ...(category && category !== 'all' ? { category } : {}) });
  },
  searchOrigin: (id: string, query: string, city: string, expected_version: number) =>
    call<OriginSearch>(`/trips/${encodeURIComponent(id)}/places/origin/search?query=${encodeURIComponent(query)}&city=${encodeURIComponent(city)}&expected_version=${expected_version}`),
  confirmOrigin: (id: string, expected_version: number, expected_workspace_version: number, confirmation: OriginConfirmation) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}/places/origin/confirm`, 'POST', { expected_version, expected_workspace_version, ...confirmation }),
};
import type { CandidateEvidence, CandidatePage, DiscoveryBudget, DiscoveryCoverage, DiscoveryResult, Gap, Source, TripUsage, Understanding } from './discovery-types';
import type { ConfirmedOrigin, MapAction, OriginConfirmation, OriginSearch, PlacesSnapshot } from './places-types';
