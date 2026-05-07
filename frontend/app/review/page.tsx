'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import Link from 'next/link'

export default function ReviewPage() {
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<any[]>('/agent/review-queue')
      .then(data => setItems(data))
      .catch(err => { setItems([]); setError(err.message) })
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">👁️ Human Review Queue</h1>
          <p className="text-[#8888aa] mb-6">Jobs where AI confidence is too low — needs your judgment</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? <p className="text-[#8888aa]">Loading…</p> : (
            <div className="grid gap-4">
              {items.map(item => (
                <div key={item.id} className="card border-violet-500/30">
                  <div className="flex items-start justify-between mb-3">
                    <div>
                      <span className="text-xs text-violet-400 font-medium">REVIEW NEEDED</span>
                      <p className="text-sm text-[#8888aa] mt-1">{item.reason}</p>
                    </div>
                    <span className="text-sm text-amber-400">
                      Confidence: {((item.confidence || 0) * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="flex gap-3">
                    <Link href={`/jobs/${item.job_id}`}
                      className="bg-indigo-600 text-white px-4 py-1.5 rounded-lg text-sm hover:bg-indigo-500 transition-all">
                      View Job #{item.job_id}
                    </Link>
                    <p className="text-xs text-[#8888aa] self-center">
                      {new Date(item.created_at).toLocaleDateString()}
                    </p>
                  </div>
                </div>
              ))}
              {items.length === 0 && !error && (
                <p className="text-[#8888aa]">Queue is empty — great job!</p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
