"use client";
/**
 * Companies — directory of tracked companies with live hiring signals.
 *
 * Uses the public analytics endpoint (no auth required).
 * Shows: company name, ATS platform, recent job count, last activity.
 * Supports filtering by ATS type and searching by name.
 */
import { useState, useEffect, useMemo } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface HiringCompany {
  company_id: number;
  company_name: string;
  ats_type: string | null;
  jobs_count: number;
  last_posted: string | null;
}

interface HiringResponse {
  companies: HiringCompany[];
  window_days: number;
  as_of: string;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const ATS_COLOURS: Record<string, string> = {
  greenhouse:      "bg-green-100 text-green-800 border-green-200",
  lever:           "bg-purple-100 text-purple-800 border-purple-200",
  ashby:           "bg-blue-100 text-blue-800 border-blue-200",
  smartrecruiters: "bg-orange-100 text-orange-800 border-orange-200",
  workday:         "bg-yellow-100 text-yellow-800 border-yellow-200",
  icims:           "bg-teal-100 text-teal-800 border-teal-200",
  workable:        "bg-pink-100 text-pink-800 border-pink-200",
  bamboohr:        "bg-emerald-100 text-emerald-800 border-emerald-200",
  breezy:          "bg-cyan-100 text-cyan-800 border-cyan-200",
  recruitee:       "bg-indigo-100 text-indigo-800 border-indigo-200",
};

const ATS_DOT: Record<string, string> = {
  greenhouse:      "bg-green-500",
  lever:           "bg-purple-500",
  ashby:           "bg-blue-500",
  smartrecruiters: "bg-orange-400",
  workday:         "bg-yellow-500",
  icims:           "bg-teal-500",
  workable:        "bg-pink-500",
  bamboohr:        "bg-emerald-600",
  breezy:          "bg-cyan-500",
  recruitee:       "bg-indigo-500",
};

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  if (h < 1) return "Just now";
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return `${Math.floor(d / 7)}w ago`;
}

function ActivityBar({ count, max }: { count: number; max: number }) {
  const pct = max > 0 ? (count / max) * 100 : 0;
  const colour =
    pct >= 66 ? "bg-orange-400" : pct >= 33 ? "bg-brand" : "bg-gray-300";
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 bg-gray-100 rounded-full h-1.5">
        <div
          className={`h-1.5 rounded-full ${colour} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-sm font-semibold text-gray-700 w-8 text-right">
        {count}
      </span>
    </div>
  );
}

function SkeletonRow() {
  return (
    <tr className="animate-pulse border-b border-gray-50">
      <td className="py-3 pr-4"><div className="h-4 bg-gray-200 rounded w-32" /></td>
      <td className="py-3 pr-4"><div className="h-5 bg-gray-200 rounded-full w-20" /></td>
      <td className="py-3 pr-4"><div className="h-3 bg-gray-200 rounded w-24" /></td>
      <td className="py-3"><div className="h-3 bg-gray-200 rounded w-12" /></td>
    </tr>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

const WINDOW_OPTIONS = [
  { label: "24 h",   days: 1 },
  { label: "7 days", days: 7 },
  { label: "30 d",   days: 30 },
  { label: "90 d",   days: 90 },
];

export default function CompaniesPage() {
  const [data, setData]       = useState<HiringResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [windowDays, setWindowDays] = useState(7);
  const [search, setSearch]   = useState("");
  const [atsFilter, setAtsFilter] = useState<string | null>(null);

  // Fetch whenever window changes
  useEffect(() => {
    setLoading(true);
    fetch(`/api/analytics/companies/hiring?days=${windowDays}&top_k=100`)
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((d: HiringResponse) => setData(d))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [windowDays]);

  // Derived list of ATS options from data
  const atsOptions = useMemo(() => {
    if (!data) return [];
    const set = new Set(data.companies.map((c) => c.ats_type ?? "unknown"));
    return Array.from(set).sort();
  }, [data]);

  // Filtered + searched companies
  const filtered = useMemo(() => {
    if (!data) return [];
    return data.companies.filter((c) => {
      const matchesSearch =
        !search.trim() ||
        c.company_name.toLowerCase().includes(search.toLowerCase().trim());
      const matchesAts =
        !atsFilter || (c.ats_type ?? "unknown") === atsFilter;
      return matchesSearch && matchesAts;
    });
  }, [data, search, atsFilter]);

  const maxCount = useMemo(
    () => Math.max(...(data?.companies.map((c) => c.jobs_count) ?? [1])),
    [data],
  );

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Page header */}
      <div className="bg-white border-b border-gray-200 px-4 sm:px-6 lg:px-8 py-6">
        <div className="max-w-7xl mx-auto">
          <h1 className="text-2xl font-extrabold text-gray-900">Company Directory</h1>
          <p className="text-sm text-gray-500 mt-1">
            Companies actively hiring tracked by JobJarvis
            {data && (
              <span className="ml-2 text-gray-400">
                · {data.companies.length} companies in the last {data.window_days} days
              </span>
            )}
          </p>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

        {/* Controls */}
        <div className="flex flex-col sm:flex-row gap-3 mb-6">
          {/* Search */}
          <div className="relative flex-1">
            <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none">
              🔍
            </span>
            <input
              type="search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search company name…"
              className="w-full pl-9 pr-4 py-2.5 rounded-xl border border-gray-200 bg-white
                         shadow-sm focus:outline-none focus:ring-2 focus:ring-brand text-sm"
            />
          </div>

          {/* ATS filter chips */}
          <div className="flex flex-wrap gap-2 items-center">
            <button
              onClick={() => setAtsFilter(null)}
              className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                !atsFilter
                  ? "bg-brand text-white border-brand"
                  : "bg-white text-gray-600 border-gray-200 hover:border-brand hover:text-brand"
              }`}
            >
              All
            </button>
            {atsOptions.map((ats) => (
              <button
                key={ats}
                onClick={() => setAtsFilter(atsFilter === ats ? null : ats)}
                className={`px-3 py-1.5 rounded-full text-xs font-medium border transition-colors flex items-center gap-1.5 ${
                  atsFilter === ats
                    ? "bg-brand text-white border-brand"
                    : "bg-white text-gray-600 border-gray-200 hover:border-brand hover:text-brand"
                }`}
              >
                <span className={`w-2 h-2 rounded-full ${ATS_DOT[ats] ?? "bg-gray-400"}`} />
                {ats}
              </button>
            ))}
          </div>

          {/* Time window */}
          <div className="flex gap-1 shrink-0">
            {WINDOW_OPTIONS.map(({ label, days }) => (
              <button
                key={days}
                onClick={() => setWindowDays(days)}
                className={`px-3 py-1.5 rounded-xl text-xs font-medium border transition-colors ${
                  windowDays === days
                    ? "bg-gray-900 text-white border-gray-900"
                    : "bg-white text-gray-600 border-gray-200 hover:border-gray-400"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Result count */}
        <p className="text-sm text-gray-500 mb-4">
          {loading ? (
            <span className="animate-pulse">Loading…</span>
          ) : (
            <>
              <span className="font-semibold text-gray-900">{filtered.length}</span>
              {filtered.length !== data?.companies.length && (
                <> of {data?.companies.length}</>
              )}{" "}
              companies · sorted by hiring activity
            </>
          )}
        </p>

        {/* Table */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b border-gray-100 bg-gray-50/50">
                <tr>
                  <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-widest px-5 py-3">
                    Company
                  </th>
                  <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-widest px-4 py-3">
                    Platform
                  </th>
                  <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-widest px-4 py-3">
                    Jobs ({windowDays}d)
                  </th>
                  <th className="text-right text-xs font-semibold text-gray-400 uppercase tracking-widest px-5 py-3">
                    Last posted
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {loading
                  ? Array.from({ length: 15 }).map((_, i) => <SkeletonRow key={i} />)
                  : filtered.length === 0
                  ? (
                    <tr>
                      <td colSpan={4} className="text-center py-16 text-gray-400 text-sm">
                        No companies found
                        {search && <> matching "<strong>{search}</strong>"</>}
                      </td>
                    </tr>
                  )
                  : filtered.map((c, i) => {
                      const ats = c.ats_type ?? "unknown";
                      const atsClass = ATS_COLOURS[ats] ?? "bg-gray-100 text-gray-700 border-gray-200";
                      return (
                        <tr
                          key={c.company_id}
                          className="hover:bg-gray-50/50 transition-colors"
                        >
                          <td className="px-5 py-3 font-medium text-gray-900">
                            <span className="text-gray-300 text-xs mr-2 tabular-nums">
                              {String(i + 1).padStart(2, "0")}
                            </span>
                            {c.company_name}
                          </td>
                          <td className="px-4 py-3">
                            <span
                              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                                          text-[11px] font-medium border ${atsClass}`}
                            >
                              <span className={`w-1.5 h-1.5 rounded-full ${ATS_DOT[ats] ?? "bg-gray-400"}`} />
                              {ats}
                            </span>
                          </td>
                          <td className="px-4 py-3">
                            <ActivityBar count={c.jobs_count} max={maxCount} />
                          </td>
                          <td className="px-5 py-3 text-right text-xs text-gray-400 whitespace-nowrap">
                            {timeAgo(c.last_posted)}
                          </td>
                        </tr>
                      );
                    })}
              </tbody>
            </table>
          </div>

          {/* Footer note */}
          {!loading && data && (
            <div className="border-t border-gray-100 px-5 py-3 bg-gray-50/30">
              <p className="text-xs text-gray-400">
                Showing companies that posted at least 1 job in the last {windowDays} day{windowDays !== 1 && "s"} ·
                updated {new Date(data.as_of).toLocaleTimeString()}
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
