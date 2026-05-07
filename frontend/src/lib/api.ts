import type { QueryState, SearchResponse, Stats, Job } from "@/types";

// In dev: Next.js rewrites /api/* → http://localhost:8000/api/*
// In prod: set NEXT_PUBLIC_API_BASE env var or rely on same-origin
const BASE = "/api/jobs";

function buildQS(params: Record<string, unknown>): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      qs.set(k, String(v));
    }
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export async function searchJobs(
  query: QueryState,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const res = await fetch(
    `${BASE}/search${buildQS(query as unknown as Record<string, unknown>)}`,
    { signal },
  );
  if (!res.ok) throw new Error(`Search failed: ${res.status}`);
  return res.json();
}

export async function getJob(id: number, signal?: AbortSignal): Promise<Job> {
  const res = await fetch(`${BASE}/search/${id}`, { signal });
  if (!res.ok) throw new Error(`Job not found: ${res.status}`);
  return res.json();
}

export async function getStats(): Promise<Stats> {
  try {
    const res = await fetch(`${BASE}/search/stats/summary`);
    if (!res.ok) return fallbackStats();
    return res.json();
  } catch {
    return fallbackStats();
  }
}

function fallbackStats(): Stats {
  return { total_jobs: 0, last_24h: 0, as_of: new Date().toISOString() };
}
