/**
 * Central API client.
 *
 * WHY 127.0.0.1 not localhost:
 *   On macOS, `localhost` often resolves to ::1 (IPv6) while uvicorn
 *   binds to 127.0.0.1 (IPv4). That mismatch causes ECONNREFUSED → "Failed to fetch".
 *   Always use 127.0.0.1 explicitly.
 *
 * WHY .env.local not next.config.js:
 *   The `env:` block in next.config.js is evaluated once at build/dev-server startup
 *   and bakes the value into the bundle. That means even if you later add .env.local,
 *   next.config.js wins and overrides it. .env.local is the canonical Next.js mechanism
 *   for per-machine env variables — no conflict, no surprise.
 */
export const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000"

console.log("BASE_URL:", process.env.NEXT_PUBLIC_API_URL)

// ── Token helpers ─────────────────────────────────────────────────────────────

export function getToken(): string | null {
  if (typeof window === 'undefined') return null
  return localStorage.getItem('jj_token')
}

export function setToken(token: string): void {
  localStorage.setItem('jj_token', token)
}

export function clearToken(): void {
  localStorage.removeItem('jj_token')
  localStorage.removeItem('jj_user')
}

// ── Core request handler ──────────────────────────────────────────────────────

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }
  if (token) headers['Authorization'] = `Bearer ${token}`

  console.log("Requesting:", `${BASE_URL}/api${path}`)

  let res: Response
  try {
    res = await fetch(`${BASE_URL}/api${path}`, { ...options, headers })
  } catch (err) {
    // Network-level failure (ECONNREFUSED, DNS failure, offline, etc.)
    throw new Error('Backend not reachable')
  }

  if (!res.ok) {
    // Parse FastAPI's { detail: "..." } error shape; fall back to status text
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(
      typeof body.detail === 'string'
        ? body.detail
        : JSON.stringify(body.detail) // FastAPI validation errors are arrays
    )
  }

  if (res.status === 204) return {} as T
  return res.json()
}

// ── Public API surface ────────────────────────────────────────────────────────

export const api = {
  get:    <T>(path: string)                  => request<T>(path),
  post:   <T>(path: string, body?: unknown)  => request<T>(path, { method: 'POST',   body: JSON.stringify(body) }),
  patch:  <T>(path: string, body?: unknown)  => request<T>(path, { method: 'PATCH',  body: JSON.stringify(body) }),
  delete: <T>(path: string)                  => request<T>(path, { method: 'DELETE' }),
}

// ── Named auth helpers (used by auth page) ────────────────────────────────────

export async function signup(email: string, password: string, fullName?: string): Promise<TokenResponse> {
  return api.post<TokenResponse>('/auth/signup', { email, password, full_name: fullName || undefined })
}

export async function login(email: string, password: string): Promise<TokenResponse> {
  return api.post<TokenResponse>('/auth/login', { email, password })
}

export async function getCurrentUser(): Promise<UserProfile> {
  return api.get<UserProfile>('/auth/me')
}

// ── Type definitions ──────────────────────────────────────────────────────────

export interface TokenResponse {
  access_token: string
  token_type: string
  user_id: number
  email: string
  role: string
}

export interface UserProfile {
  id: number
  email: string
  full_name?: string
  role: string
  is_active: boolean
  target_roles?: string[]
  open_to_remote: boolean
  work_authorization?: string
}

export interface Job {
  id: number
  title: string
  company_name: string
  normalized_location: string
  country: string
  remote_type: string
  role_category: string
  experience_level: string
  salary_min?: number
  salary_max?: number
  salary_currency?: string
  required_skills?: string[]
  preferred_skills?: string[]
  spam_score: number
  eligibility_risk_score: number
  source_type?: string
  freshness_label?: string
  first_seen_at: string
  job_url?: string
  active: boolean
  decision?: string
  fit_score?: number
}

export interface AIDecision {
  id: number
  job_id: number
  decision: string
  fit_score: number
  priority: string
  confidence: number
  role_category: string
  matched_skills: string[]
  missing_skills: string[]
  risk_flags: string[]
  why_apply: string[]
  why_not: string[]
  application_strategy: string
  apply_within_hours: number
  recommended_resume: string
  resume_suggestions: string[]
  needs_human_review: boolean
  interview_probability: number
  created_at: string
}

export interface Application {
  id: number
  job_id: number
  status: string
  applied_at?: string
  follow_up_at?: string   // was missing — tracker page uses this
  recruiter_name?: string
  recruiter_email?: string
  notes?: string
  outcome?: string
  interview_rounds: number
  created_at: string
}

export interface WeeklyPlan {
  week_start: string
  weekly_goal: string
  priority_roles: string[]
  target_companies?: string[]
  skills_to_improve: string[]
  resume_actions: string[]
  application_targets: Array<{
    job_id: number
    title: string
    company: string
    fit_score: number
    apply_within_hours: number
  }>
  project_recommendations: string[]
  metrics: { applications_this_week: number; top_decisions_count: number }
}
