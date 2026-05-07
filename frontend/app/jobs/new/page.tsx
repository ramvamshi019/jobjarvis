'use client'
import { useEffect, useState } from 'react'
import { api, type Job } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import { JobCard } from '@/components/jobs/JobCard'

export default function NewJobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [hours, setHours] = useState(24)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    setLoading(true)
    setError('')
    api.get<Job[]>(`/jobs/new?hours=${hours}&page_size=50`)
      .then(data => setJobs(data))
      .catch(err => {
        setJobs([])
        setError(err.message)
      })
      .finally(() => setLoading(false))
  }, [hours])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8">
        <div className="max-w-5xl mx-auto">
          <div className="flex items-center justify-between mb-6">
            <h1 className="text-2xl font-bold text-white">⚡ New Jobs</h1>
            <select
              value={hours}
              onChange={e => setHours(Number(e.target.value))}
              className="bg-[#22223a] border border-[#2e2e4a] text-[#e2e2f0] rounded-lg px-3 py-2 text-sm"
            >
              <option value={1}>Last hour</option>
              <option value={6}>Last 6 hours</option>
              <option value={24}>Last 24 hours</option>
              <option value={72}>Last 3 days</option>
            </select>
          </div>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          <p className="text-[#8888aa] mb-4">{jobs.length} jobs in the last {hours}h</p>

          {loading ? (
            <p className="text-[#8888aa]">Loading…</p>
          ) : (
            <div className="grid gap-3">
              {jobs.map(j => <JobCard key={j.id} job={j} />)}
              {jobs.length === 0 && !error && (
                <p className="text-[#8888aa]">No new jobs in this range.</p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
