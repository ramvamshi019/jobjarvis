import { authHeaders } from "@/lib/auth";

export interface DraftAnswer { question: string; answer: string }
export interface Draft {
  id: number;
  job_id: number;
  job_title: string;
  company: string;
  apply_url: string | null;
  ats: string | null;
  fields_filled: number;
  answers: DraftAnswer[];
  screenshot: string | null;
  created_at: string;
  status: string;
}

export async function listDrafts(): Promise<Draft[]> {
  const r = await fetch("/api/drafts", { headers: { ...authHeaders() } });
  if (!r.ok) throw new Error(`Failed: ${r.status}`);
  return r.json();
}

export async function updateDraft(id: number, answers: DraftAnswer[]): Promise<Draft> {
  const r = await fetch(`/api/drafts/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ answers }),
  });
  if (!r.ok) throw new Error(`Failed: ${r.status}`);
  return r.json();
}

export async function discardDraft(id: number): Promise<void> {
  const r = await fetch(`/api/drafts/${id}/discard`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  if (!r.ok) throw new Error(`Failed: ${r.status}`);
}

export async function submitDraft(id: number): Promise<{ queued: boolean; company: string }> {
  const r = await fetch(`/api/drafts/${id}/submit`, {
    method: "POST",
    headers: { ...authHeaders() },
  });
  if (!r.ok) throw new Error(`Failed: ${r.status}`);
  return r.json();
}
