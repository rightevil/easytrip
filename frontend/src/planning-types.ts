import type { Transport, TripRequest } from './api';
import type { ConfirmedOrigin, Poi } from './places-types';

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
export interface PlanningInput { request_version: number; input_hash: string; request: TripRequest; planning: PlanningConstraints; origin: ConfirmedOrigin | null; selected_ids: string[]; selected: PlanningPlace[]; missing: PlanningIssue[]; active_plan_id: string | null }
export interface Plan { id: string; trip_id: string; version: number; request_version: number; input_hash: string; parent_plan_id: string | null; run_id: string | null; status: 'draft' | 'conditional' | 'partial' | 'conflict' | 'validated'; created_at: string; payload: { input: PlanningInput; itinerary: Record<string, unknown>[]; unknowns: PlanningIssue[]; conflicts: PlanningIssue[]; usage: Record<string, unknown>; diff: Record<string, unknown>[]; valid_until: string | null } }
