"use client";
/**
 * /matches — personalized job matches with filters + re-upload.
 */
import { useEffect, useState, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import {
  uploadResume,
  listResumes,
  getMatches,
  refreshMatches,
  generateCoverLetter,
  tailorResumeForJob,
  autoApply,
  type MatchRow,
  type ResumeRow,
  type CountryFilter,
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
  title = "Upload your resume",
  subtitle = "We'll match it against 200,000+ jobs from real companies — no LinkedIn, no Indeed, just direct from each company's hiring page.",
  onCancel,
}: {
  onUploaded: () => void;
  title?: string;
  subtitle?: string;
  onCancel?: () => void;
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
      await uploadResume(file, targetRole || undefined);
      onUploaded();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [targetRole, onUploaded]);

  return (
    <div className="max-w-2xl mx-auto py-8 px-4">
      <div className="flex items-center justify-between mb-2">
        <h1 className="text-3xl font-bold text-gray-900">{title}</h1>
        {onCancel && (
          <button
            onClick={onCancel}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            ✕ Cancel
          </button>
        )}
      </div>
      <p className="text-gray-600 mb-6">{subtitle}</p>

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
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files?.[0];
            if (f) handleFile(f);
          }}
          onClick={() => !busy && inputRef.current?.click()}
          className={`border-2 border-dashed rounded-2xl p-10 text-center
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
                Parsing → embedding → matching…
              </p>
              <p className="text-xs text-gray-500">
                ~5–15 sec the first time
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
  const [aiOpen, setAiOpen]     = useState(false);
  const [aiBusy, setAiBusy]     = useState<string | null>(null);
  const [aiOutput, setAiOutput] = useState<{ kind: string; text: string } | null>(null);
  const [aiErr, setAiErr]       = useState<string | null>(null);

  const pct = Math.round(m.match_score * 100);
  const sim = m.sim_score !== null ? Math.round(m.sim_score * 100) : null;
  const apply = m.apply_url || m.url || "#";

  async function run(kind: "cover" | "resume" | "apply") {
    setAiBusy(kind);
    setAiErr(null);
    setAiOutput(null);
    try {
      if (kind === "cover") {
        const r = await generateCoverLetter(m.job_id);
        setAiOutput({ kind: "Cover letter", text: r.cover_letter });
      } else if (kind === "resume") {
        const r = await tailorResumeForJob(m.job_id);
        setAiOutput({ kind: "Tailored resume", text: r.tailored_resume });
      } else {
        const r = await autoApply([m.job_id], true);  // dry-run by default
        setAiOutput({
          kind: "Auto-apply queued",
          text: `Queued ${r.queued} job(s) in dry-run mode. Check /app/data/auto_apply/ for screenshots.`,
        });
      }
    } catch (e) {
      setAiErr(e instanceof Error ? e.message : String(e));
    } finally {
      setAiBusy(null);
    }
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 hover:shadow-md hover:border-brand transition-all">
      <div className="flex items-start gap-4 p-4">
        <div className="text-sm font-bold text-gray-300 w-6 shrink-0 pt-0.5">{rank}</div>
        <div className="flex-1 min-w-0">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <a href={apply} target="_blank" rel="noopener noreferrer"
                 className="text-base font-semibold text-gray-900 truncate block hover:underline">
                {m.title}
              </a>
              <p className="text-sm text-brand font-medium">{m.company}</p>
            </div>
            <div className={`px-2 py-1 rounded-full text-xs font-semibold whitespace-nowrap ${scoreColor(m.match_score)}`}>
              {pct}% match
            </div>
          </div>
          <div className="text-xs text-gray-500 mt-2 flex items-center gap-2 flex-wrap">
            {m.location && <span>📍 {m.location}</span>}
            {m.remote_type && <span className="px-1.5 py-0.5 bg-gray-100 rounded">{m.remote_type}</span>}
            <span>💰 {fmtSalary(m.salary_min, m.salary_max)}</span>
            <span>🕐 {fmtAge(m.posted_at, m.first_seen_at)}</span>
            {m.source_ats && <span className="px-1.5 py-0.5 bg-blue-50 text-blue-700 rounded">{m.source_ats}</span>}
            {sim !== null && <span className="text-gray-400">· sim {sim}%</span>}
          </div>

          {/* AI action row */}
          <div className="mt-3 flex flex-wrap gap-2">
            <a href={apply} target="_blank" rel="noopener noreferrer"
               className="px-2.5 py-1 text-xs font-medium rounded-md bg-brand text-white hover:bg-blue-700">
              Apply →
            </a>
            <button
              onClick={() => run("cover")}
              disabled={!!aiBusy}
              className="px-2.5 py-1 text-xs font-medium rounded-md bg-purple-50 text-purple-700 hover:bg-purple-100 disabled:opacity-50">
              {aiBusy === "cover" ? "Writing…" : "✦ Cover letter"}
            </button>
            <button
              onClick={() => run("resume")}
              disabled={!!aiBusy}
              className="px-2.5 py-1 text-xs font-medium rounded-md bg-emerald-50 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50">
              {aiBusy === "resume" ? "Tailoring…" : "✦ Tailor resume"}
            </button>
            <button
              onClick={() => run("apply")}
              disabled={!!aiBusy}
              className="px-2.5 py-1 text-xs font-medium rounded-md bg-amber-50 text-amber-700 hover:bg-amber-100 disabled:opacity-50">
              {aiBusy === "apply" ? "Queuing…" : "🤖 Auto-apply (dry-run)"}
            </button>
          </div>

          {/* AI output panel */}
          {(aiOutput || aiErr) && (
            <div className="mt-3 border border-gray-200 rounded-lg p-3 bg-gray-50">
              {aiErr && (
                <div className="text-xs text-red-700">⚠ {aiErr}</div>
              )}
              {aiOutput && (
                <>
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-semibold text-gray-700">{aiOutput.kind}</span>
                    <button
                      onClick={() => navigator.clipboard.writeText(aiOutput.text)}
                      className="text-xs text-brand hover:underline">
                      Copy
                    </button>
                  </div>
                  <pre className="text-xs text-gray-800 whitespace-pre-wrap font-sans max-h-96 overflow-y-auto">
                    {aiOutput.text}
                  </pre>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

const RECENCY_OPTIONS = [
  { value: null,  label: "Any time" },
  { value: 1,     label: "Last 24h" },
  { value: 7,     label: "Last 7 days" },
  { value: 30,    label: "Last 30 days" },
];

const COUNTRY_OPTIONS: { value: CountryFilter; label: string }[] = [
  { value: "us",     label: "🇺🇸 US + Remote" },
  { value: "remote", label: "🌐 Remote-only" },
  { value: "all",    label: "🌍 Worldwide" },
];

export default function MatchesPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();

  const [resumes, setResumes] = useState<ResumeRow[]>([]);
  const [matches, setMatches] = useState<MatchRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  // Filter state — persisted to localStorage so they survive reloads
  const [country, setCountry] = useState<CountryFilter>("us");
  const [recencyDays, setRecencyDays] = useState<number | null>(null);

  // Restore filter prefs on mount
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      const saved = localStorage.getItem("jj_match_filters");
      if (saved) {
        const f = JSON.parse(saved);
        if (f.country) setCountry(f.country);
        if (f.recencyDays !== undefined) setRecencyDays(f.recencyDays);
      }
    } catch {}
  }, []);

  // Persist on change
  useEffect(() => {
    if (typeof window === "undefined") return;
    localStorage.setItem("jj_match_filters",
      JSON.stringify({ country, recencyDays }));
  }, [country, recencyDays]);

  useEffect(() => {
    if (!authLoading && !user) router.push("/login?next=/matches");
  }, [user, authLoading, router]);

  const fetchMatches = useCallback(async () => {
    setErr(null);
    try {
      const m = await getMatches({ limit: 50, country, recencyDays });
      setMatches(m.matches);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [country, recencyDays]);

  const recompute = useCallback(async () => {
    setRefreshing(true);
    setErr(null);
    try {
      await refreshMatches(50);
      await fetchMatches();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setRefreshing(false);
    }
  }, [fetchMatches]);

  // Initial load
  useEffect(() => {
    if (!user) return;
    (async () => {
      setLoading(true);
      try {
        const [rs, m] = await Promise.all([
          listResumes(),
          getMatches({ limit: 50, country, recencyDays }),
        ]);
        setResumes(rs);
        setMatches(m.matches);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);

  // Re-fetch matches when filters change (no recompute, just re-query)
  useEffect(() => {
    if (!user || loading) return;
    fetchMatches();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [country, recencyDays]);

  const activeResume = resumes.find((r) => r.is_active);

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
  if (!user) return null;

  // No resume → upload zone
  if (!activeResume && !showUpload) {
    return (
      <UploadZone onUploaded={async () => {
        const [rs, m] = await Promise.all([
          listResumes(),
          getMatches({ limit: 50, country, recencyDays }),
        ]);
        setResumes(rs);
        setMatches(m.matches);
      }} />
    );
  }

  // Show re-upload zone
  if (showUpload) {
    return (
      <UploadZone
        title="Update your resume"
        subtitle="Drop a new version below — we'll re-embed and re-match against the latest jobs."
        onCancel={() => setShowUpload(false)}
        onUploaded={async () => {
          const [rs, m] = await Promise.all([
            listResumes(),
            getMatches({ limit: 50, country, recencyDays }),
          ]);
          setResumes(rs);
          setMatches(m.matches);
          setShowUpload(false);
        }}
      />
    );
  }

  return (
    <div className="max-w-3xl mx-auto py-6 px-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Your matches</h1>
          <p className="text-sm text-gray-600 mt-1">
            Ranked by AI similarity to{" "}
            <span className="font-medium">{activeResume!.name}</span>
            {activeResume!.target_role && <> · target: {activeResume!.target_role}</>}
          </p>
        </div>
        <div className="flex gap-2 shrink-0">
          <button
            onClick={() => setShowUpload(true)}
            className="px-3 py-2 text-sm font-medium rounded-lg border border-gray-200
                       bg-white hover:bg-gray-50 text-gray-700"
          >
            ↻ Change resume
          </button>
          <button
            onClick={recompute}
            disabled={refreshing}
            className="px-3 py-2 text-sm font-medium rounded-lg
                       bg-brand text-white hover:bg-blue-700 disabled:opacity-50
                       whitespace-nowrap"
          >
            {refreshing ? "Refreshing…" : "Recompute"}
          </button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="bg-white border border-gray-200 rounded-xl p-3 mb-4
                      flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-500 uppercase">
            Location
          </label>
          <select
            value={country}
            onChange={(e) => setCountry(e.target.value as CountryFilter)}
            className="px-2 py-1 text-sm border border-gray-200 rounded-lg bg-white"
          >
            {COUNTRY_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs font-medium text-gray-500 uppercase">
            Posted
          </label>
          <select
            value={recencyDays === null ? "any" : String(recencyDays)}
            onChange={(e) => {
              const v = e.target.value;
              setRecencyDays(v === "any" ? null : Number(v));
            }}
            className="px-2 py-1 text-sm border border-gray-200 rounded-lg bg-white"
          >
            {RECENCY_OPTIONS.map((o) => (
              <option key={o.label} value={o.value === null ? "any" : o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="text-xs text-gray-500 ml-auto">
          {matches.length} of top 50 shown
        </div>
      </div>

      {err && (
        <div className="p-3 mb-4 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
          {err}
        </div>
      )}

      {matches.length === 0 ? (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-center">
          <p className="font-semibold text-amber-900 mb-2">
            No matches with the current filters
          </p>
          <p className="text-sm text-amber-800 mb-4">
            Try widening location to <em>Worldwide</em> or posted-time to
            <em> Any time</em>, or click <strong>Recompute</strong>.
          </p>
          <button
            onClick={recompute}
            disabled={refreshing}
            className="px-4 py-2 text-sm font-medium rounded-lg
                       bg-brand text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {refreshing ? "Recomputing…" : "Recompute matches"}
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
        Showing top {matches.length} matches. Composite score = 60% AI
        similarity + 15% salary fit + 10% location fit + 5% freshness.
      </p>
    </div>
  );
}
