'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import Link from 'next/link'

export default function ApplyNowPage() {
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<any[]>('/jobs/apply-now?page_size=50')
      .then(data => setItems(data))
      .catch(err => {
        setItems([])
        setError(err.message)
      })
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">🚀 Apply Now</h1>
          <p className="text-[#8888aa] mb-6">AI-recommended jobs to apply immediately</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? (
            <p className="text-[#8888aa]">Loading…</p>
          ) : (
            <div className="grid gap-3">
              {items.map(item => (
                <Link key={item.job_id} href={`/jobs/${item.job_id}`}>
                  <div className="card hover:border-emerald-500/50 transition-all cursor-pointer">
                    <div className="flex items-start justify-between">
                      <div>
                        <h3 className="font-semibold text-white">{item.title}</h3>
                        <p className="text-[#8888aa] text-sm">{item.company}</p>
                      </div>
                      <div className="text-right ml-4">
                        <div className="text-lg font-bold text-emerald-400">{item.fit_score?.toFixed(0)}%</div>
                        <span className={`text-xs font-bold ${item.priority === 'HIGH' ? 'text-red-400' : 'text-amber-400'}`}>
                          {item.priority}
                        </span>
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-1 mt-3">
                      {(item.matched_skills || []).slice(0, 5).map((s: string) => (
                        <span key={s} className="text-xs bg-emerald-900/30 text-emerald-400 px-2 py-0.5 rounded">{s}</span>
                      ))}
                    </div>
                    <p className="text-xs text-amber-400 mt-2">⏰ Apply within {item.apply_within_hours}h</p>
                  </div>
                </Link>
              ))}
              {items.length === 0 && !error && (
                <div className="card text-center py-12">
                  <p className="text-[#8888aa]">No apply-now jobs. Run the AI Agent first.</p>
                  <Link href="/agent" className="mt-4 inline-block bg-indigo-600 text-white px-6 py-2 rounded-lg text-sm">
                    Run AI Agent
                  </Link>
                </div>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
