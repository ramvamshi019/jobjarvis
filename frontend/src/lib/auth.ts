/**
 * Auth utilities — JWT token management + API calls.
 * Token is stored in localStorage under "jj_token".
 */

const TOKEN_KEY = "jj_token";
const USER_KEY  = "jj_user";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface AuthUser {
  user_id: number;
  email: string;
  full_name?: string;
  role: string;
  access_token: string;
}

export interface SignupPayload {
  email: string;
  password: string;
  full_name?: string;
}

export interface LoginPayload {
  email: string;
  password: string;
}

// ── Token helpers ─────────────────────────────────────────────────────────────

export function saveAuth(user: AuthUser): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, user.access_token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function loadAuth(): AuthUser | null {
  if (typeof window === "undefined") return null;
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

/** Attach Authorization header if token is present. */
export function authHeaders(): HeadersInit {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// ── API calls ─────────────────────────────────────────────────────────────────

async function _post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.detail ?? `Request failed: ${res.status}`);
  }
  return data as T;
}

interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: number;
  email: string;
  role: string;
}

export async function apiSignup(payload: SignupPayload): Promise<AuthUser> {
  const data = await _post<TokenResponse>("/auth/signup", payload);
  return {
    user_id:      data.user_id,
    email:        data.email,
    full_name:    payload.full_name,
    role:         data.role,
    access_token: data.access_token,
  };
}

export async function apiLogin(payload: LoginPayload): Promise<AuthUser> {
  const data = await _post<TokenResponse>("/auth/login", payload);
  return {
    user_id:      data.user_id,
    email:        data.email,
    role:         data.role,
    access_token: data.access_token,
  };
}

export async function apiMe(): Promise<{ email: string; full_name?: string; role: string } | null> {
  const token = getToken();
  if (!token) return null;
  try {
    const res = await fetch("/api/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}
