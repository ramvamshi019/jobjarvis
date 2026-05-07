"use client";
/**
 * SearchBar — debounced keyword + location inputs.
 *
 * - Keystrokes update local state immediately for snappy UX.
 * - onChange fires 300 ms after the user stops typing.
 * - Submitting the form fires immediately (bypasses debounce).
 * - Syncs back when parent resets filters (e.g. "Clear all").
 */
import { useState, useEffect, useRef, useCallback } from "react";
import type { JobFilters } from "@/types";

interface Props {
  filters: JobFilters;
  onChange: (f: Partial<JobFilters>) => void;
}

const DEBOUNCE_MS = 300;

export default function SearchBar({ filters, onChange }: Props) {
  const [q,   setQ]   = useState(filters.q ?? "");
  const [loc, setLoc] = useState(filters.location ?? "");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const firstRender = useRef(true);

  // Sync inputs when parent resets (e.g., "Clear all filters")
  useEffect(() => { setQ(filters.q ?? "");   }, [filters.q]);
  useEffect(() => { setLoc(filters.location ?? ""); }, [filters.location]);

  // Debounced emit
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return; }
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      onChange({ q: q.trim() || undefined, location: loc.trim() || undefined });
    }, DEBOUNCE_MS);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [q, loc]); // onChange is intentionally excluded — it's a stable useCallback

  // Immediate submit (Enter / button click)
  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (timerRef.current) clearTimeout(timerRef.current);
      onChange({ q: q.trim() || undefined, location: loc.trim() || undefined });
    },
    [q, loc, onChange],
  );

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col sm:flex-row gap-2 w-full max-w-4xl mx-auto"
    >
      {/* Keyword */}
      <div className="relative flex-1">
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none">
          🔍
        </span>
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Job title, keyword, or company"
          autoComplete="off"
          className="w-full pl-9 pr-4 py-2.5 rounded-xl border border-gray-200 bg-white
                     shadow-sm focus:outline-none focus:ring-2 focus:ring-brand text-sm"
        />
      </div>

      {/* Location */}
      <div className="relative sm:w-52">
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none">
          📍
        </span>
        <input
          type="text"
          value={loc}
          onChange={(e) => setLoc(e.target.value)}
          placeholder="City, country, or 'remote'"
          autoComplete="off"
          className="w-full pl-9 pr-4 py-2.5 rounded-xl border border-gray-200 bg-white
                     shadow-sm focus:outline-none focus:ring-2 focus:ring-brand text-sm"
        />
      </div>

      <button
        type="submit"
        className="px-6 py-2.5 bg-brand text-white font-semibold rounded-xl
                   hover:bg-blue-700 active:bg-blue-800 transition-colors text-sm shrink-0"
      >
        Search
      </button>
    </form>
  );
}
