import type { Transport, TripRequest } from './api';
import type { ConfirmedOrigin, Poi } from './places-types';
import type { Coordinate } from './amap';

export type Provenance = 'user' | 'source' | 'estimated' | 'unknown';
export interface ActivityConstraint {
  candidate_id: string;
  duration_minutes: number | null;
  duration_source: Provenance;
  cost_cny: number | null;
  cost_source: Provenance;
  required: boolean;
  excluded: boolean;
  locked: boolean;
  state: 'pending' | 'completed' | 'skipped';
  fixed_start_at: string | null;
  not_before: string | null;
  not_after: string | null;
}
export interface PlanningConstraints {
  origin_city?: string | null;
  timezone: string;
  return_to_origin: boolean;
  end_candidate_id: string | null;
  end_at: string | null;
  max_walk_meters: number | null;
  disabled_modes: Exclude<Transport, 'mixed'>[];
  transfer_buffer_minutes: number;
  activity_constraints: ActivityConstraint[];
}
export const defaultPlanning: PlanningConstraints = { timezone: 'Asia/Shanghai', return_to_origin: false, end_candidate_id: null, end_at: null, max_walk_meters: null, disabled_modes: [], transfer_buffer_minutes: 10, activity_constraints: [] };
export const newConstraint = (candidate_id: string): ActivityConstraint => ({ candidate_id, duration_minutes: null, duration_source: 'unknown', cost_cny: null, cost_source: 'unknown', required: false, excluded: false, locked: false, state: 'pending', fixed_start_at: null, not_before: null, not_after: null });
export interface PlanningIssue { code: string; message: string; candidate_id?: string }
export interface PlanningPlace { candidate_id: string; name: string; category: 'eat' | 'play' | 'stay'; kind: string; city: string | null; run_id: string; poi: Poi; activities: Record<string, unknown>[]; sources: Record<string, unknown>[]; evidence: Record<string, unknown>[]; mentions: Record<string, unknown>[] }
export interface PlanningInput { trip_id?: string; request_version: number; input_hash: string; request: TripRequest; planning: PlanningConstraints; origin: ConfirmedOrigin | null; selected_ids: string[]; selected: PlanningPlace[]; missing: PlanningIssue[]; active_plan_id: string | null }
export interface PlanningBudget { max_routes: number; max_combinations: number; max_seconds: number }
export const defaultPlanningBudget: PlanningBudget = { max_routes: 40, max_combinations: 3, max_seconds: 600 };
export interface PlanningResult { plan_ids: string[]; input_hash: string; summary: string; missing?: PlanningIssue[] }
export interface RouteLeg {
  id: string;
  mode: Exclude<Transport, 'mixed'>;
  origin: Coordinate;
  destination: Coordinate;
  departure_at: string;
  read_at: string;
  valid_until: string | null;
  api_version: string;
  status: string;
  duration_seconds: number | null;
  distance_meters: number | null;
  walking_distance_meters: number | null;
  geometry: Coordinate[][];
  geometry_complete: boolean;
  cost_cny: number | null;
  cost_source: string;
  duration_source: string;
  conditional: boolean;
  conditions: string[];
}
export interface RouteItem { kind: 'route'; start_at: string | null; end_at: string | null; from_candidate_id?: string | null; to_candidate_id?: string | null; leg: RouteLeg }
export interface ActivityItem { kind: 'activity'; candidate_id: string; name: string; poi: Poi; category: 'eat' | 'play' | 'stay'; start_at: string | null; end_at: string | null; duration_minutes: number | null; duration_source: Provenance; cost_cny: number | null; cost_source: Provenance; state: ActivityConstraint['state']; locked: boolean; required: boolean }
export interface WaitItem { kind: 'wait'; start_at: string | null; end_at: string | null; reason: string }
export type ItineraryItem = RouteItem | ActivityItem | WaitItem;
export interface Plan { id: string; trip_id: string; version: number; request_version: number; input_hash: string; parent_plan_id: string | null; run_id: string | null; status: 'draft' | 'conditional' | 'partial' | 'conflict' | 'validated'; created_at: string; payload: { input: PlanningInput; itinerary: ItineraryItem[]; unknowns: PlanningIssue[]; conflicts: PlanningIssue[]; usage: Record<string, unknown>; diff: Record<string, unknown>[]; valid_until: string | null } }
