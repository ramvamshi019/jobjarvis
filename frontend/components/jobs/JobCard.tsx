'use client'
import Link from 'next/link'
import type { Job } from '@/lib/api'
import { DecisionBadge } from '@/components/ui/DecisionBadge'

interface Props {
  job: Job
  decision?: string
  fitScore?: number
}

const FRESH_COLORS: Record<string, string> = {
  new_last_hour: 'text-emerald-400',
  new_last_6_hours: 'text-green-400',
  new_today: 'text-lime-400',
  new_last_3_days: 'text-yellow-400',
  stale: 'text-gray-500',
}

const FRESH_LABELS: Record<string, string> = {
  new_last_hour: '< 1h ago',
  new_last_6_hours: '< 6h ago',
  new_today: 'Today',
  new_last_3_days: '< 3 days',
  stale: 'Older',
}

export function JobCard({ job, decision, fitScore }: Props) {
  return (
    <Link href={`/jobs/${job.id}`}>
      <div className="card hover:border-indigo-500/50 transition-all cursor-pointer group">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <h3 className="font-semibold text-white truncate group-hover:text-indigo-300 transition-colors">
              {job.title}
            </h3>
            <p className="text-[#8888aa] text-sm mt-0.5">{job.company_name}</p>
          </div>
          <div className="flex flex-col items-end gap-1.5 shrink-0">
            {decision && <DecisionBadge decision={decision} />}
            {fitScore != null && (
              <span className={`text-xs font-bold ${fitScore >= 75 ? 'text-emerald-400' : fitScore >= 55 ? 'text-amber-400' : 'text-gray-500'}`}>
                {fitScore.toFixed(0)}% fit
              </span>
            )}
          </div>
        </div>

        <div className="flex flex-wrap gap-2 mt-3">
          {job.normalized_location && (
            <span className="text-xs bg-[#22223a] px-2 py-0.5 rounded text-[#8888aa]">
              📍 {job.normalized_location}
            </span>
          )}
          {job.remote_type && (
            <span className="text-xs bg-[#22223a] px-2 py-0.5 rounded text-[#8888aa]">
              {job.remote_type === 'remote' ? '🌐 Remote' : job.remote_type === 'hybrid' ? '🔀 Hybrid' : '🏢 Onsite'}
            </span>
          )}
          {job.experience_level && (
            <span className="text-xs bg-[#22223a] px-2 py-0.5 rounded text-[#8888aa]">
              {job.experience_level}
            </span>
          )}
          {job.salary_min && (
            <span className="text-xs bg-[#22223a] px-2 py-0.5 rounded text-emerald-600">
              ${(job.salary_min / 1000).toFixed(0)}k–${((job.salary_max || job.salary_min * 1.3) / 1000).toFixed(0)}k
            </span>
          )}
        </div>

        <div className="flex items-center justify-between mt-3">
          <div className="flex flex-wrap gap-1">
            {(job.required_skills || []).slice(0, 4).map(s => (
              <span key={s} className="text-xs bg-indigo-900/30 text-indigo-400 px-1.5 py-0.5 rounded">
                {s}
              </span>
            ))}
          </div>
          <span className={`text-xs ${FRESH_COLORS[job.freshness_label || 'stale'] || 'text-gray-500'}`}>
            {FRESH_LABELS[job.freshness_label || 'stale'] || ''}
          </span>
        </div>
      </div>
    </Link>
  )
}
