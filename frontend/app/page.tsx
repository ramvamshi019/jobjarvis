'use client'
import { useState, useEffect } from 'react'
import { login, signup, setToken, getToken } from '@/lib/api'
import { useRouter } from 'next/navigation'

export default function AuthPage() {
  const [mode, setMode] = useState<'login' | 'signup'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const router = useRouter()

  // Already logged in → skip auth page entirely
  useEffect(() => {
    if (getToken()) router.replace('/dashboard')
  }, [router])

  const switchMode = (m: 'login' | 'signup') => {
    setMode(m)
    setError('')
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      const res = mode === 'login'
        ? await login(email, password)
        : await signup(email, password, name)

      setToken(res.access_token)
      localStorage.setItem('jj_user', JSON.stringify({
        id: res.user_id,
        email: res.email,
        role: res.role,
      }))
      router.push('/dashboard')
    } catch (err: any) {
      setError(err.message || 'Something went wrong. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0f0f14] px-4">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <h1 className="text-4xl font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent">
            JobJarvis
          </h1>
          <p className="text-[#8888aa] mt-2">Autonomous AI Career Intelligence Platform</p>
        </div>

        <div className="card">
          <div className="flex gap-2 mb-6">
            {(['login', 'signup'] as const).map(m => (
              <button
                key={m}
                onClick={() => switchMode(m)}
                className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all ${
                  mode === m ? 'bg-indigo-600 text-white' : 'text-[#8888aa] hover:text-white'
                }`}
              >
                {m === 'login' ? 'Sign In' : 'Sign Up'}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="space-y-4">
            {mode === 'signup' && (
              <input
                className="input w-full"
                placeholder="Full Name (optional)"
                value={name}
                onChange={e => setName(e.target.value)}
              />
            )}
            <input
              className="input w-full"
              type="email"
              placeholder="Email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              required
            />
            <input
              className="input w-full"
              type="password"
              placeholder="Password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              required
            />

            {error && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg px-4 py-3">
                <p className="text-red-400 text-sm">{error}</p>
              </div>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full py-2.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded-lg text-white font-medium transition-all"
            >
              {loading ? 'Loading…' : mode === 'login' ? 'Sign In' : 'Create Account'}
            </button>
          </form>
        </div>
      </div>

      <style>{`
        .input {
          background: #22223a;
          border: 1px solid #2e2e4a;
          border-radius: 8px;
          padding: 10px 14px;
          color: #e2e2f0;
          outline: none;
          width: 100%;
        }
        .input:focus { border-color: #6366f1; }
      `}</style>
    </div>
  )
}
