'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

export default function AgentPage() {
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<any>(null)
  const [memory, setMemory] = useState<any[]>([])
  const [memError, setMemError] = useState('')
  const [runError, setRunError] = useState('')

  useEffect(() => {
    api.get<any[]>('/agent/memory')
      .then(data => setMemory(data))
      .catch(err => { setMemory([]); setMemError(err.message) })
  }, [])

  const runAgent = async () => {
    setRunning(true)
    setRunError('')
    setResult(null)
    try {
      const res = await api.post<any>('/agent/run')
      setResult(res)
    } catch (err: any) {
      setRunError(err.message)
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-4xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-1">🔮 AI Career Agent</h1>
          <p className="text-[#8888aa] mb-6">Autonomous loop: Observe → Analyze → Decide → Act → Learn</p>

          <div className="card mb-6">
            <h2 className="font-semibold text-white mb-4">Run Agent</h2>
            <p className="text-[#8888aa] text-sm mb-4">
              The CareerAgent analyzes all new jobs, makes apply/skip decisions, and updates your memory.
            </p>
            <button
              onClick={runAgent}
              disabled={running}
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-6 py-2.5 rounded-lg font-medium transition-all"
            >
              {running ? '⚙️ Running…' : '▶ Run CareerAgent Now'}
            </button>
            {runError && (
              <div className="mt-3 bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3">
                <p className="text-red-400 text-sm">{runError}</p>
              </div>
            )}
          </div>

          {result && !runError && (
            <div className="card mb-6">
              <h2 className="font-semibold text-white mb-3">Last Run Results</h2>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {([
                  ['Jobs Analyzed', result.jobs_analyzed],
                  ['Apply Now',     result.apply_now],
                  ['Skipped',       result.skip],
                  ['Review Queue',  result.review_queue],
                ] as [string, number][]).map(([k, v]) => (
                  <div key={k} className="bg-[#22223a] p-3 rounded-lg">
                    <p className="text-xs text-[#8888aa]">{k}</p>
                    <p className="text-xl font-bold text-white">{v ?? 0}</p>
                  </div>
                ))}
              </div>
              {result.corrections_applied > 0 && (
                <p className="text-sm text-indigo-300 mt-3">
                  ✓ Applied {result.corrections_applied} self-correction{result.corrections_applied !== 1 ? 's' : ''} from feedback
                </p>
              )}
            </div>
          )}

          <div className="card">
            <h2 className="font-semibold text-white mb-4">AI Memory ({memory.length} items)</h2>

            {memError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
                <p className="text-red-400 text-sm">{memError}</p>
              </div>
            )}

            {memory.length === 0 && !memError ? (
              <p className="text-[#8888aa] text-sm">
                No memories yet. Run the agent and provide feedback to build memory.
              </p>
            ) : (
              <div className="space-y-3">
                {memory.slice(0, 15).map(m => (
                  <div key={m.id} className="bg-[#22223a] p-3 rounded-lg">
                    <div className="flex items-start justify-between">
                      <p className="text-sm text-white">{m.insight}</p>
                      <span className="text-xs text-[#8888aa] ml-3 shrink-0">w:{m.weight?.toFixed(1)}</span>
                    </div>
                    <p className="text-xs text-[#8888aa] mt-1">
                      {m.type} • {new Date(m.created_at).toLocaleDateString()}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
