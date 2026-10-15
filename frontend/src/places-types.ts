import type { Trip } from './api';
import type { Candidate } from './discovery-types';
import type { Coordinate } from './amap';

export interface Poi {
  id: string;
  name: string | null;
  location: Coordinate;
  address: string | null;
  city: string | null;
  adcode: string | null;
  type: string | null;
  entrance_location: Coordinate | null;
  read_at: string;
  api_version: string;
  missing_fields: string[];
}
export interface PlaceMatch {
  candidate_id: string;
  run_id: string;
  request_version: number;
  status: 'unique' | 'ambiguous' | 'unmatched';
  poi: Poi | null;
  options: Poi[];
  reason: string;
  confirmed: boolean;
  updated_at?: string;
}
export interface PlacePoint extends Poi {
  name: string;
  candidate_ids: string[];
  source_ids: string[];
  activity_ids: string[];
  categories: Candidate['category'][];
}
export interface ConfirmedOrigin {
  city?: string | null;
  name: string;
  location: Coordinate;
  poi_id: string | null;
  source: 'amap' | 'map';
  request_origin: string | null;
}
export interface PlacesSnapshot {
  run_id: string | null;
  request_version: number;
  candidates: Candidate[];
  manual_candidates?: ManualCandidate[];
  matches: PlaceMatch[];
  points: PlacePoint[];
  trip: Trip;
}
export interface ManualCandidate {
  id: string;
  trip_id: string;
  source: 'user';
  run_id: null;
  request_version: number;
  created_at: string;
  name: string;
  city: string | null;
  category: Candidate['category'];
  kind: 'place';
  mention_ids: string[];
  evidence_ids: string[];
  source_ids: string[];
  match_status: 'unique';
  poi: Poi;
}
export interface ManualPlaceSearch {
  search_id: string;
  request_version: number;
  query: string;
  city: string;
  items: Poi[];
}
export interface ManualPlaceConfirmation {
  operation_id: string;
  expected_version: number;
  expected_workspace_version: number;
  search_id: string;
  poi_id: string;
  category: Candidate['category'];
}
export interface OriginSearch {
  search_id: string;
  items: Poi[];
  request_origin: string | null;
  request_version: number;
}
export type OriginConfirmation = { source: 'amap'; search_id: string; poi_id: string }
  | { source: 'map'; name: string; location: Coordinate };
export type MapAction = 'initialize' | 'refresh' | 'add' | 'remove' | 'select' | 'unselect';
