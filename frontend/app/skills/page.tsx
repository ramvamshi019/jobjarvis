'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

export default function SkillsPage() {
  const [gaps, setGaps] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get<any[]>('/reports/skill-gaps')
      .then(data => setGaps(data))
      .catch(err => { setGaps([]); setError(err.message) })
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">📊 Skill Gaps</h1>
          <p className="text-[#8888aa] mb-6">Skills you're missing from high-fit jobs</p>

          {error && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{error}</p>
            </div>
          )}

          {loading ? <p className="text-[#8888aa]">Loading…</p> : (
            <div className="grid gap-4">
              {gaps.map(gap => (
                <div key={gap.skill} className="card">
                  <div className="flex items-start justify-between mb-3">
                    <div>
                      <h3 className="font-semibold text-white text-lg">{gap.skill}</h3>
                      <p className="text-[#8888aa] text-sm">Appears in {gap.frequency} relevant jobs</p>
                    </div>
                    <span className={`text-xs font-bold px-3 py-1 rounded-full ${
                      gap.importance === 'HIGH'   ? 'bg-red-500/20 text-red-400' :
                      gap.importance === 'MEDIUM' ? 'bg-amber-500/20 text-amber-400' :
                                                    'bg-gray-500/20 text-gray-400'
                    }`}>{gap.importance}</span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <p className="text-xs text-[#8888aa] mb-1">Learning Plan</p>
                      <p className="text-sm text-white">{gap.learning_plan}</p>
                    </div>
                    <div>
                      <p className="text-xs text-[#8888aa] mb-1">Portfolio Project</p>
                      <p className="text-sm text-indigo-300">{gap.project_suggestion}</p>
                    </div>
                  </div>
                  <div className="flex items-center justify-between mt-3 pt-3 border-t border-[#2e2e4a]">
                    <p className="text-xs text-[#8888aa]">{gap.resume_tip}</p>
                    <p className="text-xs text-[#8888aa]">~{gap.estimated_days} days</p>
                  </div>
                </div>
              ))}
              {gaps.length === 0 && !error && (
                <p className="text-[#8888aa]">No skill gaps detected. Upload a resume and run the AI Agent.</p>
              )}
            </div>
          )}
        </div>
      </main>
    </div>
  )
}
