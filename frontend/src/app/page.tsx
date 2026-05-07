"use client";
/**
 * Homepage — public job search (no authentication).
 *
 * Layout:
 *   - Sticky top nav with search bar
 *   - Hero section (shown when no active search)
 *   - 3-column body: [filters | job list | job detail]
 *
 * All data is fetched via useJobs() hook → GET /api/jobs/search (public).
 */
import { useState, useCallback, useEffect } from "react";
import type { JobFilters, Stats } from "@/types";
import { useJobs } from "@/hooks/useJobs";
import { getStats } from "@/lib/api";
import SearchBar   from "@/components/SearchBar";
import FilterPanel from "@/components/FilterPanel";
import JobList     from "@/components/JobList";
import JobDetail   from "@/components/JobDetail";

export default function HomePage() {
  const { jobs, total, hasMore, loading, filters, updateFilters, loadMore } =
    useJobs({});

  const [activeId,    setActiveId]    = useState<number | null>(null);
  const [stats,       setStats]       = useState<Stats | null>(null);
  const [filterOpen,  setFilterOpen]  = useState(false);

  // Fetch homepage stats once
  useEffect(() => {
    getStats().then(setStats).catch(() => {});
  }, []);

  // Close detail panel when filter/search changes
  const handleUpdateFilters = useCallback(
    (f: JobFilters) => {
      updateFilters(f);
      setActiveId(null);
    },
    [updateFilters],
  );

  // SearchBar sends partial updates (q + location only) — merge with existing filters
  const handleSearchChange = useCallback(
    (partial: Partial<JobFilters>) => {
      handleUpdateFilters({ ...filters, ...partial });
    },
    [filters, handleUpdateFilters],
  );

  const hasActiveFilters =
    filters.experience || filters.remote || filters.freshness || filters.country;

  const hasActiveSearch = filters.q || filters.location;
  const showHero = !hasActiveSearch && !hasActiveFilters && jobs.length === 0 && !loading;

  return (
    <div className="min-h-screen bg-gray-50">

      {/* ── Search bar (below global nav) ───────────────────────────────────── */}
      <div className="bg-white border-b border-gray-100 px-4 sm:px-6 lg:px-8 py-2.5">
        <div className="max-w-7xl mx-auto">
          <SearchBar filters={filters} onChange={handleSearchChange} />
        </div>
      </div>

      {/* ── Hero (empty state, no active search) ────────────────────────────── */}
      {showHero && (
        <div className="bg-gradient-to-br from-brand to-blue-800 py-16 px-4 text-center text-white">
          <h1 className="text-4xl font-extrabold mb-3">Find your next tech job</h1>
          <p className="text-blue-100 text-lg mb-1">
            {stats
              ? `${stats.total_jobs.toLocaleString()} jobs from top companies`
              : "Thousands of jobs from top companies"}
            , updated every 5 minutes
          </p>
          {stats && stats.last_24h > 0 && (
            <p className="text-blue-200 text-sm mt-1">
              🔥 {stats.last_24h.toLocaleString()} new in the last 24 h
            </p>
          )}
        </div>
      )}

      {/* ── Main layout ─────────────────────────────────────────────────────── */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">

        {/* Result count + mobile filter toggle */}
        <div className="flex items-center justify-between mb-4 min-h-[28px]">
          <div className="text-sm text-gray-600">
            {total > 0 ? (
              <>
                <span className="font-semibold text-gray-900">
                  {total.toLocaleString()}
                </span>{" "}
                {total === 1 ? "job" : "jobs"}
                {filters.q && (
                  <>
                    {" "}for "<strong>{filters.q}</strong>"
                  </>
                )}
              </>
            ) : !loading ? (
              <span className="text-gray-400">
                {hasActiveSearch || hasActiveFilters
                  ? "No results — try different terms"
                  : "Search above to find jobs"}
              </span>
            ) : null}
          </div>

          {/* Mobile filter toggle */}
          <button
            type="button"
            onClick={() => setFilterOpen((v) => !v)}
            className="sm:hidden text-sm text-brand font-medium flex items-center gap-1.5"
          >
            {filterOpen ? "Hide filters" : "Filters"}
            {hasActiveFilters && (
              <span className="w-2 h-2 rounded-full bg-brand" />
            )}
          </button>
        </div>

        <div className="flex gap-6">

          {/* ── Filter sidebar ─────────────────────────────────────────────── */}
          <div
            className={`shrink-0 w-52 ${filterOpen ? "block" : "hidden"} sm:block`}
          >
            <div className="sticky top-[72px] bg-white rounded-xl border border-gray-200 p-4">
              <FilterPanel filters={filters} onChange={handleUpdateFilters} />
            </div>
          </div>

          {/* ── Job list ───────────────────────────────────────────────────── */}
          {/* Hidden on mobile when a detail panel is open */}
          <div className={`flex-1 min-w-0 ${activeId ? "hidden lg:block" : "block"}`}>
            <JobList
              jobs={jobs}
              loading={loading}
              hasMore={hasMore}
              activeId={activeId}
              onSelect={setActiveId}
              onLoadMore={loadMore}
              highlight={filters.q}
            />
          </div>

          {/* ── Job detail panel ───────────────────────────────────────────── */}
          {activeId !== null && (
            <aside className="w-full lg:w-[440px] shrink-0">
              <div
                className="sticky top-[72px] overflow-hidden rounded-xl border
                           border-gray-200 shadow-md bg-white"
                style={{ height: "calc(100vh - 90px)" }}
              >
                <JobDetail
                  jobId={activeId}
                  onClose={() => setActiveId(null)}
                />
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}
