// ── Job ───────────────────────────────────────────────────────────────────────
export interface Job {
  id: number;
  title: string;
  company_name: string;
  location: string | null;
  remote_type: "remote" | "hybrid" | "onsite" | null;
  experience_level: string | null;
  employment_type: string | null;
  role_category: string | null;
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  job_url: string | null;
  posted_at: string | null;
  first_seen_at: string;
  freshness_label: string | null;
  freshness_score: number | null;
  source: string | null;
  required_skills: string[] | null;
  country: string | null;
  city: string | null;
}

// ── API responses ─────────────────────────────────────────────────────────────
export interface SearchResponse {
  jobs: Job[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface Stats {
  total_jobs: number;
  last_24h: number;
  as_of: string;
}

// ── Filter state (no page — page is managed by the hook) ─────────────────────
export interface JobFilters {
  q?: string;
  location?: string;
  experience?: string;
  remote?: string;
  role?: string;
  freshness?: string;
  country?: string;
}

// ── Internal hook query state (filters + pagination) ─────────────────────────
export interface QueryState extends JobFilters {
  page: number;
  page_size: number;
}
