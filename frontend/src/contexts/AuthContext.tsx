"use client";
/**
 * AuthContext — global auth state for the whole app.
 *
 * Wraps the app in AuthProvider (added to layout.tsx).
 * Components consume it via useAuth().
 */
import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from "react";
import {
  type AuthUser,
  type LoginPayload,
  type SignupPayload,
  apiLogin,
  apiSignup,
  clearAuth,
  loadAuth,
  saveAuth,
} from "@/lib/auth";

// ── Context shape ─────────────────────────────────────────────────────────────

interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  login: (payload: LoginPayload) => Promise<void>;
  signup: (payload: SignupPayload) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// ── Provider ──────────────────────────────────────────────────────────────────

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser]       = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  // Rehydrate from localStorage on mount (runs client-side only)
  useEffect(() => {
    const stored = loadAuth();
    if (stored) setUser(stored);
    setLoading(false);
  }, []);

  const login = useCallback(async (payload: LoginPayload) => {
    const authUser = await apiLogin(payload);
    saveAuth(authUser);
    setUser(authUser);
  }, []);

  const signup = useCallback(async (payload: SignupPayload) => {
    const authUser = await apiSignup(payload);
    saveAuth(authUser);
    setUser(authUser);
  }, []);

  const logout = useCallback(() => {
    clearAuth();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, signup, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
