"use client";
import { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";

export default function SignupPage() {
  const { signup, user } = useAuth();
  const router = useRouter();

  const [fullName, setFullName] = useState("");
  const [email,    setEmail]    = useState("");
  const [password, setPassword] = useState("");
  const [confirm,  setConfirm]  = useState("");
  const [error,    setError]    = useState("");
  const [loading,  setLoading]  = useState(false);

  useEffect(() => {
    if (user) router.replace("/dashboard");
  }, [user, router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");

    if (password.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }

    setLoading(true);
    try {
      await signup({
        email:     email.trim(),
        password,
        full_name: fullName.trim() || undefined,
      });
      router.push("/dashboard");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Signup failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="w-full max-w-sm">

        <div className="bg-white rounded-2xl border border-gray-200 shadow-sm px-8 py-10">

          <div className="text-center mb-8">
            <Link href="/" className="text-2xl font-extrabold text-brand tracking-tight">
              JobJarvis
            </Link>
            <p className="text-gray-500 text-sm mt-2">Create your free account</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Full name <span className="text-gray-400">(optional)</span>
              </label>
              <input
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                autoComplete="name"
                placeholder="Your name"
                className="w-full px-4 py-2.5 rounded-xl border border-gray-200 text-sm
                           focus:outline-none focus:ring-2 focus:ring-brand bg-white"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Email
              </label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
                autoComplete="email"
                placeholder="you@example.com"
                className="w-full px-4 py-2.5 rounded-xl border border-gray-200 text-sm
                           focus:outline-none focus:ring-2 focus:ring-brand bg-white"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Password
              </label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                autoComplete="new-password"
                placeholder="Min. 8 characters"
                className="w-full px-4 py-2.5 rounded-xl border border-gray-200 text-sm
                           focus:outline-none focus:ring-2 focus:ring-brand bg-white"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1.5">
                Confirm password
              </label>
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                required
                autoComplete="new-password"
                placeholder="Repeat password"
                className={`w-full px-4 py-2.5 rounded-xl border text-sm
                           focus:outline-none focus:ring-2 focus:ring-brand bg-white
                           ${confirm && confirm !== password
                             ? "border-red-300 focus:ring-red-400"
                             : "border-gray-200"}`}
              />
            </div>

            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm
                              rounded-xl px-4 py-3">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 bg-brand text-white font-semibold rounded-xl
                         hover:bg-blue-700 active:bg-blue-800 transition-colors
                         disabled:opacity-60 disabled:cursor-not-allowed mt-2"
            >
              {loading ? "Creating account…" : "Create account"}
            </button>
          </form>

          <p className="text-center text-sm text-gray-500 mt-6">
            Already have an account?{" "}
            <Link href="/login" className="text-brand font-medium hover:underline">
              Sign in
            </Link>
          </p>
        </div>

        <p className="text-center text-xs text-gray-400 mt-4">
          <Link href="/" className="hover:text-brand">← Back to job search</Link>
        </p>
      </div>
    </div>
  );
}
