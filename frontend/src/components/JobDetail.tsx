"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import type { Job } from "@/types";
import { getJob } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { createApplication } from "@/lib/applications";

interface Props {
  jobId: number;
  onClose: () => void;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-gray-100 pt-4 mt-4">
      <h3 className="text-[11px] font-semibold text-gray-400 uppercase tracking-widest mb-3">
        {title}
      </h3>
      {children}
    </div>
  );
}

function FactRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <>
      <dt className="text-gray-500 text-sm">{label}</dt>
      <dd className="font-medium text-sm capitalize">{value}</dd>
    </>
  );
}

function Skeleton() {
  return (
    <div className="p-5 space-y-4 animate-pulse">
      {/* Title area */}
      <div className="space-y-2">
        <div className="h-6 bg-gray-200 rounded w-4/5" />
        <div className="h-4 bg-gray-200 rounded w-1/2" />
        <div className="h-3 bg-gray-200 rounded w-1/3" />
      </div>
      {/* Facts */}
      <div className="border-t border-gray-100 pt-4 grid grid-cols-2 gap-3">
        {[70, 40, 60, 50, 80, 45].map((w, i) => (
          <div key={i} className="h-3 bg-gray-200 rounded" style={{ width: `${w}%` }} />
        ))}
      </div>
      {/* Skills */}
      <div className="border-t border-gray-100 pt-4 flex flex-wrap gap-2">
        {[60, 80, 50, 70, 55].map((w, i) => (
          <div key={i} className="h-6 bg-gray-200 rounded-full" style={{ width: `${w}px` }} />
        ))}
      </div>
      {/* Apply button */}
      <div className="border-t border-gray-100 pt-4">
        <div className="h-11 bg-gray-200 rounded-xl" />
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function JobDetail({ jobId, onClose }: Props) {
  const { user } = useAuth();
  const [job, setJob]           = useState<Job | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState(false);
  const [saved, setSaved]       = useState(false);
  const [saving, setSaving]     = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(false);
    setJob(null);

    const ctrl = new AbortController();
    getJob(jobId, ctrl.signal)
      .then((j) => { if (alive) { setJob(j); setLoading(false); } })
      .catch((err) => {
        if (!alive || err?.name === "AbortError") return;
        setError(true);
        setLoading(false);
      });

    return () => { alive = false; ctrl.abort(); };
  }, [jobId]);

  async function handleSave() {
    if (!user || saving || saved) return;
    setSaving(true);
    try {
      await createApplication(jobId, "saved");
      setSaved(true);
    } catch {
      /* ignore duplicate saves */
      setSaved(true);
    } finally {
      setSaving(false);
    }
  }

  const location =
    [job?.city, job?.country].filter(Boolean).join(", ") ||
    job?.location ||
    "Location not specified";

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* ── Header bar ── */}
      <div className="flex items-center justify-between px-5 py-3 border-b border-gray-100 shrink-0">
        <span className="text-sm font-semibold text-gray-700">Job details</span>
        <button
          onClick={onClose}
          aria-label="Close job detail"
          className="w-7 h-7 flex items-center justify-center rounded-full
                     text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
        >
          ✕
        </button>
      </div>

      {/* ── Body ── */}
      <div className="flex-1 overflow-y-auto">
        {loading && <Skeleton />}

        {error && (
          <div className="p-5 text-sm text-gray-500">
            Could not load job details.
          </div>
        )}

        {!loading && !error && job && (
          <div className="px-5 pb-8 pt-5">
            {/* Title block */}
            <h1 className="text-xl font-bold text-gray-900 leading-snug">{job.title}</h1>
            <p className="text-brand font-semibold mt-1">{job.company_name}</p>
            <p className="text-sm text-gray-500 mt-0.5">📍 {location}</p>

            {/* Freshness / new badge */}
            {(job.freshness_label === "last_24h" ||
              (typeof job.freshness_score === "number" && job.freshness_score >= 0.8)) && (
              <span className="inline-block mt-2 px-2.5 py-0.5 rounded-full text-xs font-semibold
                               bg-orange-50 text-orange-600 border border-orange-200">
                🔥 Posted recently
              </span>
            )}

            {/* Quick facts */}
            <Section title="Quick facts">
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2.5">
                {job.remote_type && (
                  <FactRow label="Work type" value={job.remote_type} />
                )}
                {job.experience_level && (
                  <FactRow label="Experience" value={job.experience_level} />
                )}
                {job.employment_type && (
                  <FactRow label="Employment" value={job.employment_type} />
                )}
                {(job.salary_min || job.salary_max) && (
                  <FactRow
                    label="Salary"
                    value={
                      <span className="text-green-700">
                        {job.salary_min && job.salary_max
                          ? `${job.salary_currency ?? "USD"} ${job.salary_min.toLocaleString()}–${job.salary_max.toLocaleString()}`
                          : job.salary_max
                          ? `Up to ${job.salary_max.toLocaleString()}`
                          : `From ${job.salary_min!.toLocaleString()}`}
                      </span>
                    }
                  />
                )}
                {job.role_category && (
                  <FactRow label="Role" value={job.role_category} />
                )}
                {job.source && (
                  <FactRow label="Source" value={job.source} />
                )}
                {job.posted_at && (
                  <FactRow
                    label="Posted"
                    value={new Date(job.posted_at).toLocaleDateString("en-US", {
                      year: "numeric",
                      month: "short",
                      day: "numeric",
                    })}
                  />
                )}
              </dl>
            </Section>

            {/* Required skills */}
            {job.required_skills && job.required_skills.length > 0 && (
              <Section title="Required skills">
                <div className="flex flex-wrap gap-1.5">
                  {job.required_skills.map((s) => (
                    <span
                      key={s}
                      className="px-2.5 py-1 text-xs bg-brand/10 text-brand
                                 rounded-full font-medium border border-brand/20"
                    >
                      {s}
                    </span>
                  ))}
                </div>
              </Section>
            )}

            {/* Apply CTA — sticky at bottom of scroll */}
            {job.job_url ? (
              <div className="mt-6 space-y-2">
                <a
                  href={job.job_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block w-full text-center py-3 px-6 bg-brand text-white
                             font-semibold rounded-xl hover:bg-blue-700 active:bg-blue-800
                             transition-colors shadow-sm"
                >
                  Apply now →
                </a>

                {/* Save to tracker */}
                {user ? (
                  <button
                    onClick={handleSave}
                    disabled={saving || saved}
                    className={`w-full py-2.5 px-6 font-semibold rounded-xl transition-colors text-sm
                      ${saved
                        ? "bg-green-50 text-green-700 border border-green-200 cursor-default"
                        : "bg-gray-100 text-gray-700 hover:bg-gray-200 border border-gray-200"
                      }`}
                  >
                    {saved ? "✓ Saved to tracker" : saving ? "Saving…" : "📋 Save to my applications"}
                  </button>
                ) : (
                  <Link
                    href="/login"
                    className="block w-full text-center py-2.5 px-6 text-sm font-medium
                               text-gray-600 bg-gray-100 hover:bg-gray-200 rounded-xl
                               border border-gray-200 transition-colors"
                  >
                    Log in to track this application
                  </Link>
                )}

                <p className="text-center text-xs text-gray-400">
                  Opens the company&apos;s careers page
                </p>
              </div>
            ) : (
              <div className="mt-6 py-3 px-6 bg-gray-100 text-gray-400 text-sm
                              text-center rounded-xl">
                No direct application link available
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
