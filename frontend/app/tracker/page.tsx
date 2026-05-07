'use client'
import { useEffect, useState } from 'react'
import { api, type Application } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

const STATUSES = ['saved', 'applied', 'recruiter_contacted', 'interview', 'rejected', 'offer', 'closed']
const STATUS_COLORS: Record<string, string> = {
  saved:               'bg-blue-500/20 text-blue-400',
  applied:             'bg-indigo-500/20 text-indigo-400',
  recruiter_contacted: 'bg-purple-500/20 text-purple-400',
  interview:           'bg-emerald-500/20 text-emerald-400',
  rejected:            'bg-red-500/20 text-red-400',
  offer:               'bg-yellow-500/20 text-yellow-400',
  closed:              'bg-gray-500/20 text-gray-400',
}

export default function TrackerPage() {
  const [apps, setApps] = useState<Application[]>([])
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    const path = filter ? `/applications?status=${filter}` : '/applications'
    api.get<Application[]>(path)
      .then(data => setApps(data))
      .catch(err => { setApps([]); setError(err.message) })
      .finally(() => setLoading(false))
  }, [filter])

  const counts: Record<string, number> = {}
  apps.forEach(a => { counts[a.status] = (counts[a.status] || 0) + 1 })

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-6">📋 Application Tracker</h1>

          <div className="flex flex-wrap gap-2 mb-6">
            <button
              onClick={() => setFilter('')}
              className={`py-2 px-3 rounded-lg text-xs font-medium transition-all ${
                filter === '' ? 'bg-indigo-600 text-white' : 'bg-[#22223a] text-[#8888aa] hover:text-white'
              }`}
            >
              All {apps.length > 0 ? `(${apps.length})` : ''}
            </button>
            {STATUSES.map(s => (
              <button key={s} onClick={() => setFilter(filter === s ? '' : s)}
                className={`py-2 px-3 rounded-lg text-xs font-medium transition-all ${
                  filter === s ? 'bg-indigo-600 text-white' : 'bg-[#22223a] text-[#8888aa] hover:text-white'
                }`}>
                {s.replace('_', ' ')} {counts[s] ? `(${counts[s]})` : ''}
              </button>
            ))}
          </div>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? <p className="text-[#8888aa]">Loading…</p> : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[#8888aa] border-b border-[#2e2e4a]">
                    <th className="pb-3 pr-4">Job</th>
                    <th className="pb-3 pr-4">Status</th>
                    <th className="pb-3 pr-4">Applied</th>
                    <th className="pb-3 pr-4">Follow-up</th>
                    <th className="pb-3">Interviews</th>
                  </tr>
                </thead>
                <tbody>
                  {apps.map(app => (
                    <tr key={app.id} className="border-b border-[#2e2e4a]/50 hover:bg-white/2">
                      <td className="py-3 pr-4 text-white">#{app.job_id}</td>
                      <td className="py-3 pr-4">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[app.status] || 'bg-gray-500/20 text-gray-400'}`}>
                          {app.status}
                        </span>
                      </td>
                      <td className="py-3 pr-4 text-[#8888aa]">
                        {app.applied_at ? new Date(app.applied_at).toLocaleDateString() : '—'}
                      </td>
                      <td className="py-3 pr-4 text-[#8888aa]">
                        {app.follow_up_at ? new Date(app.follow_up_at).toLocaleDateString() : '—'}
                      </td>
                      <td className="py-3 text-[#8888aa]">{app.interview_rounds}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {apps.length === 0 && !error && (
                <p className="text-[#8888aa] text-center py-8">No applications yet.</p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
