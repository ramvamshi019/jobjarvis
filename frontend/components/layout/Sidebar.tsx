'use client'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clearToken, getToken } from '@/lib/api'
import { useRouter } from 'next/navigation'
import { useEffect } from 'react'

const NAV = [
  { href: '/dashboard', icon: '🏠', label: 'Dashboard' },
  { href: '/jobs', icon: '💼', label: 'All Jobs' },
  { href: '/jobs/new', icon: '⚡', label: 'New Jobs' },
  { href: '/jobs/apply', icon: '🚀', label: 'Apply Now' },
  { href: '/jobs/ai-data', icon: '🤖', label: 'AI/Data Jobs' },
  { href: '/decisions', icon: '🧠', label: 'AI Decisions' },
  { href: '/skills', icon: '📊', label: 'Skill Gaps' },
  { href: '/resumes', icon: '📄', label: 'Resumes' },
  { href: '/tracker', icon: '📋', label: 'Applications' },
  { href: '/market', icon: '📈', label: 'Market Trends' },
  { href: '/review', icon: '👁️', label: 'Review Queue' },
  { href: '/agent', icon: '🔮', label: 'AI Agent' },
  { href: '/admin', icon: '⚙️', label: 'Admin' },
  { href: '/settings', icon: '🔧', label: 'Settings' },
]

export default function Sidebar() {
  const pathname = usePathname()
  const router = useRouter()

  // Auth guard — redirect to login if no token present
  useEffect(() => {
    if (!getToken()) {
      router.replace('/')
    }
  }, [router])

  const logout = () => {
    clearToken()
    router.push('/')
  }

  return (
    <aside className="w-56 min-h-screen bg-[#1a1a24] border-r border-[#2e2e4a] flex flex-col">
      <div className="p-4 border-b border-[#2e2e4a]">
        <h1 className="text-lg font-bold bg-gradient-to-r from-indigo-400 to-purple-400 bg-clip-text text-transparent">
          JobJarvis
        </h1>
        <p className="text-[#8888aa] text-xs mt-0.5">AI Career Intelligence</p>
      </div>
      <nav className="flex-1 overflow-y-auto py-2">
        {NAV.map(({ href, icon, label }) => {
          const active = pathname === href || pathname?.startsWith(href + '/')
          return (
            <Link key={href} href={href}
              className={`flex items-center gap-3 px-4 py-2.5 text-sm transition-all ${
                active
                  ? 'bg-indigo-600/20 text-indigo-300 border-r-2 border-indigo-500'
                  : 'text-[#8888aa] hover:text-white hover:bg-white/5'
              }`}>
              <span className="text-base">{icon}</span>
              {label}
            </Link>
          )
        })}
      </nav>
      <div className="p-3 border-t border-[#2e2e4a]">
        <button onClick={logout}
          className="w-full text-left px-3 py-2 text-sm text-[#8888aa] hover:text-red-400 transition-colors">
          Sign Out
        </button>
      </div>
    </aside>
  )
}
