"use client";
import { useEffect, useRef } from "react";
import type { Job } from "@/types";
import JobCard from "./JobCard";

interface Props {
  jobs: Job[];
  loading: boolean;
  hasMore: boolean;
  activeId: number | null;
  onSelect: (id: number) => void;
  onLoadMore: () => void;
  highlight?: string;
}

function SkeletonCard() {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4 space-y-3 animate-pulse">
      <div className="flex justify-between gap-3">
        <div className="space-y-1.5 flex-1">
          <div className="h-4 bg-gray-200 rounded w-3/4" />
          <div className="h-3 bg-gray-200 rounded w-1/2" />
        </div>
        <div className="h-3 bg-gray-200 rounded w-12" />
      </div>
      <div className="h-3 bg-gray-200 rounded w-2/3" />
      <div className="flex gap-2">
        <div className="h-5 bg-gray-200 rounded-full w-16" />
        <div className="h-5 bg-gray-200 rounded-full w-24" />
      </div>
    </div>
  );
}

export default function JobList({
  jobs,
  loading,
  hasMore,
  activeId,
  onSelect,
  onLoadMore,
  highlight,
}: Props) {
  const sentinelRef = useRef<HTMLDivElement>(null);

  // Infinite scroll — IntersectionObserver on the sentinel div
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && hasMore && !loading) onLoadMore();
      },
      { threshold: 0.1 },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [hasMore, loading, onLoadMore]);

  // Empty state (initial load or no results)
  if (!loading && jobs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-24 text-center">
        <span className="text-5xl mb-4">🔍</span>
        <p className="font-semibold text-gray-700">No jobs found</p>
        <p className="text-sm text-gray-400 mt-1">
          Try different keywords or clear some filters
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {/* Job cards */}
      {jobs.map((job) => (
        <JobCard
          key={job.id}
          job={job}
          active={job.id === activeId}
          onClick={() => onSelect(job.id)}
          highlight={highlight}
        />
      ))}

      {/* Skeleton loaders — shown during first load or load-more */}
      {loading &&
        [1, 2, 3].map((i) => <SkeletonCard key={i} />)}

      {/* IntersectionObserver sentinel — triggers loadMore */}
      <div ref={sentinelRef} className="h-4" aria-hidden />

      {/* End-of-results label */}
      {!hasMore && jobs.length > 0 && !loading && (
        <p className="text-center text-xs text-gray-400 py-6">
          All {jobs.length.toLocaleString()} results shown
        </p>
      )}
    </div>
  );
}
