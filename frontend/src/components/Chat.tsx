import { useCallback, useEffect, useRef, useState } from "react";
import { get, newKey, post, STAGE_LABEL, uploadFiles, watchJob } from "../api";
import type { Message, Selection } from "../types";
import type { Items } from "../App";
import MessageView from "./MessageView";

type JobInfo = { id: string; kind: string; state: string; stage: string; detail?: string; error?: string | null; label?: string };

export default function Chat({ caseId, conversationId, items, onItemsChanged, onSelect }: {
  caseId: string;
  conversationId: string;
  items: Items;
  onItemsChanged: () => void;
  onSelect: (s: Selection) => void;
}) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [jobs, setJobs] = useState<Record<string, JobInfo>>({});
  const [uploading, setUploading] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);
  const watchers = useRef<Record<string, () => void>>({});

  const loadMessages = useCallback(async () => {
    setMessages(await get<Message[]>(`/api/conversations/${conversationId}/messages`));
  }, [conversationId]);

  const track = useCallback((id: string, kind: string, label?: string) => {
    if (watchers.current[id]) return;
    setJobs((j) => ({ ...j, [id]: { id, kind, state: "running", stage: "queued", label } }));
    watchers.current[id] = watchJob(id, (e) => {
      if (e.type === "stage") {
        setJobs((j) => ({ ...j, [id]: { ...j[id], stage: e.data.stage, detail: e.data.detail } }));
      } else if (["artifact", "dataset", "extraction"].includes(e.type)) {
        onItemsChanged();
      } else if (e.type === "done") {
        delete watchers.current[id];
        setJobs((j) => ({ ...j, [id]: { ...j[id], state: e.data.error ? "failed" : e.data.cancelled ? "cancelled" : "done", error: e.data.error } }));
        loadMessages();
        onItemsChanged();
        if (e.type === "done" && kind === "ingest" && !e.data.error && e.data.document_id && e.data.rows !== undefined) {
          onSelect({ type: "review", id: e.data.document_id });
        }
        if (kind === "chat" && e.data.created?.length) {
          const last = e.data.created[e.data.created.length - 1];
          onSelect({ type: last.type, id: last.id });
        }
      }
    });
  }, [loadMessages, onItemsChanged, onSelect]);

  // On load, attach to running jobs and learn about failed ones (for retry).
  useEffect(() => {
    loadMessages();
    get<any[]>(`/api/cases/${caseId}/jobs`).then((js) => {
      const m: Record<string, JobInfo> = {};
      for (const j of js) m[j.id] = { id: j.id, kind: j.kind, state: j.state, stage: j.stage, error: j.error };
      setJobs((cur) => ({ ...m, ...cur }));
      for (const j of js) if (j.state === "running" || j.state === "queued") track(j.id, j.kind);
    });
    return () => {
      Object.values(watchers.current).forEach((u) => u());
      watchers.current = {};
    };
  }, [caseId, conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, Object.keys(jobs).length]);

  const sendMessage = async () => {
    const t = text.trim();
    if (!t) return;
    setError("");
    setText("");
    try {
      const r = await post(`/api/conversations/${conversationId}/messages`, { text: t });
      await loadMessages();
      track(r.job_id, "chat");
    } catch (e) {
      setError((e as Error).message);
      setText(t);
    }
  };

  const upload = async (files: File[]) => {
    if (!files.length) return;
    setError("");
    setUploading(0);
    try {
      const r = await uploadFiles(`/api/cases/${caseId}/uploads`, files, setUploading, newKey());
      for (const [i, d] of r.documents.entries()) {
        if (d.job_id) track(d.job_id, "ingest", files[i]?.name);
      }
      onItemsChanged();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(null);
    }
  };

  const active = Object.values(jobs).filter((j) => j.state === "running" || j.state === "queued");
  const failedUploads = Object.values(jobs).filter((j) => j.kind === "ingest" && (j.state === "failed" || j.state === "interrupted"));

  return (
    <section
      className={`chat ${dragOver ? "drag" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        upload(Array.from(e.dataTransfer.files));
      }}
    >
      <div className="messages">
        {messages.length === 0 && (
          <div className="empty muted">
            <p>Drop records here (photo, PDF, XLSX, CSV, JSON) or use <em>Scan with phone</em>.</p>
            <p>Try: “Create an Excel sheet from the reviewed labs”, “Sort by collection date”, or a medical question —
              literature searches need your approval and never include patient details.</p>
          </div>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} m={m} job={m.job_id ? jobs[m.job_id] : undefined} items={items}
            onSelect={onSelect} onTrack={track} onChanged={loadMessages}
            onRetry={async (jid) => {
              await post(`/api/jobs/${jid}/retry`);
              delete watchers.current[jid];
              track(jid, jobs[jid]?.kind ?? "chat");
            }} />
        ))}
        {active.map((j) => (
          <div key={j.id} className="job-progress">
            <span className="spinner" />
            <span>
              {j.label ? <strong>{j.label}: </strong> : null}
              {STAGE_LABEL[j.stage] ?? j.stage}
              {j.detail ? ` — ${j.detail}` : ""}
            </span>
            <button className="small" onClick={() => post(`/api/jobs/${j.id}/cancel`)}>Cancel</button>
          </div>
        ))}
        {failedUploads.map((j) => (
          <div key={j.id} className="job-progress failed">
            <span>Processing failed{j.label ? ` (${j.label})` : ""}: {j.error}</span>
            <button className="small" onClick={async () => {
              await post(`/api/jobs/${j.id}/retry`);
              delete watchers.current[j.id];
              track(j.id, "ingest", j.label);
            }}>Retry</button>
            <button className="small" onClick={() => setJobs((x) => ({ ...x, [j.id]: { ...x[j.id], state: "dismissed" } }))}>Dismiss</button>
          </div>
        ))}
        <div ref={bottom} />
      </div>
      {error && <div className="error">{error}</div>}
      <div className="composer">
        <label className="button attach" title="Attach files">
          +
          <input type="file" multiple hidden accept=".jpg,.jpeg,.png,.pdf,.xlsx,.csv,.tsv,.json,image/*"
            onChange={(e) => {
              upload(Array.from(e.target.files ?? []));
              e.target.value = "";
            }} />
        </label>
        <textarea
          value={text}
          placeholder="Ask about this case's records, or request a table/file…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              sendMessage();
            }
          }}
          rows={2}
        />
        <button className="primary" onClick={sendMessage} disabled={!text.trim()}>Send</button>
      </div>
      {uploading !== null && <div className="upload-bar"><div style={{ width: `${Math.round(uploading * 100)}%` }} /></div>}
    </section>
  );
}
