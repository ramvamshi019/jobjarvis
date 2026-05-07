'use client'
import { useEffect, useState } from 'react'
import { api, getToken, type WeeklyPlan } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import Link from 'next/link'
import { useRouter } from 'next/navigation'

export default function DashboardPage() {
  const [plan, setPlan] = useState<WeeklyPlan | null>(null)
  const [obs, setObs] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const router = useRouter()

  const [hasToken, setHasToken] = useState(false)

  useEffect(() => {
    if (!getToken()) {
      router.replace('/')
    } else {
      setHasToken(true)
    }
  }, [router])

  useEffect(() => {
    if (!hasToken) return

    Promise.all([
      api.get<WeeklyPlan>('/agent/weekly-plan').catch(err => { console.error(err); return null }),
      api.get<any>('/observability/summary').catch(err => { console.error(err); return null }),
    ]).then(([p, o]) => {
      setPlan(p)
      setObs(o)
    }).catch((err: any) => {
      const msg: string = err?.message ?? ''
      if (msg.includes('Not authenticated') || msg.includes('401')) {
        router.replace('/')
      } else {
        setError(msg || 'Failed to load dashboard data.')
      }
    }).finally(() => setLoading(false))
  }, [hasToken, router])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-6xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">Dashboard</h1>
          <p className="text-[#8888aa] mb-6">Your AI-powered career intelligence overview</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-6">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? (
            <div className="text-[#8888aa]">Loading…</div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
              <StatCard label="Jobs Scanned Today" value={obs?.jobs_found_today ?? '—'} icon="💼" color="indigo" />
              <StatCard label="Active Companies"   value={obs?.total_companies    ?? '—'} icon="🏢" color="blue" />
              <StatCard
                label="AI Cost Today"
                value={obs ? `$${(obs.ai_cost_today_usd ?? 0).toFixed(3)}` : '—'}
                icon="💰"
                color="amber"
              />
              <StatCard
                label="System Health"
                value={obs === null ? 'Unavailable' : obs.system_healthy === true ? 'Healthy' : 'Alert'}
                icon="🔧"
                color={obs === null ? 'indigo' : obs.system_healthy === true ? 'emerald' : 'red'}
              />
            </div>
          )}

          {plan && (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <div className="card">
                <h2 className="font-semibold text-white mb-3">🎯 This Week's Goal</h2>
                <p className="text-indigo-300 font-medium">{plan.weekly_goal}</p>

                {plan.priority_roles.length > 0 && (
                  <div className="mt-4">
                    <p className="text-[#8888aa] text-sm mb-2">Priority Roles</p>
                    <div className="flex flex-wrap gap-2">
                      {plan.priority_roles.map(r => (
                        <span key={r} className="text-xs bg-indigo-600/20 text-indigo-300 px-3 py-1 rounded-full">{r}</span>
                      ))}
                    </div>
                  </div>
                )}

                {plan.skills_to_improve.length > 0 && (
                  <div className="mt-4">
                    <p className="text-[#8888aa] text-sm mb-2">Skills to Improve</p>
                    <div className="flex flex-wrap gap-2">
                      {plan.skills_to_improve.slice(0, 5).map(s => (
                        <span key={s} className="text-xs bg-amber-600/20 text-amber-300 px-3 py-1 rounded-full">{s}</span>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              <div className="card">
                <h2 className="font-semibold text-white mb-3">🚀 Top Apply Targets</h2>
                {plan.application_targets.length === 0 ? (
                  <p className="text-[#8888aa] text-sm">Run the AI Agent to get targets.</p>
                ) : (
                  <div className="space-y-3">
                    {plan.application_targets.slice(0, 5).map(t => (
                      <Link
                        key={t.job_id}
                        href={`/jobs/${t.job_id}`}
                        className="flex items-center justify-between hover:bg-white/5 p-2 rounded-lg transition-colors"
                      >
                        <div>
                          <p className="text-sm font-medium text-white">{t.title}</p>
                          <p className="text-xs text-[#8888aa]">{t.company}</p>
                        </div>
                        <div className="text-right">
                          <p className="text-xs font-bold text-emerald-400">{t.fit_score?.toFixed(0)}% fit</p>
                          <p className="text-xs text-[#8888aa]">Apply in {t.apply_within_hours}h</p>
                        </div>
                      </Link>
                    ))}
                  </div>
                )}
              </div>

              {plan.project_recommendations.length > 0 && (
                <div className="card col-span-1 lg:col-span-2">
                  <h2 className="font-semibold text-white mb-3">🔨 Recommended Portfolio Projects</h2>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {plan.project_recommendations.slice(0, 4).map((p, i) => (
                      <div key={i} className="bg-[#22223a] p-3 rounded-lg">
                        <p className="text-sm text-[#e2e2f0]">{p}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          {!loading && !plan && !error && (
            <div className="card text-center py-12">
              <p className="text-[#8888aa] mb-4">No weekly plan yet. Run the AI Agent to generate your first plan.</p>
              <Link href="/agent" className="bg-indigo-600 text-white px-6 py-2 rounded-lg text-sm hover:bg-indigo-500 transition-all">
                Go to AI Agent →
              </Link>
            </div>
          )}
        </div>
      </main>
    </div>
  )
}

function StatCard({ label, value, icon, color }: {
  label: string; value: any; icon: string; color: string
}) {
  const colors: Record<string, string> = {
    indigo:  'border-indigo-500/30  bg-indigo-500/10',
    blue:    'border-blue-500/30    bg-blue-500/10',
    amber:   'border-amber-500/30   bg-amber-500/10',
    emerald: 'border-emerald-500/30 bg-emerald-500/10',
    red:     'border-red-500/30     bg-red-500/10',
  }
  return (
    <div className={`rounded-xl p-4 border ${colors[color] ?? colors.indigo}`}>
      <div className="text-2xl mb-1">{icon}</div>
      <div className="text-xl font-bold text-white">{value}</div>
      <div className="text-xs text-[#8888aa] mt-0.5">{label}</div>
    </div>
  )
}
