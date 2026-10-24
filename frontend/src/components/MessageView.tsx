import { useState } from "react";
import { newKey, post } from "../api";
import type { Answer, Citation, Claim, EvidenceQuery, Message, Selection } from "../types";
import type { Items } from "../App";

type Props = {
  m: Message;
  job?: { state: string; error?: string | null; kind: string };
  items: Items;
  onSelect: (s: Selection) => void;
  onTrack: (jobId: string, kind: string) => void;
  onChanged: () => void;
  onRetry: (jobId: string) => void;
};

export default function MessageView({ m, job, onSelect, onTrack, onChanged, onRetry }: Props) {
  if (m.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{m.content}</div>
        {job && (job.state === "failed" || job.state === "interrupted") && (
          <div className="msg-error">
            {job.error ?? "Failed"} <button className="small" onClick={() => onRetry(m.job_id!)}>Retry</button>
          </div>
        )}
        {job?.state === "cancelled" && <div className="muted small">Cancelled — no reply was saved.</div>}
      </div>
    );
  }
  const t = m.meta?.type;
  return (
    <div className="msg assistant">
      {t === "evidence_approval" && m.meta.query ? (
        <ApprovalCard q={m.meta.query} text={m.content} onTrack={onTrack} onChanged={onChanged} />
      ) : t === "evidence_answer" ? (
        <EvidenceAnswer m={m} onSelect={onSelect} />
      ) : (
        <div className="bubble">
          <p className="pre">{m.content}</p>
          {m.meta?.tools?.length > 0 && (
            <div className="chips">
              {m.meta.tools.map((tl: any, i: number) => (
                <span key={i} className={`chip ${tl.status === "error" ? "bad" : ""}`} title={tl.error ?? ""}>
                  {tl.name.replace(/_/g, " ")}{tl.status === "error" ? " ✕" : ""}
                </span>
              ))}
            </div>
          )}
          {m.meta?.created?.length > 0 && (
            <div className="chips">
              {m.meta.created.map((c: any) => (
                <button key={c.id} className="chip link" onClick={() => onSelect({ type: c.type, id: c.id })}>
                  Open {c.type} {c.id.slice(-6)}
                </button>
              ))}
            </div>
          )}
          {t === "agent" && <div className="muted tiny">{m.meta.agent} · {m.meta.model} · {m.meta.steps} step(s)</div>}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({ q, text, onTrack, onChanged }: { q: EvidenceQuery; text: string; onTrack: (id: string, k: string) => void; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [concepts, setConcepts] = useState(q.request.concepts.join(", "));
  const [population, setPopulation] = useState(q.request.population ?? "");
  const [studyTypes, setStudyTypes] = useState<string[]>(q.request.study_types);
  const [err, setErr] = useState("");
  const pending = q.status === "pending_approval";
  const approve = async () => {
    setErr("");
    try {
      const r = await post(`/api/evidence-queries/${q.id}/approve`, { request_hash: q.request_hash });
      onTrack(r.job_id, "evidence_answer");
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  const revise = async () => {
    setErr("");
    try {
      await post(`/api/evidence-queries/${q.id}/revise`, {
        concepts: concepts.split(",").map((s) => s.trim()).filter(Boolean),
        population: population || null,
        study_types: studyTypes,
      });
      setEditing(false);
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className={`bubble approval ${q.status}`}>
      <p>{text}</p>
      <div className="approval-box">
        <div className="label">Outgoing general research topic</div>
        {q.request.concepts.length ? <div className="topic">{q.description}</div> : <div className="topic muted">— none —</div>}
        {q.search_string && <code className="search">{q.search_string}</code>}
        {q.held_terms.length > 0 && (
          <div className="held">Kept local (not in approved vocabulary, never sent): {q.held_terms.map((h) => <span key={h} className="chip">{h}</span>)}</div>
        )}
        {q.problems.map((p) => <div key={p} className="warn">{p}</div>)}
        <div className="muted tiny">Destination: hosted PubMed MCP via the local evidence gateway. The provider sees this topic, timing and the clinic's public IP — never the question, names, IDs, dates or records.</div>
      </div>
      {editing ? (
        <div className="revise">
          <label>Concepts (comma-separated, general terms)
            <input value={concepts} onChange={(e) => setConcepts(e.target.value)} />
          </label>
          <label>Population
            <select value={population} onChange={(e) => setPopulation(e.target.value)}>
              <option value="">any</option>
              {["adults", "older adults", "children", "adolescents", "infants", "pregnant women", "women", "men"].map((p) => <option key={p}>{p}</option>)}
            </select>
          </label>
          <div className="row wrap gap">
            {["systematic review", "meta-analysis", "randomized controlled trial", "guideline", "review", "clinical trial", "observational study"].map((s) => (
              <label key={s} className="check">
                <input type="checkbox" checked={studyTypes.includes(s)}
                  onChange={(e) => setStudyTypes(e.target.checked ? [...studyTypes, s] : studyTypes.filter((x) => x !== s))} /> {s}
              </label>
            ))}
          </div>
          <div className="row gap">
            <button className="primary" onClick={revise}>Prepare revised topic</button>
            <button onClick={() => setEditing(false)}>Cancel</button>
          </div>
          <div className="muted tiny">A revised topic is a new request and needs its own approval.</div>
        </div>
      ) : (
        <div className="row gap">
          {pending && <button className="primary" onClick={approve}>Approve and search</button>}
          {["pending_approval", "needs_revision", "blocked"].includes(q.status) && (
            <>
              <button onClick={() => setEditing(true)}>Edit topic</button>
              <button onClick={async () => {
                await post(`/api/evidence-queries/${q.id}/decline`);
                onChanged();
              }}>Decline</button>
            </>
          )}
          <span className={`status-tag ${q.status}`}>{q.status.replace(/_/g, " ")}</span>
        </div>
      )}
      {err && <div className="error">{err}</div>}
    </div>
  );
}

function EvidenceAnswer({ m, onSelect }: { m: Message; onSelect: (s: Selection) => void }) {
  const a: Answer = m.meta.answer;
  const [open, setOpen] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const passageMap = new Map<string, { c: Citation; text: string }>();
  for (const c of a.citations) for (const p of c.passages) passageMap.set(p.id, { c, text: p.text });
  const recMap = new Map((a.records ?? []).map((r) => [r.ref, r]));

  const chips = (cl: Claim) =>
    cl.refs.map((r) => {
      if (r.startsWith("R")) {
        const rec = recMap.get(r);
        return (
          <button key={r} className="chip record" title={rec?.text}
            onClick={() => rec && onSelect({ type: "dataset", id: rec.dataset_id })}>record {r}</button>
        );
      }
      const p = passageMap.get(r);
      return (
        <button key={r} className={`chip source ${open === r ? "on" : ""}`} onClick={() => setOpen(open === r ? null : r)}>
          {p ? `PMID ${p.c.pmid}` : r}
        </button>
      );
    });

  const exportAs = async (format: string) => {
    setExporting(true);
    try {
      const r = await post(`/api/messages/${m.id}/artifacts`, { format }, { "Idempotency-Key": newKey() });
      onSelect({ type: "artifact", id: r.artifact_id });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="bubble evidence">
      <div className="draft-note">Draft for clinician review · literature retrieved {a.retrieved_at ? new Date(a.retrieved_at * 1000).toLocaleString() : ""}</div>
      {a.query && <div className="muted tiny">Approved topic: {a.query.description}</div>}
      {a.findings.length === 0 ? (
        <p><strong>No supported answer.</strong> {m.content}</p>
      ) : (
        <>
          <h4>Literature findings</h4>
          <ul>
            {a.findings.map((f, i) => (
              <li key={i}>
                {f.text} {chips(f)}
                {f.weak_support && <span className="warn-inline" title="Low overlap with the cited passage — verify."> verify support</span>}
              </li>
            ))}
          </ul>
        </>
      )}
      {open && passageMap.get(open) && (
        <blockquote className="excerpt">
          “{passageMap.get(open)!.text}”
          <div className="muted tiny">{passageMap.get(open)!.c.title}</div>
        </blockquote>
      )}
      {a.case_interpretation.length > 0 && (
        <div className="interpretation">
          <h4>Case-specific interpretation <span className="badge">assistant draft</span></h4>
          <ul>{a.case_interpretation.map((f, i) => <li key={i}>{f.text} {chips(f)}</li>)}</ul>
        </div>
      )}
      {a.unanswered.length > 0 && (
        <>
          <h4>Not answered by the retrieved evidence</h4>
          <ul className="muted">{a.unanswered.map((u, i) => <li key={i}>{u}</li>)}</ul>
        </>
      )}
      {a.limitations.length > 0 && (
        <details>
          <summary>Limitations ({a.limitations.length})</summary>
          <ul>{a.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul>
        </details>
      )}
      {a.citations.length > 0 && (
        <div className="citations">
          {a.citations.map((c) => (
            <div key={c.pmid} className={`citation ${c.flags.includes("retracted") ? "retracted" : ""}`}>
              <div className="title">{c.title}</div>
              <div className="muted small">
                {c.authors.slice(0, 3).join(", ")}{c.authors.length > 3 ? " et al." : ""} · {c.journal} · {c.year} · {c.design}
              </div>
              <div className="small">
                PMID {c.pmid}{c.doi ? ` · DOI ${c.doi}` : ""} · <span className="badge">{c.access}</span>
                {c.flags.map((f) => <span key={f} className="badge bad">{f.replace(/_/g, " ")}</span>)}
              </div>
              <div className="row gap small">
                {c.pubmed_url && <a href={c.pubmed_url} target="_blank" rel="noopener noreferrer">PubMed citation page ↗</a>}
                {c.doi_url && <a href={c.doi_url} target="_blank" rel="noopener noreferrer">Publisher (DOI) ↗</a>}
              </div>
              {c.passages.map((p) => <div key={p.id} className="excerpt small">[{p.from}] “{p.text}”</div>)}
            </div>
          ))}
          <div className="muted tiny">A citation link existing does not prove it supports a claim. Check each excerpt.</div>
        </div>
      )}
      <div className="row gap">
        {["docx", "pdf", "md"].map((f) => <button key={f} className="small" disabled={exporting} onClick={() => exportAs(f)}>Save as {f.toUpperCase()}</button>)}
      </div>
    </div>
  );
}
