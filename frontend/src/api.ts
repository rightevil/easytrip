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
}
export interface Message {
  id: string;
  trip_id: string;
  role: string;
  content: string;
  status: string;
  created_at: string;
}
export interface Run {
  id: string;
  trip_id: string;
  kind: 'prepare_workspace';
  status: 'pending' | 'running' | 'recoverable' | 'completed' | 'partial' | 'failed' | 'cancelled' | 'stale';
  stage: 'validate_request' | 'read_workspace' | 'summarize' | 'done';
  request_version: number;
  generation: number;
  params: Record<string, unknown>;
  budget: { max_steps: number; max_seconds: number };
  checkpoint: { completed_stages?: string[]; [key: string]: unknown };
  usage: { steps: number; elapsed_seconds: number; external_calls: number; model_tokens: number; images: number };
  result: {
    conditions: { conditions_complete: boolean; missing: string[]; destination: string | null; transport: string };
    workspace: { messages_count: number; selected_count: number; map_visible_count: number; workspace_version: number; active_tab: string };
    summary: string;
    not_connected: string[];
  } | null;
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
    const message = typeof detail?.message === 'string' ? detail.message : '操作未完成，请重试。';
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
  workspace: (id: string, expected_version: number, workspace: Workspace) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}/workspace`, 'PATCH', { expected_version, workspace }),
  message: (id: string, operation_id: string, content: string) =>
    call<Message>(`/trips/${encodeURIComponent(id)}/messages`, 'POST', { operation_id, content }),
  archive: (id: string, expected_version: number, archived: boolean) =>
    call<Trip>(`/trips/${encodeURIComponent(id)}/archive`, 'POST', { expected_version, archived }),
  remove: (id: string, expected_version: number) => call<void>(`/trips/${encodeURIComponent(id)}?expected_version=${expected_version}`, 'DELETE'),
  startRun: (id: string, operation_id: string, expected_version: number) =>
    call<Run>(`/trips/${encodeURIComponent(id)}/runs`, 'POST', { operation_id, expected_version, kind: 'prepare_workspace' }),
  getRun: (id: string, trip_id: string) => call<Run>(`/runs/${encodeURIComponent(id)}?trip_id=${encodeURIComponent(trip_id)}`),
  runAction: (id: string, action: 'cancel' | 'resume', trip_id: string, generation: number) =>
    call<Run>(`/runs/${encodeURIComponent(id)}/${action}`, 'POST', { trip_id, generation }),
};
