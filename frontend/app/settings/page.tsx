'use client'
import Sidebar from '@/components/layout/Sidebar'

export default function SettingsPage() {
  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <main className="flex-1 p-8">
        <div className="max-w-2xl mx-auto">
          <h1 className="text-2xl font-bold text-white mb-6">🔧 Settings</h1>
          <div className="card">
            <p className="text-[#8888aa]">Settings UI — configure job preferences, notification thresholds, and API keys.</p>
            <div className="mt-4 space-y-3">
              <div className="bg-[#22223a] p-3 rounded-lg">
                <p className="text-xs text-[#8888aa] mb-1">API Keys</p>
                <p className="text-sm text-white">Configure OpenAI / Anthropic keys in the .env file</p>
              </div>
              <div className="bg-[#22223a] p-3 rounded-lg">
                <p className="text-xs text-[#8888aa] mb-1">Notification Threshold</p>
                <p className="text-sm text-white">Jobs with fit score ≥ 75 trigger alerts</p>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  )
}
