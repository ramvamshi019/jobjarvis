'use client'
import { useEffect, useState } from 'react'
import { api, type Job } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import { JobCard } from '@/components/jobs/JobCard'

export default function AIDataJobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [page, setPage] = useState(1)

  useEffect(() => {
    setLoading(true)
    setError('')
    // Dedicated backend endpoint — filters by AI/Data role categories server-side
    api.get<Job[]>(`/jobs/ai-data?page=${page}&page_size=25`)
      .then(data => setJobs(data || []))
      .catch(err => { setJobs([]); setError(err.message) })
      .finally(() => setLoading(false))
  }, [page])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">🤖 AI & Data Jobs</h1>
          <p className="text-[#8888aa] mb-6">Curated roles: AI Engineer · ML Engineer · Data Engineer · MLOps · Analytics</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? (
            <p className="text-[#8888aa]">Loading…</p>
          ) : (
            <>
              <div className="grid gap-3">
                {jobs.map(j => <JobCard key={j.id} job={j} />)}
                {jobs.length === 0 && !error && (
                  <div className="text-center py-12 bg-[#22223a] rounded-lg border border-[#2e2e4a]">
                    <p className="text-[#8888aa]">No AI/Data jobs found. Run the ingestion pipeline to fetch jobs.</p>
                  </div>
                )}
              </div>
              <div className="flex gap-3 mt-6">
                <button onClick={() => setPage(Math.max(1, page - 1))} disabled={page === 1}
                  className="px-4 py-2 bg-[#22223a] text-white rounded-lg disabled:opacity-40">← Prev</button>
                <span className="text-[#8888aa] self-center">Page {page}</span>
                <button onClick={() => setPage(page + 1)} disabled={jobs.length < 25}
                  className="px-4 py-2 bg-[#22223a] text-white rounded-lg disabled:opacity-40">Next →</button>
              </div>
            </>
          )}
        </div>
      </main>
    </div>
  )
}
