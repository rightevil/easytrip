import type { Transport, TripRequest } from './api';
import type { ConfirmedOrigin, Poi } from './places-types';
import type { Coordinate } from './amap';

export type Provenance = 'user' | 'source' | 'estimated' | 'unknown';
export interface ActivityConstraint {
  candidate_id: string;
  date?: string | null;
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
  rail_segments?: RailSegment[];
  day_windows?: DayWindow[];
  overnight_stays?: OvernightStay[];
  origin_city?: string | null;
  progress_at?: string | null;
  progress_origin?: ConfirmedOrigin | null;
  dietary_restrictions?: string[];
  timezone: string;
  return_to_origin: boolean;
  end_candidate_id: string | null;
  end_at: string | null;
  max_walk_meters: number | null;
  disabled_modes: Exclude<Transport, 'mixed'>[];
  transfer_buffer_minutes: number;
  activity_constraints: ActivityConstraint[];
}
export interface DayWindow { date: string; start_at: string | null; end_at: string | null }
export interface OvernightStay { date: string; candidate_id: string }
export interface RailSegment { id: string; date: string; departure_station_id: string | null; arrival_station_id: string | null; train_number: string | null; departure_at: string | null; arrival_at: string | null; confirmed: boolean; source: 'user'; boarding_buffer_minutes: number; arrival_buffer_minutes: number; cost_cny: number | null }
export const defaultPlanning: PlanningConstraints = { timezone: 'Asia/Shanghai', progress_at: null, progress_origin: null, dietary_restrictions: [], return_to_origin: false, end_candidate_id: null, end_at: null, max_walk_meters: null, disabled_modes: [], transfer_buffer_minutes: 10, activity_constraints: [] };
export const newConstraint = (candidate_id: string): ActivityConstraint => ({ candidate_id, duration_minutes: null, duration_source: 'unknown', cost_cny: null, cost_source: 'unknown', required: false, excluded: false, locked: false, state: 'pending', fixed_start_at: null, not_before: null, not_after: null });
export interface PlanningIssue { code: string; message: string; candidate_id?: string; date?: string; period?: 'lunch' | 'dinner'; window_start?: string; window_end?: string; meal_window?: MealSupplement['meal_window']; anchor?: { name?: string | null; address?: string | null; city?: string | null }; relaxations?: string[] }
export interface PlanningPlace { candidate_id: string; name: string; category: 'eat' | 'play' | 'stay'; kind: string; city: string | null; run_id: string | null; source?: 'user'; poi: Poi; activities: Record<string, unknown>[]; sources: Record<string, unknown>[]; evidence: Record<string, unknown>[]; mentions: Record<string, unknown>[] }
export interface MealSupplement extends PlanningPlace { category: 'eat'; meal_window: { period: 'lunch' | 'dinner'; start_at: string; end_at: string; anchor_candidate_id: string }; constraint: Partial<ActivityConstraint> }
export interface LodgingSupplement extends PlanningPlace { category: 'stay'; lodging_window: { date: string; anchor_candidate_id: string | null; anchor_name: string; anchor_location: Coordinate; city: string } }
export type PlanningSupplement = MealSupplement | LodgingSupplement;
export interface PlanningReplacements { trip_id: string; request_version: number; workspace_version: number; candidates: { candidate_id: string; name: string; category: PlanningPlace['category']; poi: Poi }[] }
export interface PlanningReplacement { operation_id: string; expected_version: number; expected_workspace_version: number; input_hash: string; candidate_id: string; replacement_id: string }
export interface PlanningInput { trip_id?: string; request_version: number; input_hash: string; request: TripRequest; planning: PlanningConstraints; origin: ConfirmedOrigin | null; selected_ids: string[]; selected: PlanningPlace[]; lodging_candidates?: PlanningPlace[]; lodgings?: PlanningPlace[]; station_candidates?: PlanningPlace[]; stations?: PlanningPlace[]; missing: PlanningIssue[]; active_plan_id: string | null }
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
  conditions: (string | { code: string; message: string })[];
}
export interface RouteItem { kind: 'route'; start_at: string | null; end_at: string | null; from_candidate_id?: string | null; to_candidate_id?: string | null; leg: RouteLeg; historical?: boolean; execution_unknown?: boolean; untravelled?: boolean }
export interface ActivityItem { kind: 'activity'; candidate_id: string; name: string; poi: Poi; category: 'eat' | 'play' | 'stay'; start_at: string | null; end_at: string | null; duration_minutes: number | null; duration_source: Provenance; cost_cny: number | null; cost_source: Provenance; state: ActivityConstraint['state']; locked: boolean; required: boolean; historical?: boolean; execution_unknown?: boolean; untravelled?: boolean }
export interface WaitItem { kind: 'wait'; start_at: string | null; end_at: string | null; reason: string; historical?: boolean; execution_unknown?: boolean; untravelled?: boolean }
export interface RailItem { kind: 'rail'; rail_id: string; train_number: string | null; start_at: string | null; end_at: string | null; departure_station: PlanningPlace | null; arrival_station: PlanningPlace | null; source: 'user'; confirmed: boolean; boarding_buffer_minutes: number; arrival_buffer_minutes: number; cost_cny: number | null; cost_source: Provenance; historical?: boolean; execution_unknown?: boolean; untravelled?: boolean }
export type ItineraryItem = (RouteItem | ActivityItem | WaitItem | RailItem) & { date?: string };
export interface DayPlan { date: string; start_at: string | null; end_at: string | null; origin: ConfirmedOrigin | null; endpoint: ConfirmedOrigin | null; lodging_candidate_id: string | null; overnight_rail_id?: string | null; continuing_rail_id?: string | null; itinerary: ItineraryItem[]; unknowns: PlanningIssue[]; conflicts: PlanningIssue[] }
export interface Plan { id: string; trip_id: string; version: number; request_version: number; input_hash: string; parent_plan_id: string | null; run_id: string | null; status: 'draft' | 'conditional' | 'partial' | 'conflict' | 'validated'; created_at: string; payload: { input: PlanningInput; days?: DayPlan[]; supplements?: PlanningSupplement[]; itinerary: ItineraryItem[]; unknowns: PlanningIssue[]; conflicts: PlanningIssue[]; usage: Record<string, unknown>; diff: Record<string, unknown>[]; valid_until: string | null } }
