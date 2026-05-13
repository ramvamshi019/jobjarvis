/**
 * Resume + matches API client.
 *
 * Hits the FastAPI endpoints we added/patched:
 *   POST /api/resumes/upload       (multipart) → embed + match in one shot
 *   GET  /api/resumes              → list resumes
 *   GET  /api/matches?limit=N      → persisted top matches
 *   POST /api/matches/refresh      → recompute against current active resume
 */
import { authHeaders } from "@/lib/auth";

export interface ResumeRow {
  id: number;
  name: string;
  target_role: string | null;
  is_active: boolean;
  experience_level: string | null;
  overall_strength_score: number | null;
  created_at: string;
}

export interface UploadResponse {
  id: number;
  name: string;
  target_role: string | null;
  skills: string[] | null;
  experience_level: string | null;
  overall_strength_score: number | null;
  is_active: boolean;
  matches_computed: number;
}

export interface MatchRow {
  job_id: number;
  title: string;
  company: string;
  location: string | null;
  remote_type: string | null;
  salary_min: number | null;
  salary_max: number | null;
  url: string | null;
  apply_url: string | null;
  posted_at: string | null;
  first_seen_at: string | null;
  source_ats: string | null;
  match_score: number;
  sim_score: number | null;
  salary_fit: number | null;
  location_fit: number | null;
  freshness_score: number | null;
}

export interface MatchesResponse {
  user_id: number;
  count: number;
  matches: MatchRow[];
}

// ── Resumes ──────────────────────────────────────────────────────────────────

export async function uploadResume(
  file: File,
  targetRole?: string,
): Promise<UploadResponse> {
  const fd = new FormData();
  fd.append("file", file);
  if (targetRole) fd.append("target_role", targetRole);

  const res = await fetch("/api/resumes/upload", {
    method: "POST",
    headers: { ...authHeaders() },
    body: fd,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data?.detail || `Upload failed: ${res.status}`);
  }
  return data as UploadResponse;
}

export async function listResumes(): Promise<ResumeRow[]> {
  const res = await fetch("/api/resumes", {
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw new Error(`Failed to list resumes: ${res.status}`);
  return res.json();
}

export async function activateResume(
  resumeId: number,
): Promise<{ message: string; matches_computed: number }> {
  const res = await fetch(`/api/resumes/${resumeId}/activate`, {
    method: "PATCH",
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw new Error(`Failed to activate resume: ${res.status}`);
  return res.json();
}

// ── Matches ──────────────────────────────────────────────────────────────────

export type CountryFilter = "us" | "remote" | "all";

export async function getMatches(
  opts: { limit?: number; country?: CountryFilter; recencyDays?: number | null } = {},
): Promise<MatchesResponse> {
  const limit = opts.limit ?? 50;
  const country = opts.country ?? "us";
  const params = new URLSearchParams({
    limit: String(limit),
    country,
  });
  if (opts.recencyDays && opts.recencyDays > 0) {
    params.set("recency_days", String(opts.recencyDays));
  }
  const res = await fetch(`/api/matches?${params}`, {
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw new Error(`Failed to load matches: ${res.status}`);
  return res.json();
}

export async function refreshMatches(top = 50): Promise<{ computed: number }> {
  const res = await fetch(`/api/matches/refresh?top=${top}`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw new Error(`Failed to refresh matches: ${res.status}`);
  return res.json();
}

// ── AI assist ────────────────────────────────────────────────────────────────

export async function generateCoverLetter(jobId: number): Promise<{ cover_letter: string }> {
  const r = await fetch(`/api/ai/cover_letter/${jobId}`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  if (!r.ok) throw new Error((await r.json())?.detail || `Failed: ${r.status}`);
  return r.json();
}

export async function tailorResumeForJob(jobId: number): Promise<{ tailored_resume: string }> {
  const r = await fetch(`/api/ai/tailor_resume/${jobId}`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  if (!r.ok) throw new Error((await r.json())?.detail || `Failed: ${r.status}`);
  return r.json();
}

export async function autoApply(jobIds: number[], dryRun = true): Promise<{ queued: number }> {
  const r = await fetch("/api/ai/auto_apply", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ job_ids: jobIds, dry_run: dryRun }),
  });
  if (!r.ok) throw new Error((await r.json())?.detail || `Failed: ${r.status}`);
  return r.json();
}
