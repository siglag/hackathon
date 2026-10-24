import { useCallback, useEffect, useRef, useState } from "react";
import { get, newKey, post, STAGE_LABEL, uploadFiles, watchJob } from "../api";
import type { Message, Selection } from "../types";
import type { Items } from "../App";
import MessageView from "./MessageView";
import { ArrowUpIcon, PlusIcon, StopIcon } from "./icons";

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
  const input = useRef<HTMLTextAreaElement>(null);
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
  const replying = active.find((j) => j.kind === "chat" || j.kind === "evidence_answer");
  const isEmpty = messages.length === 0 && active.length === 0 && failedUploads.length === 0;

  // Grow the textarea with its content, up to a cap, like ChatGPT's composer.
  useEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const composer = (
    <div className="composer-wrap">
      {error && <div className="error">{error}</div>}
      <form className="composer" onSubmit={(e) => {
        e.preventDefault();
        sendMessage();
      }}>
        <label className="icon-btn attach" title="Add photos and files">
          <PlusIcon />
          <input type="file" multiple hidden accept=".jpg,.jpeg,.png,.pdf,.xlsx,.csv,.tsv,.json,image/*"
            onChange={(e) => {
              upload(Array.from(e.target.files ?? []));
              e.target.value = "";
            }} />
        </label>
        <textarea
          ref={input}
          value={text}
          placeholder="Ask anything about this case"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              sendMessage();
            }
          }}
          rows={1}
          autoFocus
        />
        {replying && !text.trim() ? (
          <button type="button" className="send" title="Stop" onClick={() => post(`/api/jobs/${replying.id}/cancel`)}><StopIcon /></button>
        ) : (
          <button className="send" title="Send" disabled={!text.trim()}><ArrowUpIcon size={18} /></button>
        )}
        {uploading !== null && <div className="upload-bar"><div style={{ width: `${Math.round(uploading * 100)}%` }} /></div>}
      </form>
      {!isEmpty && <div className="disclaimer">Medical Assistant can make mistakes. Drafts need clinician review.</div>}
    </div>
  );

  return (
    <section
      className={`chat ${dragOver ? "drag" : ""} ${isEmpty ? "is-empty" : ""}`}
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
      {isEmpty ? (
        <div className="hero">
          <h1>What can I help with?</h1>
          {composer}
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="suggestion" onClick={() => {
                setText(s);
                input.current?.focus();
              }}>{s}</button>
            ))}
          </div>
          <p className="muted small">Drop records anywhere (photo, PDF, XLSX, CSV, JSON) or scan them with your phone.
            Literature searches need your approval and never include patient details.</p>
        </div>
      ) : (
        <>
          <div className="messages">
            <div className="thread">
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
                <div key={j.id} className="msg assistant">
                  <div className="thinking">
                    <span className="shimmer">
                      {j.label ? `${j.label}: ` : ""}
                      {STAGE_LABEL[j.stage] ?? j.stage}
                      {j.detail ? ` — ${j.detail}` : ""}
                    </span>
                    {j.kind !== "chat" && <button className="text-btn" onClick={() => post(`/api/jobs/${j.id}/cancel`)}>Cancel</button>}
                  </div>
                </div>
              ))}
              {failedUploads.map((j) => (
                <div key={j.id} className="msg assistant">
                  <div className="notice bad">
                    <span>Processing failed{j.label ? ` (${j.label})` : ""}: {j.error}</span>
                    <div className="row gap-sm">
                      <button className="small" onClick={async () => {
                        await post(`/api/jobs/${j.id}/retry`);
                        delete watchers.current[j.id];
                        track(j.id, "ingest", j.label);
                      }}>Retry</button>
                      <button className="small" onClick={() => setJobs((x) => ({ ...x, [j.id]: { ...x[j.id], state: "dismissed" } }))}>Dismiss</button>
                    </div>
                  </div>
                </div>
              ))}
              <div ref={bottom} />
            </div>
          </div>
          {composer}
        </>
      )}
    </section>
  );
}

const SUGGESTIONS = [
  "Create an Excel sheet from the reviewed labs",
  "Sort the lab table by collection date",
  "Summarize the latest records in this case",
  "What does recent evidence say about this condition?",
];
