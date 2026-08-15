/**
 * GENERATED FILE — do not edit.
 *
 * Produced from the API's OpenAPI document by `npm run generate:api`.
 * Edit `src/clipforge/api/schemas.py` and regenerate instead; editing here
 * makes the types agree with nothing.
 *
 * Source: /workspace/clipper-ai/openapi.json
 * Schemas: 40
 */

export interface ActivityOut {
  at: string;
  kind: string;
  summary: string;
  state?: string;
  reference?: string;
}

export interface AnalyticsResponse {
  window_days: number;
  posts_measured: number;
  total_views?: number | null;
  total_likes?: number | null;
  avg_watch_pct?: number | null;
  series: MetricSeriesOut[];
  by_platform: PlatformBreakdownOut[];
  top: PublishedVideoOut[];
  note?: string;
}

/** What this deployment can actually do. */
export interface CapabilityOut {
  key: string;
  label: string;
  available: boolean;
  detail?: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface ChannelOut {
  id: string;
  name: string;
  niche: string;
  state: string;
  timezone: string;
  topics: string[];
  monetised: boolean;
  budget_monthly_cents: number;
  budget_spent_cents: number;
  budget_remaining_cents: number;
  consecutive_failures: number;
  circuit_opened_at?: string | null;
  last_error?: string;
  total_items: number;
  total_published: number;
  total_blocked: number;
  total_failed: number;
  created_at: string;
}

export interface ChannelStateUpdate {
  /** draft, active or paused */
  state: string;
}

/** Deleting an account asks for the password again. */
export interface DeleteAccountRequest {
  password: string;
}

/** An address and nothing else. */
export interface EmailRequest {
  email: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  retry_after_s?: number | null;
}

export interface ErrorResponse {
  error: ErrorBody;
}

export interface LoginRequest {
  email: string;
  password: string;
  tenant_id?: string;
}

export interface LoginResponse {
  tokens: TokenPairOut;
  memberships: MembershipOut[];
  unverified?: boolean;
}

export interface MembershipOut {
  user_id: string;
  tenant_id: string;
  tenant_name?: string;
  role: string;
  active: boolean;
}

export interface MeResponse {
  identity_id: string;
  email: string;
  tenant_id: string;
  user_id: string;
  role: string;
  session_id: string;
  expires_at: string;
  memberships: MembershipOut[];
}

/** Used where the answer must not depend on whether an account exists. */
export interface MessageResponse {
  message: string;
}

export interface MetricSeriesOut {
  key: string;
  label: string;
  unit?: string;
  points: SeriesPointOut[];
}

export interface OverviewResponse {
  tenant_id: string;
  stats: StatOut[];
  pipeline: PipelineStageOut[];
  activity: ActivityOut[];
  attention: string[];
  generated_at: string;
}

export interface PageChannelOut {
  items: ChannelOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface PagePublishedVideoOut {
  items: PublishedVideoOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface PageSourceOut {
  items: SourceOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface PageUploadOut {
  items: UploadOut[];
  total: number;
  limit: number;
  offset: number;
}

export interface PasswordResetRequest {
  token: string;
  new_password: string;
}

/** One stage of acquisition → transcription → clips → uploads. */
export interface PipelineStageOut {
  stage: string;
  label: string;
  total: number;
  done: number;
  failed: number;
  in_flight: number;
}

export interface PlatformBreakdownOut {
  platform: string;
  posts: number;
  views: number;
  likes: number;
  avg_watch_pct?: number | null;
}

export interface PublishedVideoOut {
  upload_id: string;
  channel_id: string;
  channel_name?: string;
  platform: string;
  title?: string;
  remote_post_id: string;
  published_at?: string | null;
  permalink?: string;
  views?: number | null;
  likes?: number | null;
  comments?: number | null;
  shares?: number | null;
  avg_watch_pct?: number | null;
  measured_at?: string | null;
  age_hours?: number | null;
}

export interface RefreshRequest {
  refresh_token: string;
}

export interface SeriesPointOut {
  at: string;
  value: number;
}

export interface SessionOut {
  session_id: string;
  ip?: string;
  user_agent?: string;
  issued_at: string;
  expires_at?: string | null;
  revoked: boolean;
  rotations: number;
  current?: boolean;
}

export interface SettingsResponse {
  identity_id: string;
  email: string;
  verified: boolean;
  tenant_id: string;
  role: string;
  memberships: MembershipOut[];
  sessions: SessionOut[];
  accounts: SocialAccountOut[];
  capabilities: CapabilityOut[];
}

export interface SignUpRequest {
  email: string;
  password: string;
}

export interface SocialAccountOut {
  id: string;
  platform: string;
  handle?: string;
  channel_id?: string | null;
  connected: boolean;
  needs_reauth?: boolean;
  detail?: string;
}

export interface SourceOut {
  id: string;
  title: string;
  kind: string;
  url?: string;
  creator?: string;
  language?: string;
  topics: string[];
  duration_s: number;
  has_transcript: boolean;
  rights_basis: string;
  rights_expires_at?: string | null;
  published_at?: string | null;
  created_at: string;
  acquisition_state?: string;
  media_path?: string;
  transcription_state?: string;
  word_count?: number;
}

/** One headline number, with what it means attached. */
export interface StatOut {
  key: string;
  label: string;
  value: number;
  detail?: string;
  unit?: string;
}

/** What this workspace is storing, and what the backend is doing. */
export interface StorageUsageOut {
  backend: string;
  objects?: number | null;
  bytes?: number | null;
  gigabytes?: number | null;
  largest_key?: string;
  largest_bytes?: number | null;
  operations?: Record<string, Record<string, number>>;
  total_calls?: number;
  total_failures?: number;
  total_retries?: number;
  note?: string;
}

export interface SubmitSourceRequest {
  url: string;
  channel_id?: string;
}

export interface TokenPairOut {
  access_token: string;
  refresh_token: string;
  token_type?: string;
  expires_in: number;
  session_id: string;
  tenant_id?: string;
}

export interface UploadOut {
  id: string;
  channel_id: string;
  channel_name?: string;
  account_id: string;
  platform: string;
  state: string;
  title?: string;
  caption?: string;
  visibility: string;
  run_at: string;
  next_attempt_at?: string | null;
  attempt_count: number;
  last_error?: string;
  remote_post_id?: string;
  published_at?: string | null;
  clip_id?: string | null;
}

export interface VerifyEmailRequest {
  token: string;
}
