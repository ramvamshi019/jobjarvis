'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

export default function AdminPage() {
  const [health, setHealth] = useState<any>(null)
  const [failing, setFailing] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retryMsg, setRetryMsg] = useState('')

  useEffect(() => {
    Promise.all([
      api.get<any>('/admin/system-health').catch(() => null),
      api.get<any[]>('/admin/companies/failing').catch(() => []),
    ]).then(([h, f]) => {
      setHealth(h)
      setFailing(f || [])
    }).catch(err => {
      setError(err.message)
    }).finally(() => setLoading(false))
  }, [])

  const retry = async (id: number) => {
    setRetryMsg('')
    try {
      await api.post(`/admin/companies/${id}/retry`)
      setRetryMsg(`Company ${id} queued for retry.`)
    } catch (err: any) {
      setRetryMsg(`Retry failed: ${err.message}`)
    }
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-6">⚙️ Admin Panel</h1>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-6">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {retryMsg && (
            <div className={`rounded-lg px-4 py-3 mb-4 border text-sm ${
              retryMsg.startsWith('Retry failed')
                ? 'bg-red-500/10 border-red-500/30 text-red-400'
                : 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400'
            }`}>
              {retryMsg}
            </div>
          )}

          {loading ? <p className="text-[#8888aa]">Loading…</p> : (
            <div className="grid gap-6">
              {health && (
                <div className="card">
                  <h2 className="font-semibold text-white mb-4">System Health</h2>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                    {([
                      ['Active Jobs',  health.active_jobs_total],
                      ['Jobs Today',   health.jobs_found_today],
                      ['Failing',      health.failing_companies],
                      ['API Success',  `${health.api_success_rate_pct}%`],
                    ] as [string, any][]).map(([k, v]) => (
                      <div key={k} className="bg-[#22223a] p-3 rounded-lg">
                        <p className="text-xs text-[#8888aa]">{k}</p>
                        <p className="text-xl font-bold text-white">{v}</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="card">
                <h2 className="font-semibold text-white mb-4">
                  ⚠️ Failing Companies ({failing.length})
                </h2>
                {failing.length === 0 ? (
                  <p className="text-[#8888aa] text-sm">No failing companies.</p>
                ) : (
                  <div className="space-y-2">
                    {failing.slice(0, 20).map(c => (
                      <div key={c.id} className="flex items-center justify-between bg-[#22223a] p-3 rounded-lg">
                        <div>
                          <p className="text-sm font-medium text-white">{c.name}</p>
                          <p className="text-xs text-[#8888aa]">
                            {c.ats_type} • {c.consecutive_failures} consecutive failures
                          </p>
                        </div>
                        <button
                          onClick={() => retry(c.id)}
                          className="bg-amber-600 hover:bg-amber-500 text-white px-3 py-1 rounded text-xs transition-all"
                        >
                          Retry
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
