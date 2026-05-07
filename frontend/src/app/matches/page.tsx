"use client";
/**
 * /matches — personalized job matches.
 *
 * Flow:
 *   1. If user not signed in → redirect to /login.
 *   2. If no active resume → show upload zone.
 *   3. After upload (or on subsequent visits with active resume) → show top
 *      50 matches with composite-score badge + apply link.
 *
 * Uses the patched POST /api/resumes/upload which embeds the resume and
 * computes matches synchronously, so the page can immediately show results.
 */
import { useEffect, useState, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import {
  uploadResume,
  listResumes,
  getMatches,
  refreshMatches,
  type MatchRow,
  type ResumeRow,
} from "@/lib/matches";

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtSalary(lo: number | null, hi: number | null): string {
  if (!lo && !hi) return "—";
  if (lo && hi) return `$${Math.round(lo / 1000)}k–$${Math.round(hi / 1000)}k`;
  if (lo) return `$${Math.round(lo / 1000)}k+`;
  return `up to $${Math.round((hi as number) / 1000)}k`;
}

function fmtAge(posted: string | null, firstSeen: string | null): string {
  const ts = posted || firstSeen;
  if (!ts) return "";
  const d = Math.floor((Date.now() - new Date(ts).getTime()) / 86_400_000);
  if (d <= 0) return "today";
  if (d === 1) return "1d ago";
  if (d < 30) return `${d}d ago`;
  return `${Math.floor(d / 30)}mo ago`;
}

function scoreColor(score: number): string {
  if (score >= 0.7) return "bg-emerald-100 text-emerald-700";
  if (score >= 0.55) return "bg-blue-100 text-blue-700";
  if (score >= 0.4) return "bg-amber-100 text-amber-700";
  return "bg-gray-100 text-gray-600";
}

// ── Upload zone ──────────────────────────────────────────────────────────────

function UploadZone({
  onUploaded,
}: {
  onUploaded: (matchesComputed: number) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [targetRole, setTargetRole] = useState("");
  const [drag, setDrag] = useState(false);

  const handleFile = useCallback(async (file: File) => {
    setErr(null);
    setBusy(true);
    try {
      const res = await uploadResume(file, targetRole || undefined);
      onUploaded(res.matches_computed);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [targetRole, onUploaded]);

  return (
    <div className="max-w-2xl mx-auto py-12 px-4">
      <h1 className="text-3xl font-bold text-gray-900 mb-2">
        Upload your resume
      </h1>
      <p className="text-gray-600 mb-8">
        We'll match it against 48,000+ jobs from real companies — no LinkedIn,
        no Indeed, just direct from each company's hiring page. Your top 50
        matches show up here right after you upload.
      </p>

      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">
            Target role (optional)
          </label>
          <input
            type="text"
            value={targetRole}
            onChange={(e) => setTargetRole(e.target.value)}
            placeholder="e.g. Software Engineer, Data Engineer"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg
                       focus:ring-2 focus:ring-brand focus:border-brand
                       text-sm"
            disabled={busy}
          />
        </div>

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files?.[0];
            if (f) handleFile(f);
          }}
          onClick={() => !busy && inputRef.current?.click()}
          className={`border-2 border-dashed rounded-2xl p-12 text-center
                      cursor-pointer transition-colors
                      ${drag ? "border-brand bg-blue-50" : "border-gray-300 hover:border-brand hover:bg-blue-50/50"}
                      ${busy ? "opacity-60 pointer-events-none" : ""}`}
        >
          <input
            ref={inputRef}
            type="file"
            accept=".pdf,.docx,.doc,.txt,.md"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleFile(f);
            }}
          />
          {busy ? (
            <div className="space-y-3">
              <div className="text-3xl">⚙️</div>
              <p className="font-semibold text-gray-700">
                Parsing → embedding → matching against 48k jobs…
              </p>
              <p className="text-xs text-gray-500">
                this takes ~5–15 seconds the first time
              </p>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-4xl">📄</div>
              <p className="font-semibold text-gray-700">
                Drop your resume here, or click to browse
              </p>
              <p className="text-xs text-gray-500">PDF, DOCX, or TXT</p>
            </div>
          )}
        </div>

        {err && (
          <div className="p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
            {err}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Match card ───────────────────────────────────────────────────────────────

function MatchCard({ m, rank }: { m: MatchRow; rank: number }) {
  const pct = Math.round(m.match_score * 100);
  const sim = m.sim_score !== null ? Math.round(m.sim_score * 100) : null;
  const apply = m.apply_url || m.url || "#";

  return (
    <a
      href={apply}
      target="_blank"
      rel="noopener noreferrer"
      className="block bg-white rounded-xl border border-gray-200 p-4
                 hover:shadow-md hover:border-brand transition-all"
    >
      <div className="flex items-start gap-4">
        <div className="text-sm font-bold text-gray-300 w-6 shrink-0 pt-0.5">
          {rank}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-base font-semibold text-gray-900 truncate">
                {m.title}
              </p>
              <p className="text-sm text-brand font-medium">{m.company}</p>
            </div>
            <div className={`px-2 py-1 rounded-full text-xs font-semibold whitespace-nowrap ${scoreColor(m.match_score)}`}>
              {pct}% match
            </div>
          </div>
          <div className="text-xs text-gray-500 mt-2 flex items-center gap-2 flex-wrap">
            {m.location && <span>📍 {m.location}</span>}
            {m.remote_type && (
              <span className="px-1.5 py-0.5 bg-gray-100 rounded">
                {m.remote_type}
              </span>
            )}
            <span>💰 {fmtSalary(m.salary_min, m.salary_max)}</span>
            <span>🕐 {fmtAge(m.posted_at, m.first_seen_at)}</span>
            {m.source_ats && (
              <span className="px-1.5 py-0.5 bg-blue-50 text-blue-700 rounded">
                {m.source_ats}
              </span>
            )}
            {sim !== null && (
              <span className="text-gray-400">
                · sim {sim}%
              </span>
            )}
          </div>
        </div>
      </div>
    </a>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function MatchesPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();

  const [resumes, setResumes] = useState<ResumeRow[]>([]);
  const [matches, setMatches] = useState<MatchRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Redirect to login if not authed
  useEffect(() => {
    if (!authLoading && !user) router.push("/login?next=/matches");
  }, [user, authLoading, router]);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setErr(null);
    try {
      await refreshMatches(50);
      const m = await getMatches(50);
      setMatches(m.matches);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }, []);

  // Load resumes + matches on mount
  useEffect(() => {
    if (!user) return;
    (async () => {
      setLoading(true);
      try {
        const [rs, m] = await Promise.all([listResumes(), getMatches(50)]);
        setResumes(rs);
        setMatches(m.matches);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, [user]);

  const activeResume = resumes.find((r) => r.is_active);

  // ── Render ────────────────────────────────────────────────────────────────
  if (authLoading || loading) {
    return (
      <div className="max-w-3xl mx-auto py-12 px-4">
        <div className="animate-pulse space-y-3">
          <div className="h-7 bg-gray-200 rounded w-48" />
          <div className="h-4 bg-gray-200 rounded w-72" />
          <div className="h-32 bg-gray-100 rounded-xl" />
          <div className="h-32 bg-gray-100 rounded-xl" />
        </div>
      </div>
    );
  }

  if (!user) return null; // redirecting

  // No resume yet → upload zone
  if (!activeResume) {
    return (
      <UploadZone
        onUploaded={async (n) => {
          // Refresh both after upload
          const [rs, m] = await Promise.all([listResumes(), getMatches(50)]);
          setResumes(rs);
          setMatches(m.matches);
        }}
      />
    );
  }

  // Have a resume → show matches
  return (
    <div className="max-w-3xl mx-auto py-8 px-4">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Your matches</h1>
          <p className="text-sm text-gray-600 mt-1">
            Ranked by AI similarity to{" "}
            <span className="font-medium">{activeResume.name}</span>
            {activeResume.target_role && (
              <> · target: {activeResume.target_role}</>
            )}
          </p>
        </div>
        <button
          onClick={refresh}
          disabled={refreshing}
          className="px-3 py-2 text-sm font-medium rounded-lg border border-gray-200
                     bg-white hover:bg-gray-50 text-gray-700 disabled:opacity-50
                     whitespace-nowrap"
        >
          {refreshing ? "Refreshing…" : "↻ Refresh"}
        </button>
      </div>

      {err && (
        <div className="p-3 mb-4 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
          {err}
        </div>
      )}

      {matches.length === 0 ? (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-center">
          <p className="font-semibold text-amber-900 mb-2">
            No matches yet
          </p>
          <p className="text-sm text-amber-800 mb-4">
            This usually means jobs haven't been embedded yet. Run the
            embedding backfill once and matches will populate immediately:
          </p>
          <code className="block bg-white text-xs text-left p-3 rounded border border-amber-200 overflow-x-auto">
            docker exec jobjarvis_celery_worker python3 -u /tmp/backfill_embeddings.py
          </code>
          <button
            onClick={refresh}
            disabled={refreshing}
            className="mt-4 px-4 py-2 text-sm font-medium rounded-lg
                       bg-brand text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {refreshing ? "Refreshing…" : "Try again"}
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {matches.map((m, i) => (
            <MatchCard key={m.job_id} m={m} rank={i + 1} />
          ))}
        </div>
      )}

      <p className="text-xs text-gray-400 mt-8 text-center">
        Showing your top {matches.length} matches.
        Composite score = 60% AI similarity + 15% salary fit + 10% location fit
        + 5% freshness.
      </p>
    </div>
  );
}
