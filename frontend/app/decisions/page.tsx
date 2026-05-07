'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import Link from 'next/link'
import { DecisionBadge } from '@/components/ui/DecisionBadge'

const TYPES = ['', 'APPLY_NOW', 'TAILOR_RESUME_FIRST', 'SAVE_FOR_LATER', 'SKIP', 'HIGH_RISK', 'REVIEW_NEEDED']

export default function DecisionsPage() {
  const [decisions, setDecisions] = useState<any[]>([])
  const [filter, setFilter] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    const path = filter ? `/jobs/decisions?decision_type=${filter}` : '/jobs/decisions'
    api.get<any[]>(path)
      .then(data => setDecisions(data))
      .catch(err => {
        setDecisions([])
        setError(err.message)
      })
      .finally(() => setLoading(false))
  }, [filter])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-6">🧠 AI Decisions</h1>

          <div className="flex flex-wrap gap-2 mb-6">
            {TYPES.map(t => (
              <button
                key={t}
                onClick={() => setFilter(t)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                  filter === t ? 'bg-indigo-600 text-white' : 'bg-[#22223a] text-[#8888aa] hover:text-white'
                }`}
              >
                {t || 'All'}
              </button>
            ))}
          </div>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? (
            <p className="text-[#8888aa]">Loading…</p>
          ) : (
            <div className="grid gap-3">
              {decisions.map(d => (
                <Link key={d.id} href={`/jobs/${d.job_id}`}>
                  <div className="card hover:border-indigo-500/50 transition-all cursor-pointer">
                    <div className="flex items-center justify-between">
                      <div>
                        <span className="text-[#8888aa] text-sm">Job #{d.job_id}</span>
                        <p className="text-xs text-[#8888aa] mt-0.5">{d.role_category}</p>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className="text-sm font-bold text-white">{d.fit_score?.toFixed(0)}%</span>
                        <DecisionBadge decision={d.decision} />
                      </div>
                    </div>
                  </div>
                </Link>
              ))}
              {decisions.length === 0 && !error && (
                <p className="text-[#8888aa]">No decisions yet. Run the AI Agent first.</p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
