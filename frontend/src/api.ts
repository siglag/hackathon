// Same-origin API client. Session cookie is HttpOnly; the CSRF token is sent
// as a header on state-changing requests (double-submit).

function csrf(): string {
  const m = document.cookie.match(/(?:^|; )ma_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

export async function get<T = any>(path: string): Promise<T> {
  return handle<T>(await fetch(path, { credentials: "same-origin" }));
}

export async function send<T = any>(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  return handle<T>(
    await fetch(path, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf(), ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export const post = <T = any>(path: string, body?: unknown, headers?: Record<string, string>) => send<T>("POST", path, body ?? {}, headers);

export function uploadFiles(
  path: string,
  files: File[] | Blob[],
  onProgress?: (fraction: number) => void,
  idempotencyKey?: string,
): Promise<any> {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    files.forEach((f, i) => fd.append("files", f, (f as File).name || `page-${i + 1}.jpg`));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-CSRF-Token", csrf());
    if (idempotencyKey) xhr.setRequestHeader("Idempotency-Key", idempotencyKey);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      let body: any = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new ApiError(xhr.status, body?.detail ?? `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
    xhr.send(fd);
  });
}

export type JobEvent = { seq: number; ts: number; type: string; data: any };

/** Subscribe to a job's server-sent events. Returns an unsubscribe function. */
export function watchJob(jobId: string, onEvent: (e: JobEvent) => void): () => void {
  const es = new EventSource(`/api/jobs/${jobId}/events`, { withCredentials: true });
  const handler = (m: MessageEvent) => {
    const e = JSON.parse(m.data) as JobEvent;
    onEvent(e);
    if (e.type === "done") es.close();
  };
  for (const t of ["stage", "tool", "artifact", "dataset", "extraction", "evidence_query", "intent", "done"]) {
    es.addEventListener(t, handler as EventListener);
  }
  es.onerror = () => {
    /* EventSource reconnects automatically with Last-Event-ID */
  };
  return () => es.close();
}

export function newKey(): string {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

export const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  uploaded: "Uploaded",
  parsing: "Parsing",
  ocr: "OCR",
  needs_review: "Needs review",
  retrieving_evidence: "Retrieving evidence",
  generating: "Generating",
  ready: "Ready",
  failed: "Failed",
  cancelled: "Cancelled",
};
