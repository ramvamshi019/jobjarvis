"use client";
/**
 * Dashboard — application tracker.
 *
 * Kanban columns: Saved → Applied → Interview → Offer / Rejected
 * Each card shows job title, company, and quick-action status buttons.
 */
import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import {
  listApplications,
  updateApplication,
  type AppStatus,
  type TrackedApplication,
} from "@/lib/applications";
import { getJob } from "@/lib/api";

// ── Column config ─────────────────────────────────────────────────────────────

const COLUMNS: { status: AppStatus; label: string; color: string; dot: string }[] = [
  { status: "saved",     label: "Saved",     color: "bg-gray-50  border-gray-200",  dot: "bg-gray-400"   },
  { status: "applied",   label: "Applied",   color: "bg-blue-50  border-blue-200",  dot: "bg-brand"      },
  { status: "interview", label: "Interview", color: "bg-amber-50 border-amber-200", dot: "bg-amber-500"  },
  { status: "offer",     label: "Offer 🎉",  color: "bg-green-50 border-green-200", dot: "bg-green-500"  },
  { status: "rejected",  label: "Rejected",  color: "bg-red-50   border-red-100",   dot: "bg-red-400"    },
];

const NEXT_STATUS: Record<AppStatus, AppStatus | null> = {
  saved:     "applied",
  applied:   "interview",
  interview: "offer",
  offer:     null,
  rejected:  null,
  closed:    null,
};

// ── Types ─────────────────────────────────────────────────────────────────────

interface RichApp extends TrackedApplication {
  job_title: string;
  company_name: string;
  job_url: string | null;
  location: string | null;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function timeAgo(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const d = Math.floor(diff / 86_400_000);
  if (d === 0) return "Today";
  if (d === 1) return "Yesterday";
  if (d < 7)  return `${d}d ago`;
  return `${Math.floor(d / 7)}w ago`;
}

function SkeletonCard() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 animate-pulse space-y-2">
      <div className="h-4 bg-gray-200 rounded w-3/4" />
      <div className="h-3 bg-gray-200 rounded w-1/2" />
      <div className="h-3 bg-gray-200 rounded w-1/3" />
    </div>
  );
}

// ── Application card ──────────────────────────────────────────────────────────

function AppCard({
  app,
  onStatusChange,
}: {
  app: RichApp;
  onStatusChange: (id: number, status: AppStatus) => void;
}) {
  const [busy, setBusy] = useState(false);
  const nextStatus = NEXT_STATUS[app.status];

  async function advance() {
    if (!nextStatus || busy) return;
    setBusy(true);
    try {
      await updateApplication(app.id, { status: nextStatus });
      onStatusChange(app.id, nextStatus);
    } catch {
      /* swallow */
    } finally {
      setBusy(false);
    }
  }

  async function markRejected() {
    if (busy) return;
    setBusy(true);
    try {
      await updateApplication(app.id, { status: "rejected" });
      onStatusChange(app.id, "rejected");
    } catch {
      /* swallow */
    } finally {
      setBusy(false);
    }
  }

  const ADVANCE_LABELS: Record<AppStatus, string> = {
    saved:     "Mark applied →",
    applied:   "Got interview →",
    interview: "Got offer →",
    offer:     "",
    rejected:  "",
    closed:    "",
  };

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 shadow-sm
                    hover:shadow-md transition-shadow space-y-2">
      {/* Title + link */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-gray-900 leading-snug line-clamp-2">
            {app.job_title}
          </p>
          <p className="text-xs text-brand font-medium mt-0.5">{app.company_name}</p>
          {app.location && (
            <p className="text-xs text-gray-400 mt-0.5">📍 {app.location}</p>
          )}
        </div>
        {app.job_url && (
          <a
            href={app.job_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-brand hover:underline shrink-0"
          >
            View →
          </a>
        )}
      </div>

      {/* Date + interview rounds */}
      <div className="flex items-center gap-3 text-xs text-gray-400">
        <span>{timeAgo(app.created_at)}</span>
        {app.interview_rounds > 0 && (
          <span className="text-amber-600 font-medium">
            {app.interview_rounds} round{app.interview_rounds > 1 ? "s" : ""}
          </span>
        )}
        {app.applied_at && (
          <span>Applied {timeAgo(app.applied_at)}</span>
        )}
      </div>

      {/* Actions */}
      {(nextStatus || app.status !== "rejected") && (
        <div className="flex gap-1.5 pt-1">
          {nextStatus && ADVANCE_LABELS[app.status] && (
            <button
              onClick={advance}
              disabled={busy}
              className="flex-1 py-1.5 text-xs font-medium rounded-lg bg-brand/10 text-brand
                         hover:bg-brand/20 transition-colors disabled:opacity-50"
            >
              {busy ? "…" : ADVANCE_LABELS[app.status]}
            </button>
          )}
          {app.status !== "rejected" && app.status !== "offer" && (
            <button
              onClick={markRejected}
              disabled={busy}
              className="py-1.5 px-2 text-xs font-medium rounded-lg bg-red-50 text-red-500
                         hover:bg-red-100 transition-colors disabled:opacity-50"
            >
              ✕
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();

  const [apps,    setApps]    = useState<RichApp[]>([]);
  const [loading, setLoading] = useState(true);

  // Redirect if not logged in
  useEffect(() => {
    if (!authLoading && !user) router.replace("/login");
  }, [user, authLoading, router]);

  const loadApps = useCallback(async () => {
    setLoading(true);
    try {
      const raw = await listApplications();

      // Enrich each app with job details in parallel
      const enriched = await Promise.all(
        raw.map(async (app) => {
          try {
            const job = await getJob(app.job_id);
            return {
              ...app,
              job_title:    job.title,
              company_name: job.company_name,
              job_url:      job.job_url ?? null,
              location:
                [job.city, job.country].filter(Boolean).join(", ") ||
                job.location ||
                null,
            } as RichApp;
          } catch {
            return {
              ...app,
              job_title:    `Job #${app.job_id}`,
              company_name: "Unknown",
              job_url:      null,
              location:     null,
            } as RichApp;
          }
        }),
      );

      setApps(enriched);
    } catch {
      /* leave empty */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user) loadApps();
  }, [user, loadApps]);

  function handleStatusChange(id: number, status: AppStatus) {
    setApps((prev) =>
      prev.map((a) => (a.id === id ? { ...a, status } : a)),
    );
  }

  if (authLoading) return null;
  if (!user) return null;

  const byStatus = (status: AppStatus) => apps.filter((a) => a.status === status);

  const totalActive = apps.filter(
    (a) => !["rejected", "closed"].includes(a.status),
  ).length;

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <div className="bg-white border-b border-gray-200 px-4 sm:px-6 lg:px-8 py-6">
        <div className="max-w-7xl mx-auto flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-extrabold text-gray-900">My Applications</h1>
            <p className="text-sm text-gray-500 mt-1">
              {loading ? "Loading…" : (
                <>
                  <span className="font-semibold text-gray-800">{totalActive}</span> active ·{" "}
                  <span className="font-semibold text-gray-800">{apps.length}</span> total
                </>
              )}
            </p>
          </div>
          <Link
            href="/"
            className="px-4 py-2 bg-brand text-white text-sm font-semibold
                       rounded-xl hover:bg-blue-700 transition-colors shadow-sm"
          >
            + Find jobs
          </Link>
        </div>
      </div>

      {/* Kanban board */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

        {loading ? (
          /* Skeleton */
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
            {COLUMNS.map((col) => (
              <div key={col.status}>
                <div className="h-6 bg-gray-200 rounded w-20 mb-3 animate-pulse" />
                <div className="space-y-3">
                  {[1, 2].map((i) => <SkeletonCard key={i} />)}
                </div>
              </div>
            ))}
          </div>
        ) : apps.length === 0 ? (
          /* Empty state */
          <div className="text-center py-24">
            <span className="text-6xl block mb-4">📋</span>
            <h2 className="text-xl font-bold text-gray-700 mb-2">No applications yet</h2>
            <p className="text-gray-500 text-sm mb-6">
              Save jobs from the search page to start tracking them here.
            </p>
            <Link
              href="/"
              className="inline-block px-6 py-3 bg-brand text-white font-semibold
                         rounded-xl hover:bg-blue-700 transition-colors shadow-sm"
            >
              Browse jobs →
            </Link>
          </div>
        ) : (
          /* Columns */
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
            {COLUMNS.map((col) => {
              const colApps = byStatus(col.status);
              return (
                <div key={col.status}>
                  {/* Column header */}
                  <div className="flex items-center gap-2 mb-3">
                    <span className={`w-2.5 h-2.5 rounded-full ${col.dot}`} />
                    <span className="text-sm font-semibold text-gray-700">
                      {col.label}
                    </span>
                    <span className="ml-auto text-xs font-medium text-gray-400
                                     bg-gray-100 px-2 py-0.5 rounded-full">
                      {colApps.length}
                    </span>
                  </div>

                  {/* Cards */}
                  <div
                    className={`rounded-xl border p-3 min-h-[120px] space-y-3 ${col.color}`}
                  >
                    {colApps.length === 0 ? (
                      <p className="text-xs text-gray-300 text-center pt-6">
                        Empty
                      </p>
                    ) : (
                      colApps.map((app) => (
                        <AppCard
                          key={app.id}
                          app={app}
                          onStatusChange={handleStatusChange}
                        />
                      ))
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
