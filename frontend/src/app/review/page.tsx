"use client";
/**
 * /review — review and submit AI-drafted job applications.
 *
 * Workflow:
 *   1. Auto-apply runs in dry-run mode → drafts appear here.
 *   2. User reviews AI answers, edits any that need fixing.
 *   3. Click "Submit" → bot re-runs in submit mode with the edited answers.
 */
import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import {
  listDrafts, updateDraft, discardDraft, submitDraft,
  type Draft, type DraftAnswer,
} from "@/lib/drafts";

function fmtAge(iso: string): string {
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (m < 1)   return "just now";
  if (m < 60)  return `${m}m ago`;
  if (m < 1440) return `${Math.floor(m/60)}h ago`;
  return `${Math.floor(m/1440)}d ago`;
}

function DraftCard({ d, onChange }: { d: Draft; onChange: () => void }) {
  const [answers, setAnswers] = useState<DraftAnswer[]>(d.answers);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const setAnswer = (i: number, value: string) =>
    setAnswers((prev) => prev.map((a, idx) => (idx === i ? { ...a, answer: value } : a)));

  async function save() {
    setBusy("save"); setErr(null);
    try { await updateDraft(d.id, answers); } catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  async function submit() {
    setBusy("submit"); setErr(null);
    try {
      await updateDraft(d.id, answers);   // persist edits first
      const r = await submitDraft(d.id);  // then submit
      alert(`Submitting to ${r.company}…`);
      onChange();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(null);
    }
  }

  async function discard() {
    if (!confirm("Discard this draft? (skips applying)")) return;
    setBusy("discard"); setErr(null);
    try { await discardDraft(d.id); onChange(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(null); }
  }

  return (
    <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
      {/* Header */}
      <div className="flex items-start justify-between gap-2 mb-3">
        <div className="min-w-0">
          <h3 className="text-lg font-semibold text-gray-900 truncate">{d.job_title}</h3>
          <p className="text-sm text-brand font-medium">{d.company}</p>
          <p className="text-xs text-gray-500 mt-1">
            {d.ats && <span className="bg-blue-50 text-blue-700 px-1.5 py-0.5 rounded mr-2">{d.ats}</span>}
            {d.fields_filled} fields filled · drafted {fmtAge(d.created_at)}
          </p>
        </div>
        {d.apply_url && (
          <a href={d.apply_url} target="_blank" rel="noopener noreferrer"
             className="text-xs text-brand hover:underline shrink-0">View job ↗</a>
        )}
      </div>

      {/* AI-drafted answers */}
      {answers.length === 0 ? (
        <p className="text-sm text-gray-500 italic mb-3">
          No custom questions detected — standard fields were filled. Ready to submit.
        </p>
      ) : (
        <div className="space-y-4 mb-4">
          {answers.map((a, i) => (
            <div key={i}>
              <label className="block text-xs font-semibold text-gray-600 mb-1">
                Q: {a.question}
              </label>
              <textarea
                value={a.answer}
                onChange={(e) => setAnswer(i, e.target.value)}
                rows={Math.max(3, Math.ceil(a.answer.length / 80))}
                className="w-full px-3 py-2 text-sm border border-gray-200 rounded-lg
                           focus:ring-2 focus:ring-brand focus:border-brand
                           font-mono"
                disabled={!!busy}
              />
            </div>
          ))}
        </div>
      )}

      {err && (
        <div className="mb-3 p-2 rounded-md bg-red-50 border border-red-200 text-xs text-red-700">
          {err}
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2 justify-end">
        <button
          onClick={discard}
          disabled={!!busy}
          className="px-3 py-1.5 text-sm font-medium rounded-md
                     bg-gray-100 hover:bg-gray-200 text-gray-700 disabled:opacity-50">
          {busy === "discard" ? "…" : "Discard"}
        </button>
        <button
          onClick={save}
          disabled={!!busy}
          className="px-3 py-1.5 text-sm font-medium rounded-md
                     bg-white border border-gray-300 hover:bg-gray-50 disabled:opacity-50">
          {busy === "save" ? "Saving…" : "Save edits"}
        </button>
        <button
          onClick={submit}
          disabled={!!busy}
          className="px-3 py-1.5 text-sm font-semibold rounded-md
                     bg-brand text-white hover:bg-blue-700 disabled:opacity-50">
          {busy === "submit" ? "Submitting…" : "Submit application →"}
        </button>
      </div>
    </div>
  );
}

export default function ReviewPage() {
  const { user, loading } = useAuth();
  const router = useRouter();
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [fetching, setFetching] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && !user) router.push("/login?next=/review");
  }, [user, loading, router]);

  const refresh = useCallback(async () => {
    setFetching(true);
    setErr(null);
    try {
      const d = await listDrafts();
      setDrafts(d);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setFetching(false);
    }
  }, []);

  useEffect(() => {
    if (user) refresh();
  }, [user, refresh]);

  if (loading || fetching) {
    return (
      <div className="max-w-3xl mx-auto py-12 px-4">
        <div className="animate-pulse h-32 bg-gray-100 rounded-xl" />
      </div>
    );
  }
  if (!user) return null;

  return (
    <div className="max-w-3xl mx-auto py-8 px-4">
      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Review applications</h1>
          <p className="text-sm text-gray-600 mt-1">
            AI drafted these for you. Edit any answer, then submit.
          </p>
        </div>
        <button
          onClick={refresh}
          className="px-3 py-2 text-sm font-medium rounded-lg border border-gray-200
                     bg-white hover:bg-gray-50 text-gray-700">
          ↻ Refresh
        </button>
      </div>

      {err && (
        <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
          {err}
        </div>
      )}

      {drafts.length === 0 ? (
        <div className="bg-amber-50 border border-amber-200 rounded-xl p-6 text-center">
          <p className="font-semibold text-amber-900 mb-2">No drafts pending</p>
          <p className="text-sm text-amber-800">
            Go to <a href="/matches" className="underline">Matches</a>, click <strong>🤖 Auto-apply (dry-run)</strong> on
            jobs you want to apply to, then come back here to review and submit.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {drafts.map((d) => (
            <DraftCard key={d.id} d={d} onChange={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}
