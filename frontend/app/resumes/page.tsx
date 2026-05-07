'use client'
import { useEffect, useRef, useState } from 'react'
import { api, BASE_URL, getToken } from '@/lib/api'
import Sidebar from '@/components/layout/Sidebar'

export default function ResumesPage() {
  const [resumes, setResumes] = useState<any[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState('')
  const [loadError, setLoadError] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)

  const load = async () => {
    try {
      const data = await api.get<any[]>('/resumes')
      setResumes(data)
      setLoadError('')
    } catch (err: any) {
      setResumes([])
      setLoadError(err.message)
    }
  }

  useEffect(() => { load() }, [])

  const upload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setUploadError('')
    const fd = new FormData()
    fd.append('file', file)
    const token = getToken()
    try {
      // Must use raw fetch for multipart/form-data — the api client always sets
      // Content-Type: application/json which would break the file upload.
      // BASE_URL is imported so it matches the same source-of-truth as api.ts.
      const res = await fetch(`${BASE_URL}/api/resumes/upload`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        throw new Error(body.detail || 'Upload failed')
      }
      await load()
      // Reset input so the same file can be re-uploaded if needed
      if (fileRef.current) fileRef.current.value = ''
    } catch (err: any) {
      setUploadError(err.message)
    } finally {
      setUploading(false)
    }
  }

  const activate = async (id: number) => {
    try {
      await api.patch(`/resumes/${id}/activate`)
      await load()
    } catch (err: any) {
      setLoadError(err.message)
    }
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8 overflow-y-auto">
        <div className="max-w-4xl mx-auto">
          <div className="flex items-center justify-between mb-6">
            <h1 className="text-2xl font-bold text-white">📄 Resume Versions</h1>
            <button
              onClick={() => fileRef.current?.click()}
              disabled={uploading}
              className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white px-4 py-2 rounded-lg text-sm transition-all"
            >
              {uploading ? 'Uploading…' : '+ Upload Resume'}
            </button>
            <input ref={fileRef} type="file" accept=".pdf,.docx,.txt" className="hidden" onChange={upload} />
          </div>

          {uploadError && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">Upload failed: {uploadError}</p>
            </div>
          )}
          {loadError && (
            <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3 mb-4">
              <p className="text-red-400 text-sm">{loadError}</p>
            </div>
          )}

          <div className="grid gap-4">
            {resumes.map(r => (
              <div key={r.id} className={`card ${r.is_active ? 'border-emerald-500/50' : ''}`}>
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="font-semibold text-white">{r.name}</h3>
                    {r.target_role && <p className="text-[#8888aa] text-sm">Target: {r.target_role}</p>}
                    <p className="text-xs text-[#8888aa]">
                      Strength: {r.overall_strength_score?.toFixed(0) || '—'}/100 • {r.experience_level}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    {r.is_active ? (
                      <span className="text-xs bg-emerald-500/20 text-emerald-400 px-2 py-1 rounded">Active</span>
                    ) : (
                      <button
                        onClick={() => activate(r.id)}
                        className="text-xs bg-[#22223a] hover:bg-indigo-600 text-[#8888aa] hover:text-white px-3 py-1 rounded transition-all"
                      >
                        Set Active
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
            {resumes.length === 0 && !loadError && (
              <p className="text-[#8888aa]">No resumes uploaded. Upload a PDF or DOCX to get started.</p>
            )}
          </div>
        </div>
      </main>
    </div>
  )
}
