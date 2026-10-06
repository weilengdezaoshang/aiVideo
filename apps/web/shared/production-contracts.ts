// Generated from backend.services.public_projection; do not edit by hand.

export interface AcceptedJobResponse {
  job: JobResponse;
  jobId: string;
}

export interface JobEnvelope {
  job: JobResponse;
}

export interface JobResponse {
  id: string;
  status: "queued" | "running" | "completed" | "failed" | "unknown";
  kind: string;
  params: Record<string, unknown>;
  progress: number;
  message: string;
  images: Array<Record<string, unknown>>;
  createdAt: string;
  documentId: string | null;
  clientRef: string | null;
  requestId: string | null;
  code: string | null;
  recovery: string | null;
  retryAfter: number | null;
  stateVersion: number;
  phase: string;
  batchCount: number;
  children: Array<JobResponse>;
  groupId: string | null;
  slot: number | null;
  directionTitle: string | null;
}

export interface JobsResponse {
  jobs: Array<JobResponse>;
}
