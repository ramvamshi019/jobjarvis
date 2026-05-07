"use client";
/**
 * Analytics — market intelligence dashboard.
 *
 * Sections:
 *   1. Market overview (totals, 24h/7d activity)
 *   2. Role distribution (horizontal bars)
 *   3. Trending skills (ranked chips)
 *   4. Remote type breakdown
 *   5. Top hiring companies
 */
import { useState, useEffect } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface MarketOverview {
  total_active_jobs: number;
  jobs_last_24h: number;
  jobs_last_7d: number;
  total_companies: number;
  ats_distribution: Record<string, number>;
  role_distribution: Record<string, number>;
  remote_distribution: Record<string, number>;
  as_of: string;
}

interface SkillCount {
  skill: string;
  count: number;
  pct_of_jobs: number;
}

interface TrendingSkills {
  skills: SkillCount[];
  window_days: number;
  total_jobs_analyzed: number;
}

interface HiringCompany {
  company_id: number;
  company_name: string;
  ats_type: string | null;
  jobs_count: number;
  last_posted: string | null;
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────

async function fetchOverview(): Promise<MarketOverview> {
  const r = await fetch("/api/analytics/market/overview");
  if (!r.ok) throw new Error("overview failed");
  return r.json();
}

async function fetchSkills(days = 30): Promise<TrendingSkills> {
  const r = await fetch(`/api/analytics/skills/trending?days=${days}&top_k=30`);
  if (!r.ok) throw new Error("skills failed");
  return r.json();
}

async function fetchHiring(): Promise<{ companies: HiringCompany[]; window_days: number }> {
  const r = await fetch("/api/analytics/companies/hiring?top_k=20&days=7");
  if (!r.ok) throw new Error("hiring failed");
  return r.json();
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-1">
        {label}
      </p>
      <p className={`text-3xl font-extrabold ${accent ?? "text-gray-900"}`}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </p>
      {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
    </div>
  );
}

function BarChart({
  data,
  color = "bg-brand",
}: {
  data: [string, number][];
  color?: string;
}) {
  const max = Math.max(...data.map(([, v]) => v), 1);
  return (
    <div className="space-y-2">
      {data.map(([label, value]) => (
        <div key={label} className="flex items-center gap-3">
          <span className="w-36 shrink-0 text-xs text-gray-600 truncate capitalize">
            {label}
          </span>
          <div className="flex-1 bg-gray-100 rounded-full h-2">
            <div
              className={`h-2 rounded-full ${color} transition-all`}
              style={{ width: `${(value / max) * 100}%` }}
            />
          </div>
          <span className="w-10 text-right text-xs font-semibold text-gray-700">
            {value.toLocaleString()}
          </span>
        </div>
      ))}
    </div>
  );
}

function SkillChip({
  skill,
  count,
  pct,
  rank,
}: {
  skill: string;
  count: number;
  pct: number;
  rank: number;
}) {
  // Colour intensity based on rank
  const intensity =
    rank <= 3
      ? "bg-brand text-white border-brand"
      : rank <= 8
      ? "bg-brand/20 text-brand border-brand/30"
      : "bg-gray-100 text-gray-700 border-gray-200";

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border ${intensity}`}
      title={`${count} jobs (${pct}%)`}
    >
      {skill}
      <span className="opacity-70">{pct}%</span>
    </span>
  );
}

function SectionHeader({ title, sub }: { title: string; sub?: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-base font-bold text-gray-900">{title}</h2>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 shadow-sm">
      {children}
    </div>
  );
}

function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-3 animate-pulse">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="h-4 bg-gray-200 rounded" style={{ width: `${60 + (i % 3) * 15}%` }} />
      ))}
    </div>
  );
}

const ATS_COLOURS: Record<string, string> = {
  greenhouse: "bg-green-500",
  lever: "bg-purple-500",
  ashby: "bg-blue-500",
  smartrecruiters: "bg-orange-400",
  workday: "bg-yellow-500",
  icims: "bg-teal-500",
  workable: "bg-pink-500",
  bamboohr: "bg-emerald-600",
  breezy: "bg-cyan-500",
  recruitee: "bg-indigo-500",
  other: "bg-gray-400",
  unknown: "bg-gray-300",
};

// ── Main page ─────────────────────────────────────────────────────────────────

export default function AnalyticsPage() {
  const [overview, setOverview] = useState<MarketOverview | null>(null);
  const [skills, setSkills]     = useState<TrendingSkills | null>(null);
  const [hiring, setHiring]     = useState<HiringCompany[]>([]);
  const [loading, setLoading]   = useState(true);
  const [skillDays, setSkillDays] = useState(30);

  // Initial data load
  useEffect(() => {
    setLoading(true);
    Promise.all([fetchOverview(), fetchSkills(skillDays), fetchHiring()])
      .then(([ov, sk, hi]) => {
        setOverview(ov);
        setSkills(sk);
        setHiring(hi.companies ?? []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Reload skills when window changes
  useEffect(() => {
    fetchSkills(skillDays)
      .then(setSkills)
      .catch(() => {});
  }, [skillDays]);

  const rolePairs = overview
    ? Object.entries(overview.role_distribution).slice(0, 10)
    : [];

  const remotePairs = overview
    ? Object.entries(overview.remote_distribution).sort((a, b) => b[1] - a[1])
    : [];

  const atsPairs = overview
    ? Object.entries(overview.ats_distribution).sort((a, b) => b[1] - a[1])
    : [];

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Page header */}
      <div className="bg-white border-b border-gray-200 px-4 sm:px-6 lg:px-8 py-6">
        <div className="max-w-7xl mx-auto">
          <h1 className="text-2xl font-extrabold text-gray-900">Market Analytics</h1>
          <p className="text-sm text-gray-500 mt-1">
            Real-time insights from{" "}
            {overview ? overview.total_companies.toLocaleString() : "—"} tracked companies
            {overview && (
              <span className="ml-2 text-gray-400">
                · updated {new Date(overview.as_of).toLocaleTimeString()}
              </span>
            )}
          </p>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8 space-y-8">

        {/* ── Overview stat cards ─────────────────────────────────────────── */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
          {loading ? (
            [1, 2, 3, 4].map((i) => (
              <div key={i} className="bg-white rounded-xl border border-gray-200 p-5 h-24 animate-pulse">
                <div className="h-3 bg-gray-200 rounded w-2/3 mb-3" />
                <div className="h-8 bg-gray-200 rounded w-1/2" />
              </div>
            ))
          ) : (
            <>
              <StatCard
                label="Active Jobs"
                value={overview?.total_active_jobs ?? 0}
                sub="across all platforms"
                accent="text-brand"
              />
              <StatCard
                label="New (24 h)"
                value={overview?.jobs_last_24h ?? 0}
                sub="posted in last day"
                accent="text-orange-500"
              />
              <StatCard
                label="New (7 days)"
                value={overview?.jobs_last_7d ?? 0}
                sub="posted this week"
              />
              <StatCard
                label="Companies"
                value={overview?.total_companies ?? 0}
                sub="tracked & scanned"
              />
            </>
          )}
        </div>

        {/* ── Two-column row: Roles + Remote ──────────────────────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

          {/* Role distribution */}
          <Card>
            <SectionHeader
              title="Roles in demand"
              sub="Top 10 role categories by active job count"
            />
            {loading ? (
              <Skeleton rows={8} />
            ) : rolePairs.length > 0 ? (
              <BarChart data={rolePairs} color="bg-brand" />
            ) : (
              <p className="text-sm text-gray-400">No role data yet</p>
            )}
          </Card>

          {/* Remote breakdown */}
          <Card>
            <SectionHeader
              title="Work arrangement"
              sub="Remote vs hybrid vs on-site split"
            />
            {loading ? (
              <Skeleton rows={4} />
            ) : (
              <div className="space-y-5">
                <BarChart data={remotePairs} color="bg-purple-500" />

                {/* ATS platform share */}
                <div className="pt-4 border-t border-gray-100">
                  <p className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-3">
                    ATS platform breakdown
                  </p>
                  <div className="flex flex-wrap gap-2">
                    {atsPairs.map(([ats, count]) => (
                      <span
                        key={ats}
                        className="flex items-center gap-1.5 px-2.5 py-1 rounded-full
                                   text-xs font-medium bg-gray-100 text-gray-700 border border-gray-200"
                      >
                        <span
                          className={`w-2 h-2 rounded-full ${ATS_COLOURS[ats] ?? "bg-gray-400"}`}
                        />
                        {ats}
                        <span className="font-bold">{count}</span>
                      </span>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </Card>
        </div>

        {/* ── Trending skills ─────────────────────────────────────────────── */}
        <Card>
          <div className="flex items-center justify-between mb-4">
            <SectionHeader
              title="Trending skills"
              sub={`Top skills in job postings over the last ${skillDays} days`}
            />
            <div className="flex gap-2 shrink-0">
              {[7, 14, 30, 60].map((d) => (
                <button
                  key={d}
                  onClick={() => setSkillDays(d)}
                  className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                    skillDays === d
                      ? "bg-brand text-white border-brand"
                      : "bg-white text-gray-600 border-gray-200 hover:border-brand hover:text-brand"
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>
          </div>
          {loading || !skills ? (
            <div className="flex flex-wrap gap-2 animate-pulse">
              {Array.from({ length: 20 }).map((_, i) => (
                <div
                  key={i}
                  className="h-7 bg-gray-200 rounded-full"
                  style={{ width: `${50 + (i % 5) * 20}px` }}
                />
              ))}
            </div>
          ) : skills.skills.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {skills.skills.map((s, i) => (
                <SkillChip
                  key={s.skill}
                  skill={s.skill}
                  count={s.count}
                  pct={s.pct_of_jobs}
                  rank={i + 1}
                />
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-400">
              Skills data is being computed — check back soon.
            </p>
          )}
          {skills && (
            <p className="text-xs text-gray-400 mt-4">
              Analysed {skills.total_jobs_analyzed.toLocaleString()} jobs · deeper blue = higher demand
            </p>
          )}
        </Card>

        {/* ── Top hiring companies ────────────────────────────────────────── */}
        <Card>
          <SectionHeader
            title="Most active hiring (last 7 days)"
            sub="Companies with the most new job postings this week"
          />
          {loading ? (
            <Skeleton rows={10} />
          ) : hiring.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100">
                    <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-widest py-2 pr-4">
                      Company
                    </th>
                    <th className="text-left text-xs font-semibold text-gray-400 uppercase tracking-widest py-2 pr-4">
                      ATS
                    </th>
                    <th className="text-right text-xs font-semibold text-gray-400 uppercase tracking-widest py-2 pr-4">
                      Jobs (7d)
                    </th>
                    <th className="text-right text-xs font-semibold text-gray-400 uppercase tracking-widest py-2">
                      Last posted
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {hiring.map((c, i) => (
                    <tr key={c.company_id} className="hover:bg-gray-50 transition-colors">
                      <td className="py-2.5 pr-4 font-medium text-gray-900">
                        <span className="text-gray-300 text-xs mr-2">{i + 1}</span>
                        {c.company_name}
                      </td>
                      <td className="py-2.5 pr-4">
                        <span
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                                     text-[11px] font-medium border bg-gray-50 text-gray-600 border-gray-200"
                        >
                          <span
                            className={`w-1.5 h-1.5 rounded-full ${ATS_COLOURS[c.ats_type ?? "unknown"] ?? "bg-gray-400"}`}
                          />
                          {c.ats_type ?? "unknown"}
                        </span>
                      </td>
                      <td className="py-2.5 pr-4 text-right font-semibold text-orange-600">
                        {c.jobs_count.toLocaleString()}
                      </td>
                      <td className="py-2.5 text-right text-gray-400 text-xs">
                        {c.last_posted
                          ? new Date(c.last_posted).toLocaleDateString("en-US", { month: "short", day: "numeric" })
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-sm text-gray-400">
              No hiring data yet — jobs are still being indexed.
            </p>
          )}
        </Card>

      </div>
    </div>
  );
}
