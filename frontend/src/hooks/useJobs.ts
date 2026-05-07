"use client";
/**
 * useJobs — single source of truth for the public job search.
 *
 * Architecture:
 *   - One combined QueryState (filters + page) drives every fetch.
 *     This eliminates the race condition where setParams() and fetchJobs()
 *     are called separately: here a single setQuery() triggers everything.
 *
 *   - appendRef is set atomically BEFORE the state update so the effect
 *     always reads the correct intent (replace vs append) even under React's
 *     concurrent rendering.
 *
 *   - alive flag gates ALL state updates — prevents the aborted request's
 *     .finally() from resetting loading=false while the next request is live.
 *
 *   - AbortController is created per-effect; the previous one is cancelled
 *     before the new fetch starts, so stale responses can never land.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import type { Job, JobFilters, QueryState } from "@/types";
import { searchJobs } from "@/lib/api";

const PAGE_SIZE = 25;

export interface UseJobsReturn {
  jobs: Job[];
  total: number;
  hasMore: boolean;
  loading: boolean;
  /** Replace results with a new filter set — always resets to page 1. */
  updateFilters: (f: JobFilters) => void;
  /** Append next page. No-op when already loading or no more pages. */
  loadMore: () => void;
  /** Current active filters (derived from QueryState, read-only). */
  filters: JobFilters;
}

export function useJobs(initial: JobFilters = {}): UseJobsReturn {
  // ── Single source of truth ────────────────────────────────────────────────
  const [query, setQuery] = useState<QueryState>({
    ...initial,
    page: 1,
    page_size: PAGE_SIZE,
  });

  // ── Derived results ───────────────────────────────────────────────────────
  const [jobs,    setJobs]    = useState<Job[]>([]);
  const [total,   setTotal]   = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  // ── appendRef: set BEFORE state update, read at effect start ─────────────
  // Avoids checking page===1 when the state hasn't flushed yet.
  const appendRef = useRef(false);

  // ── AbortController ref: holds the previous in-flight controller ──────────
  const abortRef = useRef<AbortController | null>(null);

  // ── Main fetch effect ─────────────────────────────────────────────────────
  useEffect(() => {
    // 1. Cancel the previous in-flight request immediately.
    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    // 2. Capture and reset the append flag synchronously.
    const shouldAppend = appendRef.current;
    appendRef.current = false;

    // 3. alive flag — gates every state setter so the aborted request's
    //    .finally() cannot interfere with the new request's loading state.
    let alive = true;
    setLoading(true);

    searchJobs(query, ctrl.signal)
      .then((res) => {
        if (!alive) return;
        setJobs((prev) =>
          shouldAppend ? [...prev, ...res.jobs] : res.jobs
        );
        setTotal(res.total);
        setHasMore(res.has_more);
      })
      .catch((err: unknown) => {
        // AbortError is expected — swallow silently.
        if (err instanceof Error && err.name === "AbortError") return;
        if (!alive) return;
        // On real errors leave the existing list intact; loading resets below.
      })
      .finally(() => {
        // CRITICAL: only touch state if this effect is still the active one.
        if (alive) setLoading(false);
      });

    return () => {
      // Cleanup: mark dead and abort in case the component unmounts or the
      // query changes before the fetch completes.
      alive = false;
      ctrl.abort();
    };
  }, [query]); // eslint-disable-line react-hooks/exhaustive-deps
  // searchJobs is a stable module-level import — safe to omit from deps.

  // ── Public actions ────────────────────────────────────────────────────────

  const updateFilters = useCallback((f: JobFilters) => {
    appendRef.current = false;               // next fetch replaces
    setQuery({ ...f, page: 1, page_size: PAGE_SIZE });
  }, []);

  const loadMore = useCallback(() => {
    if (loading || !hasMore) return;
    appendRef.current = true;                // next fetch appends
    setQuery((prev) => ({ ...prev, page: prev.page + 1 }));
  }, [loading, hasMore]);

  // ── Derived filter view (strips pagination fields) ────────────────────────
  const filters: JobFilters = {
    q:          query.q,
    location:   query.location,
    experience: query.experience,
    remote:     query.remote,
    role:       query.role,
    freshness:  query.freshness,
    country:    query.country,
  };

  return { jobs, total, hasMore, loading, updateFilters, loadMore, filters };
}
