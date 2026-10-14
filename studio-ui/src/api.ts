// CineCrew Studio UI — thin typed client for the FastAPI contract in src/studio/api.py.
// Every call throws ApiError carrying the server's message (and body) so screens can show it verbatim.

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function detailText(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d))
      return d
        .map((x) => (x && typeof x === "object" && "msg" in x ? `${(x as { loc?: unknown[] }).loc?.join(".") ?? ""}: ${(x as { msg: string }).msg}` : JSON.stringify(x)))
        .join("; ");
    return JSON.stringify(d);
  }
  return fallback;
}

export async function api<T = unknown>(path: string, opts: { method?: string; body?: unknown } = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
      headers: opts.body !== undefined ? { "content-type": "application/json" } : undefined,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch (e) {
    throw new ApiError(0, `Không kết nối được máy chủ Studio (${(e as Error).message})`, null);
  }
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) {
    const fallback = typeof body === "string" && body ? body.slice(0, 300) : `HTTP ${res.status}`;
    throw new ApiError(res.status, detailText(body, fallback), body);
  }
  return body as T;
}

export const mediaUrl = (sha: string | null | undefined) => (sha ? `/media/${sha}` : undefined);

// ── types (subset of what the API returns; extra fields are ignored) ─────────────
export interface BudgetSummary {
  limit_usd: number;
  committed_usd: number;
  reserved_usd: number;
  remaining_usd: number;
  committed_by_kind: Record<string, number>;
}

export interface ProjectRow {
  id: string;
  name: string;
  kind: string;
  mode: string;
  settings: Record<string, any>;
  budget_limit_usd: number;
  created_at: number;
  budget?: BudgetSummary;
  episodes?: number;
}

export interface JobView {
  id: string;
  key: string;
  kind: string;
  state: string;
  provider_state: string;
  verdict: string;
  verdict_source: string | null;
  error: string | null;
  locked: boolean;
  cache_hit: boolean;
  attempts: number;
  max_attempts: number;
  cost_usd: number;
  media_sha: string | null;
  media_meta: Record<string, any> | null;
  review: Record<string, any> | null;
  fixture: boolean;
}

export interface Asset {
  id: string;
  kind: string;
  name: string;
  data: { asset_id: string; identity?: string; wardrobe?: Record<string, string>; voice?: Record<string, string> };
  locked: number;
  version: number;
  canonical_media: string | null;
  reference_job: JobView | null;
  preview_sha: string | null;
}

export interface EpisodeSummary {
  id: string;
  number: number;
  title: string;
  version: number;
  storyboard_approved: number;
  readiness: string;
}

export interface ProjectDetail extends Omit<ProjectRow, "episodes"> {
  assets: Asset[];
  episodes: EpisodeSummary[];
  budget: BudgetSummary;
}

export interface Template {
  template_id: string;
  title: string;
  kind: string;
  aspect: string;
  episodes: { number: number; title: string; shots: number }[];
}

export interface Shot {
  index: number;
  shot_id: string;
  action: string;
  beat: string;
  dialogue: string | null;
  speaker: string | null;
  duration: number;
  lighting: string;
  camera: { shot_scale?: string; angle?: string; movement?: string };
  location: string;
  narration: string | null;
  locked: boolean;
  keyframe_locked: boolean;
  voice_locked: boolean;
  jobs: Partial<Record<"kf" | "clip" | "tts" | "narr", JobView>>;
}

export interface Readiness {
  status: "final" | "draft" | "blocked" | string;
  reason: string;
  missing: string[];
  not_accepted: string[];
  contains_fixture_media?: boolean;
  storyboard_approved?: boolean;
}

export interface ExportRow {
  id: string;
  kind: string;
  status: string;
  created_at: number;
}

export interface Episode {
  id: string;
  project_id: string;
  number: number;
  title: string;
  version: number;
  storyboard_approved: boolean;
  shots: Shot[];
  readiness: Readiness;
  exports: ExportRow[];
  references: JobView[];
  counts: Record<string, number>;
}

export interface ExportView {
  id: string;
  episode_id: string;
  kind: string;
  status: string;
  files: Record<string, string>;
  manifest: Record<string, any>;
  reused: boolean;
  created_at: number;
}

export interface StudioEvent {
  id: number;
  episode_id: string | null;
  job_id: string | null;
  level: string;
  message: string;
  created_at: number;
}

export interface Attempt {
  id: string;
  n: number;
  purpose: string;
  provider: string;
  model: string;
  request: { params?: Record<string, any>; adaptations?: unknown[] } | null;
  provider_state: string;
  verdict: string | null;
  review: Record<string, any> | null;
  error: string | null;
  cost_estimate_usd: number | null;
  cost_actual_usd: number | null;
  media_sha: string | null;
  created_at: number;
}

export interface JobDetail extends Omit<JobView, "attempts"> {
  spec: Record<string, any>;
  attempts: Attempt[];
}

export const BUSY_STATES = ["ready", "running", "pending"];

export const fmtUsd = (n: number | null | undefined) =>
  n === null || n === undefined ? "không rõ giá" : `$${Number(n).toFixed(n !== 0 && Math.abs(n) < 0.01 ? 4 : 2)}`;

export const fmtTime = (t: number) => new Date(t * 1000).toLocaleString("vi-VN");
