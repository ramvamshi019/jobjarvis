"use client";
/**
 * USA — every US job, newest first.
 *
 * A focused, country-locked feed: country is always "US" and the API
 * already sorts newest-discovered first, so fresh jobs appear on top
 * (the useJobs hook auto-refreshes page 1 every 60s). The user can
 * narrow by recency (Last 24 h / 7 days) and by role. The live total
 * count is shown so the real ingestion volume is always visible.
 */
import { useState, useCallback } from "react";
import type { JobFilters } from "@/types";
import { useJobs } from "@/hooks/useJobs";
import JobList from "@/components/JobList";
import JobDetail from "@/components/JobDetail";

const FRESHNESS: { value: string; label: string }[] = [
  { value: "", label: "All time" },
  { value: "last_24h", label: "Last 24 hours" },
  { value: "last_7_days", label: "Last 7 days" },
];

const EXPERIENCE: { value: string; label: string }[] = [
  { value: "", label: "All levels" },
  { value: "entry", label: "Entry level" },
  { value: "mid", label: "Mid level" },
  { value: "senior", label: "Senior" },
];

const ROLES = [
  "",
  "Software Engineer",
  "Data Engineer",
  "AI Engineer",
  "ML Engineer",
  "MLOps Engineer",
  "Data Platform Engineer",
  "Analytics Engineer",
  "Backend Engineer",
  "QA/SDET",
];

export default function UsaPage() {
  const { jobs, total, hasMore, loading, updateFilters, loadMore } = useJobs({
    country: "US",
  });

  const [activeId, setActiveId] = useState<number | null>(null);
  const [freshness, setFreshness] = useState("");
  const [role, setRole] = useState("");
  const [experience, setExperience] = useState("");

  // country is always pinned to US; recency + role + level layer on top.
  const apply = useCallback(
    (next: Partial<JobFilters>) => {
      const f: JobFilters = {
        country: "US",
        freshness: next.freshness ?? freshness,
        role: next.role ?? role,
        experience: next.experience ?? experience,
      };
      updateFilters(f);
      setActiveId(null);
    },
    [freshness, role, experience, updateFilters],
  );

  const onFreshness = (v: string) => {
    setFreshness(v);
    apply({ freshness: v });
  };
  const onRole = (v: string) => {
    setRole(v);
    apply({ role: v });
  };
  const onExperience = (v: string) => {
    setExperience(v);
    apply({ experience: v });
  };

  return (
    <div className="min-h-screen bg-gray-50">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="bg-white border-b border-gray-100 px-4 sm:px-6 lg:px-8 py-4">
        <div className="max-w-7xl mx-auto">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h1 className="text-2xl font-extrabold text-gray-900">
                USA jobs
              </h1>
              <p className="text-sm text-gray-600 mt-0.5">
                {loading && total === 0 ? (
                  "Loading…"
                ) : (
                  <>
                    <span className="font-semibold text-gray-900">
                      {total.toLocaleString()}
                    </span>{" "}
                    {total === 1 ? "job" : "jobs"} in the United States — newest
                    first
                  </>
                )}
              </p>
            </div>

            {/* Role selector */}
            <select
              value={role}
              onChange={(e) => onRole(e.target.value)}
              className="rounded-lg border border-gray-200 bg-white px-3 py-2
                         text-sm text-gray-700 focus:outline-none focus:ring-2
                         focus:ring-brand/30"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r === "" ? "All roles" : r}
                </option>
              ))}
            </select>
          </div>

          {/* Experience-level chips */}
          <div className="flex flex-wrap items-center gap-2 mt-3">
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-400 mr-1">
              Level
            </span>
            {EXPERIENCE.map((opt) => {
              const isActive = experience === opt.value;
              return (
                <button
                  key={opt.value || "all"}
                  type="button"
                  onClick={() => onExperience(opt.value)}
                  className={`px-3 py-1.5 rounded-full text-sm font-medium border
                             transition-colors ${
                               isActive
                                 ? "bg-brand text-white border-brand"
                                 : "bg-white text-gray-600 border-gray-200 hover:bg-blue-50"
                             }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>

          {/* Recency chips */}
          <div className="flex flex-wrap items-center gap-2 mt-2">
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-400 mr-1">
              When
            </span>
            {FRESHNESS.map((opt) => {
              const isActive = freshness === opt.value;
              return (
                <button
                  key={opt.value || "all"}
                  type="button"
                  onClick={() => onFreshness(opt.value)}
                  className={`px-3 py-1.5 rounded-full text-sm font-medium border
                             transition-colors ${
                               isActive
                                 ? "bg-brand text-white border-brand"
                                 : "bg-white text-gray-600 border-gray-200 hover:bg-blue-50"
                             }`}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* ── Body: list + detail ────────────────────────────────────────────── */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="flex gap-6">
          <div className={`flex-1 min-w-0 ${activeId ? "hidden lg:block" : "block"}`}>
            <JobList
              jobs={jobs}
              loading={loading}
              hasMore={hasMore}
              activeId={activeId}
              onSelect={setActiveId}
              onLoadMore={loadMore}
            />
          </div>

          {activeId !== null && (
            <aside className="w-full lg:w-[440px] shrink-0">
              <div
                className="sticky top-[72px] overflow-hidden rounded-xl border
                           border-gray-200 shadow-md bg-white"
                style={{ height: "calc(100vh - 90px)" }}
              >
                <JobDetail jobId={activeId} onClose={() => setActiveId(null)} />
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
