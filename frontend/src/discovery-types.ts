export interface DiscoveryBudget {
  max_steps: number;
  max_seconds: number;
  max_searches: number;
  target_notes: number;
  comment_limit: number;
  include_replies: boolean;
  reply_limit: number;
  reserved_gap_searches: number;
  max_model_calls: number;
  max_images: number;
  max_tokens: number;
}
export const defaultDiscoveryBudget: DiscoveryBudget = {
  max_steps: 160, max_seconds: 600, max_searches: 8, target_notes: 30, comment_limit: 20,
  include_replies: false, reply_limit: 20, reserved_gap_searches: 2,
  max_model_calls: 64, max_images: 90, max_tokens: 800000,
};
export interface Source {
  id: string;
  trip_id: string;
  run_id?: string;
  request_version?: number;
  platform: string;
  kind: 'note' | 'comment';
  external_id: string | null;
  parent_source_id: string | null;
  reply_to_source_id?: string | null;
  title: string;
  text: string;
  images: string[];
  published_at: string | null;
  completeness: 'complete' | 'partial' | 'unknown';
  read_status: 'success' | 'failed';
  error_code: string | null;
  dedupe_confidence: 'high' | 'low';
  first_read_at: string;
  last_read_at: string;
  read_at?: string;
  queries?: { query: string; purpose: string }[];
}
export interface Evidence {
  id: string;
  trip_id: string;
  run_id: string;
  request_version: number;
  source_id: string;
  mention_id: string | null;
  object_text: string;
  dimension: string;
  stance: 'positive' | 'negative' | 'mixed' | 'question' | 'unknown';
  nature: 'experience' | 'question' | 'hearsay' | 'wish' | 'suggestion' | 'content_reaction' | 'unknown';
  quote: string;
  link_status: 'linked' | 'unlinked' | 'uncertain';
  model_id: string | null;
  schema_version: string;
  source?: Source;
  citation_kind: 'text' | 'image';
  image_index: number | null;
  image_observation?: { source_id: string; image_index: number; text: string; uncertain: true } | null;
}
export interface Candidate {
  id: string;
  trip_id: string;
  run_id: string;
  request_version: number;
  name: string;
  category: 'eat' | 'play' | 'stay';
  city: string | null;
  direction: string | null;
  kind: 'place' | 'direction' | 'generic';
  mention_ids: string[];
  evidence_ids: string[];
  match_status: 'unmatched';
  summary: string;
  uncertainty: string[];
  stats: { author_count: number; positive_count: number; negative_count: number; uncertain_count: number };
}
export interface Gap {
  id: string;
  trip_id: string;
  run_id: string;
  request_version: number;
  kind: 'meal' | 'lodging';
  status: 'open' | 'undetermined' | 'filled' | 'unfilled';
  period: 'lunch' | 'dinner' | 'overnight' | null;
  area: string | null;
  reason: string;
  conditions: string[];
  source_ids: string[];
  candidate_ids: string[];
}
export interface CandidatePage { items: Candidate[]; total: number; limit: number; offset: number }
export interface CandidateEvidence { items: Evidence[]; sources: Source[] }
export interface DiscoveryCoverage {
  notes_obtained?: number;
  notes_analyzed?: number;
  notes_failed?: number;
  notes_reused?: number;
  notes_reused_analyzed?: number;
  comments_obtained?: number;
  comments_analyzed?: number;
  comments_judged?: number;
  comments_selected?: number;
  comments_reused?: number;
  comments_reused_analyzed?: number;
  comments_reused_judged?: number;
  judgement_rows?: number;
  evidence_count?: number;
  images_obtained?: number;
  images_analyzed_unique?: number;
  images_unanalyzed?: number;
  images_reused_obtained?: number;
  images_reused_analyzed_unique?: number;
  images_reused_unanalyzed?: number;
  targets?: { notes: number; comment_limit: number; reply_limit: number };
  partial_reasons?: string[];
}
export interface InterpretedValue {
  value: string | number | null;
  provenance: 'user' | 'default' | 'inferred' | 'unknown';
  evidence: string | null;
}
export interface Understanding {
  intent: {
    destination: InterpretedValue; origin: InterpretedValue; start_at: InterpretedValue;
    duration_minutes: InterpretedValue; transport: InterpretedValue; budget_cny: InterpretedValue;
    participants: InterpretedValue; preferences: InterpretedValue;
    destination_mode: 'explicit' | 'explore'; themes: string[]; exclusions: string[]; must_visit: string[];
    missing_for_scheduling: ('origin' | 'start_at' | 'duration_minutes')[];
  };
  query_plan: { query: string; purpose: 'main' | 'discovery'; direction_hint: string | null }[];
  reserved_gap_searches: number;
}
export interface TripUsage { external_calls: number; model_tokens: number; images: number; unknown_cost_calls: number; cost_cny: number | null }
export interface DiscoveryResult {
  summary?: string;
  partial_reasons?: string[];
  understanding?: Understanding;
  coverage?: DiscoveryCoverage;
  directions?: Candidate[];
  gaps?: Gap[];
  model?: { model_id: string; schema_version: string; semantic_quality?: 'not_validated' };
  trip_usage?: TripUsage;
}
