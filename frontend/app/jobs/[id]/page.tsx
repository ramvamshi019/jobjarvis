'use client'
import { useEffect, useState } from 'react'
import { api, type Job, type AIDecision } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'
import { DecisionBadge } from '@/components/ui/DecisionBadge'
import { useParams } from 'next/navigation'

export default function JobDetailPage() {
  const params = useParams()
  const id = params?.id as string
  const [job, setJob] = useState<Job | null>(null)
  const [decision, setDecision] = useState<AIDecision | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (!id) return
    Promise.all([
      api.get<Job>(`/jobs/${id}`).catch(err => { console.error(err); return null }),
      api.get<AIDecision>(`/jobs/${id}/decision`).catch(err => { console.error(err); return null }),
    ]).then(([j, d]) => { setJob(j); setDecision(d); setLoading(false) })
  }, [id])

  if (loading) return <div className="flex min-h-screen"><Sidebar /><main className="flex-1 p-8 text-[#8888aa]">Loading...</main></div>
  if (!job) return <div className="flex min-h-screen"><Sidebar /><main className="flex-1 p-8 text-[#8888aa]">Job not found.</main></div>

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-4xl mx-auto">
          <div className="flex items-start justify-between mb-6">
            <div>
              <h1 className="text-2xl font-bold text-white">{job.title}</h1>
              <p className="text-[#8888aa] text-lg">{job.company_name}</p>
              <div className="flex gap-2 mt-2">
                {job.normalized_location && <span className="text-sm text-[#8888aa]">📍 {job.normalized_location}</span>}
                {job.remote_type && <span className="text-sm text-[#8888aa]">• {job.remote_type}</span>}
                {job.experience_level && <span className="text-sm text-[#8888aa]">• {job.experience_level}</span>}
              </div>
            </div>
            {decision && <DecisionBadge decision={decision.decision} />}
          </div>

          {decision && (
            <div className="card mb-6">
              <h2 className="font-semibold text-white mb-4">🧠 AI Decision</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
                <Score label="Fit Score" value={`${decision.fit_score?.toFixed(0)}%`} />
                <Score label="Confidence" value={`${((decision.confidence || 0) * 100).toFixed(0)}%`} />
                <Score label="Interview Prob." value={`${((decision.interview_probability || 0) * 100).toFixed(0)}%`} />
                <Score label="Apply Within" value={`${decision.apply_within_hours}h`} />
              </div>
              {decision.why_apply?.length > 0 && (
                <div className="mb-3">
                  <p className="text-xs text-[#8888aa] mb-1">Why Apply</p>
                  {decision.why_apply.map((w, i) => <p key={i} className="text-sm text-emerald-400">✓ {w}</p>)}
                </div>
              )}
              {decision.why_not?.length > 0 && (
                <div className="mb-3">
                  <p className="text-xs text-[#8888aa] mb-1">Concerns</p>
                  {decision.why_not.map((w, i) => <p key={i} className="text-sm text-amber-400">⚠ {w}</p>)}
                </div>
              )}
              {decision.application_strategy && (
                <div className="bg-[#22223a] p-3 rounded-lg">
                  <p className="text-xs text-[#8888aa] mb-1">Strategy</p>
                  <p className="text-sm text-white">{decision.application_strategy}</p>
                </div>
              )}
              <div className="flex flex-wrap gap-2 mt-4">
                {decision.matched_skills?.map(s => (
                  <span key={s} className="text-xs bg-emerald-900/30 text-emerald-400 px-2 py-0.5 rounded">✓ {s}</span>
                ))}
                {decision.missing_skills?.map(s => (
                  <span key={s} className="text-xs bg-red-900/30 text-red-400 px-2 py-0.5 rounded">✗ {s}</span>
                ))}
              </div>
            </div>
          )}

          <div className="card">
            <h2 className="font-semibold text-white mb-4">Job Details</h2>
            {job.salary_min && (
              <p className="text-sm text-emerald-400 mb-3">
                💰 ${(job.salary_min / 1000).toFixed(0)}k – ${((job.salary_max || job.salary_min * 1.3) / 1000).toFixed(0)}k {job.salary_currency}
              </p>
            )}
            {job.job_url && (
              <a href={job.job_url} target="_blank" rel="noopener noreferrer"
                className="inline-block bg-indigo-600 text-white px-4 py-2 rounded-lg text-sm mb-4 hover:bg-indigo-500">
                View Original Posting →
              </a>
            )}
            <div className="flex flex-wrap gap-1 mb-4">
              {[...(job.required_skills || []), ...(job.preferred_skills || [])].slice(0, 12).map(s => (
                <span key={s} className="text-xs bg-[#22223a] text-[#8888aa] px-2 py-0.5 rounded">{s}</span>
              ))}
            </div>
          </div>
        </div>
      </main>
    </div>
  )
}

function Score({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-[#22223a] p-3 rounded-lg">
      <p className="text-xs text-[#8888aa]">{label}</p>
      <p className="text-lg font-bold text-white">{value}</p>
    </div>
  )
}
