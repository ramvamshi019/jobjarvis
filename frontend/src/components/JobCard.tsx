"use client";
import type { Job } from "@/types";

interface Props {
  job: Job;
  active: boolean;
  onClick: () => void;
  highlight?: string;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  if (h < 1) return "Just now";
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return `${Math.floor(d / 7)}w ago`;
}

/** Wrap the first occurrence of keyword in <mark>. */
function Highlight({ text, kw }: { text: string; kw?: string }) {
  if (!kw || !kw.trim()) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(kw.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-yellow-100 text-yellow-900 rounded-sm">{text.slice(idx, idx + kw.length)}</mark>
      {text.slice(idx + kw.length)}
    </>
  );
}

function RemoteBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const cls =
    type === "remote"
      ? "bg-green-50 text-green-700 border-green-200"
      : type === "hybrid"
      ? "bg-amber-50 text-amber-700 border-amber-200"
      : "bg-gray-50 text-gray-600 border-gray-200";
  return (
    <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium border ${cls}`}>
      {type.charAt(0).toUpperCase() + type.slice(1)}
    </span>
  );
}

function SalaryText({ job }: { job: Job }) {
  if (!job.salary_min && !job.salary_max) return null;
  const cur = job.salary_currency ?? "USD";
  const fmt = (n: number) =>
    new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: cur,
      maximumFractionDigits: 0,
    }).format(n);
  if (job.salary_min && job.salary_max)
    return <>{fmt(job.salary_min)} – {fmt(job.salary_max)}</>;
  if (job.salary_max) return <>Up to {fmt(job.salary_max)}</>;
  return <>From {fmt(job.salary_min!)}</>;
}

function isNew(job: Job): boolean {
  return (
    job.freshness_label === "last_24h" ||
    (typeof job.freshness_score === "number" && job.freshness_score >= 0.8)
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function JobCard({ job, active, onClick, highlight }: Props) {
  const location =
    [job.city, job.country].filter(Boolean).join(", ") ||
    job.location ||
    null;

  // ATS feeds (Greenhouse, Lever, ...) sometimes return an ancient posted_at
  // for re-listed roles — e.g. Palantir reposted in 2026 with posted_at=2019.
  // The "X ago" label should never claim a job is older than we discovered
  // it, so pick the most recent of the two timestamps.
  const _postedMs    = job.posted_at      ? new Date(job.posted_at).getTime()      : 0;
  const _firstSeenMs = job.first_seen_at  ? new Date(job.first_seen_at).getTime()  : 0;
  const _effectiveTs = Math.max(_postedMs, _firstSeenMs);
  const postedLabel  = _effectiveTs
    ? timeAgo(new Date(_effectiveTs).toISOString())
    : "—";
  const _isNew = isNew(job);

  return (
    <article
      onClick={onClick}
      className={`group cursor-pointer rounded-xl border p-4 transition-all
                  hover:shadow-md hover:border-gray-300 ${
                    active
                      ? "border-brand bg-blue-50/60 shadow-sm"
                      : "border-gray-200 bg-white"
                  }`}
    >
      {/* ── Header ── */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-gray-900 leading-snug line-clamp-2">
            <Highlight text={job.title} kw={highlight} />
          </h2>
          <p className="text-xs text-brand font-medium mt-0.5 truncate">
            <Highlight text={job.company_name} kw={highlight} />
          </p>
        </div>

        {/* Badges + time */}
        <div className="flex flex-col items-end gap-1 shrink-0">
          {_isNew && (
            <span className="px-2 py-0.5 rounded-full text-[11px] font-semibold
                             bg-orange-50 text-orange-600 border border-orange-200">
              🔥 New
            </span>
          )}
          {postedLabel && (
            <time className="text-[11px] text-gray-400 whitespace-nowrap">
              {postedLabel}
            </time>
          )}
        </div>
      </div>

      {/* ── Meta row ── */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-gray-500">
        {location && <span>📍 {location}</span>}
        {job.experience_level && (
          <span className="capitalize">{job.experience_level}</span>
        )}
        {(job.salary_min || job.salary_max) && (
          <span className="text-green-700 font-medium">
            <SalaryText job={job} />
          </span>
        )}
      </div>

      {/* ── Chips ── */}
      <div className="mt-2.5 flex flex-wrap gap-1.5">
        <RemoteBadge type={job.remote_type} />
        {job.role_category && (
          <span className="px-2 py-0.5 rounded-full text-[11px] font-medium
                           bg-brand/10 text-brand border border-brand/20">
            {job.role_category}
          </span>
        )}
      </div>

      {/* ── Skills preview ── */}
      {job.required_skills && job.required_skills.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {job.required_skills.slice(0, 6).map((s) => (
            <span
              key={s}
              className="px-1.5 py-0.5 text-[10px] bg-gray-100 text-gray-600 rounded"
            >
              {s}
            </span>
          ))}
          {job.required_skills.length > 6 && (
            <span className="text-[10px] text-gray-400">
              +{job.required_skills.length - 6}
            </span>
          )}
        </div>
      )}
    </article>
  );
}
