'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

export default function MarketPage() {
  const [data, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<any>('/reports/weekly-market')
      .then(d => setData(d))
      .catch(err => { setData(null); setError(err.message) })
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">📈 Market Trends</h1>
          <p className="text-[#8888aa] mb-6">AI/Data job market intelligence — last 7 days</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? <p className="text-[#8888aa]">Loading…</p> : !data ? (
            <p className="text-[#8888aa]">No market data available yet. Jobs need to be ingested first.</p>
          ) : (
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <div className="card">
                <h2 className="font-semibold text-white mb-4">🏢 Top Hiring Companies</h2>
                {(data.top_hiring_companies || []).slice(0, 10).map((c: any) => (
                  <div key={c.company} className="flex justify-between py-2 border-b border-[#2e2e4a]/50">
                    <span className="text-sm text-white">{c.company}</span>
                    <span className="text-sm text-indigo-400 font-medium">{c.job_count} jobs</span>
                  </div>
                ))}
              </div>

              <div className="card">
                <h2 className="font-semibold text-white mb-4">🎯 Top Roles</h2>
                {(data.top_roles || []).slice(0, 8).map((r: any) => (
                  <div key={r.role} className="flex justify-between py-2 border-b border-[#2e2e4a]/50">
                    <span className="text-sm text-white">{r.role}</span>
                    <span className="text-sm text-indigo-400 font-medium">{r.count}</span>
                  </div>
                ))}
              </div>

              <div className="card">
                <h2 className="font-semibold text-white mb-4">💰 Salary Ranges by Role</h2>
                {(data.salary_ranges_by_role || []).slice(0, 6).map((s: any) => s.avg_min_salary > 0 && (
                  <div key={s.role} className="py-2 border-b border-[#2e2e4a]/50">
                    <div className="flex justify-between">
                      <span className="text-sm text-white">{s.role}</span>
                      <span className="text-sm text-emerald-400">
                        ${(s.avg_min_salary / 1000).toFixed(0)}k–${(s.avg_max_salary / 1000).toFixed(0)}k
                      </span>
                    </div>
                    <span className="text-xs text-[#8888aa]">{s.sample_size} jobs sampled</span>
                  </div>
                ))}
              </div>

              <div className="card">
                <h2 className="font-semibold text-white mb-4">🌐 Remote Work Trends</h2>
                {Object.entries(data.remote_trends || {}).map(([type, count]) => (
                  <div key={type} className="flex justify-between py-2 border-b border-[#2e2e4a]/50">
                    <span className="text-sm text-white capitalize">{type || 'unknown'}</span>
                    <span className="text-sm text-indigo-400">{count as number}</span>
                  </div>
                ))}
                {data.remote_percentage != null && (
                  <p className="text-emerald-400 text-sm mt-3 font-medium">{data.remote_percentage}% Remote</p>
                )}
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
