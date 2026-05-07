'use client'
import { useEffect, useState } from 'react'
import { api, type Job } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import { JobCard } from '@/components/jobs/JobCard'

const ROLES = ['', 'AI Engineer', 'ML Engineer', 'Data Engineer', 'MLOps Engineer', 'Analytics Engineer', 'Backend Engineer', 'QA/SDET']
const REMOTE = ['', 'remote', 'hybrid', 'onsite']
const LEVELS = ['', 'intern', 'entry', 'mid', 'senior']

export default function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([])
  const [loading, setLoading] = useState(true)
  const [role, setRole] = useState('')
  const [remoteType, setRemoteType] = useState('')
  const [level, setLevel] = useState('')
  const [country, setCountry] = useState('')
  const [page, setPage] = useState(1)

  const load = async () => {
    setLoading(true)
    const params = new URLSearchParams()
    if (role) params.set('role_category', role)
    if (remoteType) params.set('remote_type', remoteType)
    if (level) params.set('experience_level', level)
    if (country) params.set('country', country)
    params.set('page', String(page))
    params.set('page_size', '25')
    const data = await api.get<Job[]>(`/jobs?${params}`).catch((err) => {
      console.error(err)
      return []
    })
    setJobs(data)
    setLoading(false)
  }

  useEffect(() => { load() }, [role, remoteType, level, country, page])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8">
        <div className="max-w-5xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-6">All Jobs</h1>
          <div className="flex flex-wrap gap-3 mb-6">
            <Select value={role} onChange={setRole} options={ROLES} label="Role" />
            <Select value={remoteType} onChange={setRemoteType} options={REMOTE} label="Remote" />
            <Select value={level} onChange={setLevel} options={LEVELS} label="Level" />
            <Select value={country} onChange={setCountry} options={['', 'US', 'IN', 'UK', 'CA']} label="Country" />
          </div>
          {loading ? <p className="text-[#8888aa]">Loading...</p> : (
            <div className="grid gap-3">
              {jobs.map(j => <JobCard key={j.id} job={j} decision={j.decision} fitScore={j.fit_score} />)}
              {jobs.length === 0 && <p className="text-[#8888aa]">No jobs found.</p>}
            </div>
          )}
          <div className="flex gap-3 mt-6">
            <button onClick={() => setPage(Math.max(1, page - 1))} disabled={page === 1}
              className="px-4 py-2 bg-[#22223a] text-white rounded-lg disabled:opacity-40">← Prev</button>
            <span className="text-[#8888aa] self-center">Page {page}</span>
            <button onClick={() => setPage(page + 1)} disabled={jobs.length < 25}
              className="px-4 py-2 bg-[#22223a] text-white rounded-lg disabled:opacity-40">Next →</button>
          </div>
        </div>
      </main>
    </div>
  )
}

function Select({ value, onChange, options, label }: { value: string; onChange: (v: string) => void; options: string[]; label: string }) {
  return (
    <select value={value} onChange={e => onChange(e.target.value)}
      className="bg-[#22223a] border border-[#2e2e4a] text-[#e2e2f0] rounded-lg px-3 py-2 text-sm">
      {options.map(o => <option key={o} value={o}>{o || `All ${label}s`}</option>)}
    </select>
  )
}
