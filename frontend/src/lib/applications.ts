/**
 * Application tracker API helpers.
 * All calls require a valid JWT (attached via authHeaders()).
 */
import { authHeaders } from "@/lib/auth";

export type AppStatus =
  | "saved"
  | "applied"
  | "interview"
  | "offer"
  | "rejected"
  | "closed";

export interface TrackedApplication {
  id: number;
  job_id: number;
  user_id: number;
  status: AppStatus;
  applied_at: string | null;
  follow_up_at: string | null;
  recruiter_name: string | null;
  recruiter_email: string | null;
  notes: string | null;
  outcome: string | null;
  interview_rounds: number;
  created_at: string;
  // Joined from the job (fetched separately)
  job_title?: string;
  company_name?: string;
  job_url?: string | null;
  location?: string | null;
}

// ── API calls ─────────────────────────────────────────────────────────────────

export async function listApplications(
  status?: AppStatus,
): Promise<TrackedApplication[]> {
  const qs = status ? `?status=${status}` : "";
  const res = await fetch(`/api/applications${qs}`, {
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error("Failed to fetch applications");
  return res.json();
}

export async function createApplication(
  jobId: number,
  status: AppStatus = "saved",
  notes?: string,
): Promise<TrackedApplication> {
  const res = await fetch("/api/applications", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ job_id: jobId, status, notes }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err?.detail ?? "Failed to save application");
  }
  return res.json();
}

export async function updateApplication(
  appId: number,
  patch: Partial<{
    status: AppStatus;
    notes: string;
    recruiter_name: string;
    recruiter_email: string;
    outcome: string;
    interview_rounds: number;
  }>,
): Promise<TrackedApplication> {
  const res = await fetch(`/api/applications/${appId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error("Failed to update application");
  return res.json();
}
