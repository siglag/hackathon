import { useEffect, useState } from "react";
import { get, newKey, post } from "../api";
import { renderMarkdown, sanitize } from "../markdown";
import type { Artifact, Dataset, Doc, Selection } from "../types";
import type { Items } from "../App";
import TableView from "./TableView";
import JsonView from "./JsonView";
import ImageView from "./ImageView";
import ExtractionReview from "./ExtractionReview";

type Props = { caseId: string; items: Items; selection: Selection | null; onSelect: (s: Selection | null) => void; onChanged: () => void };

export default function ArtifactPanel({ items, selection, onSelect, onChanged }: Props) {
  if (!selection) return <Overview items={items} onSelect={onSelect} />;
  return (
    <div className="panel-inner">
      <div className="panel-nav">
        <button className="link" onClick={() => onSelect(null)}>← All items</button>
      </div>
      {selection.type === "dataset" && <DatasetView key={selection.id} id={selection.id} onSelect={onSelect} onChanged={onChanged} />}
      {selection.type === "artifact" && <ArtifactView key={selection.id} id={selection.id} onSelect={onSelect} />}
      {selection.type === "document" && <DocumentView key={selection.id} id={selection.id} onSelect={onSelect} onChanged={onChanged} />}
      {selection.type === "review" && <ExtractionReview key={selection.id} documentId={selection.id} onSelect={onSelect} onChanged={onChanged} />}
    </div>
  );
}

const fmtTime = (t: number) => new Date(t * 1000).toLocaleString();

function Overview({ items, onSelect }: { items: Items; onSelect: (s: Selection) => void }) {
  return (
    <div className="panel-inner overview">
      <h3>Files ({items.artifacts.length})</h3>
      {items.artifacts.length === 0 && <p className="muted small">Generated files appear here.</p>}
      <ul className="items">
        {items.artifacts.map((a) => (
          <li key={a.id} onClick={() => onSelect({ type: "artifact", id: a.id })}>
            <span className="fmt">{a.format}</span> {a.title} <span className="muted">v{a.version}</span>
            {a.review_state === "draft" && <span className="badge bad">draft</span>}
          </li>
        ))}
      </ul>
      <h3>Datasets ({items.datasets.length})</h3>
      <ul className="items">
        {items.datasets.map((d) => (
          <li key={d.id} onClick={() => onSelect({ type: "dataset", id: d.id })}>
            <span className="fmt">table</span> {d.label} <span className="muted">v{d.version} · {d.row_count} rows</span>
            <ReviewBadge state={d.review_state} />
          </li>
        ))}
      </ul>
      <h3>Records ({items.documents.length})</h3>
      <ul className="items">
        {items.documents.map((d) => (
          <li key={d.id} onClick={() => onSelect(d.extraction_state && d.extraction_state !== "reviewed" ? { type: "review", id: d.id } : { type: "document", id: d.id })}>
            <span className="fmt">{d.kind}</span> {d.filename}
            <span className={`state ${d.state}`}>{d.state.replace(/_/g, " ")}</span>
            {d.unreviewed_critical > 0 && <span className="badge bad">{d.unreviewed_critical} to review</span>}
            {d.source === "phone" && <span className="badge">phone</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ReviewBadge({ state }: { state: string }) {
  if (state === "reviewed") return <span className="badge ok">reviewed</span>;
  if (state === "draft") return <span className="badge bad">contains unreviewed values</span>;
  return <span className="badge">imported</span>;
}

const EXPORTS = ["xlsx", "csv", "json", "docx", "pdf", "md"];

function DatasetView({ id, onSelect, onChanged }: { id: string; onSelect: (s: Selection) => void; onChanged: () => void }) {
  const [d, setD] = useState<Dataset | null>(null);
  const [versions, setVersions] = useState<any[]>([]);
  const [err, setErr] = useState("");
  useEffect(() => {
    get<Dataset>(`/api/datasets/${id}`).then(setD).catch((e) => setErr(e.message));
    get<any[]>(`/api/datasets/${id}/versions`).then(setVersions);
  }, [id]);
  if (err) return <div className="error">{err}</div>;
  if (!d) return <div className="muted">Loading…</div>;
  const exportAs = async (format: string) => {
    setErr("");
    try {
      const r = await post(`/api/datasets/${id}/artifacts`, { format }, { "Idempotency-Key": newKey() });
      onChanged();
      onSelect({ type: "artifact", id: r.artifact_id });
    } catch (e) {
      setErr((e as Error).message);
    }
  };
  return (
    <div className="viewer">
      <div className="viewer-head">
        <div>
          <h3>{d.label}</h3>
          <div className="muted small">Dataset v{d.version} · {d.row_count} rows · <ReviewBadge state={d.review_state} /></div>
        </div>
        <select value={id} onChange={(e) => onSelect({ type: "dataset", id: e.target.value })} title="Version history">
          {versions.map((v) => <option key={v.id} value={v.id}>v{v.version} — {new Date(v.created_at * 1000).toLocaleTimeString()}</option>)}
        </select>
      </div>
      {d.meta?.notes?.map((n) => <div key={n} className="warn small">{n}</div>)}
      {d.transformations.length > 0 && (
        <details className="small"><summary>Transformations ({d.transformations.length})</summary>
          <pre className="tiny">{JSON.stringify(d.transformations.map((t) => t.ops), null, 1)}</pre></details>
      )}
      <TableView dataset={d} onNewVersion={(nid) => {
        onChanged();
        onSelect({ type: "dataset", id: nid });
      }} />
      <div className="row wrap gap export">
        Create file: {EXPORTS.map((f) => <button key={f} className="small" onClick={() => exportAs(f)}>{f.toUpperCase()}</button>)}
      </div>
    </div>
  );
}

function ArtifactView({ id, onSelect }: { id: string; onSelect: (s: Selection) => void }) {
  const [a, setA] = useState<any>(null);
  const [versions, setVersions] = useState<Artifact[]>([]);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [ds, setDs] = useState<Dataset | null>(null);
  useEffect(() => {
    get(`/api/artifacts/${id}/preview`).then((x) => {
      setA(x);
      if (x.preview.kind === "table" && x.dataset_id && x.format !== "json") get<Dataset>(`/api/datasets/${x.dataset_id}`).then(setDs);
    });
    get<Artifact[]>(`/api/artifacts/${id}/versions`).then(setVersions);
  }, [id]);
  if (!a) return <div className="muted">Loading…</div>;
  const p = a.preview;
  return (
    <div className="viewer">
      <div className="viewer-head">
        <div>
          <h3>{a.title}</h3>
          <div className="muted small">{a.format.toUpperCase()} · v{a.version} · {fmtTime(a.created_at)} · {(a.size / 1024).toFixed(1)} KB
            {a.review_state === "draft" && <span className="badge bad">draft — unreviewed values</span>}</div>
        </div>
        <div className="row gap">
          {versions.length > 1 && (
            <select value={id} onChange={(e) => onSelect({ type: "artifact", id: e.target.value })}>
              {versions.map((v) => <option key={v.id} value={v.id}>v{v.version}</option>)}
            </select>
          )}
          <a className="button primary" href={`/api/artifacts/${id}/download`} download>Download</a>
        </div>
      </div>
      {a.format === "json" && a.json && <JsonView value={a.json} />}
      {p.kind === "table" && a.format !== "json" && ds && (
        <>
          {a.format === "xlsx" && <div className="muted small">Preview of the workbook's data sheet. Excel formatting is not reproduced; a Provenance sheet is included in the file.</div>}
          <TableView dataset={ds} readOnly />
        </>
      )}
      {p.kind === "pdf" && (
        <div className="pdf">
          <div className="row gap small">
            <button className="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button>
            Page {page} / {p.page_count}
            <button className="small" disabled={page >= p.page_count} onClick={() => setPage(page + 1)}>›</button>
            <button className="small" onClick={() => setZoom(Math.max(0.5, zoom - 0.25))}>−</button>
            <button className="small" onClick={() => setZoom(Math.min(3, zoom + 0.25))}>+</button>
          </div>
          <div className="pdf-page"><img src={`/api/artifacts/${id}/pages/${page}.png`} style={{ width: `${zoom * 100}%` }} alt={`Page ${page}`} /></div>
        </div>
      )}
      {p.kind === "report" && a.format === "md" && p.markdown && (
        <div className="report" dangerouslySetInnerHTML={{ __html: renderMarkdown(p.markdown) }} />
      )}
      {p.kind === "report" && a.format !== "md" && p.html && (
        <>
          <div className="muted small">HTML preview generated from the same content as the {a.format.toUpperCase()} file.</div>
          <div className="report" dangerouslySetInnerHTML={{ __html: sanitize(p.html) }} />
        </>
      )}
    </div>
  );
}


function DocumentView({ id, onSelect, onChanged }: { id: string; onSelect: (s: Selection) => void; onChanged: () => void }) {
  const [d, setD] = useState<any>(null);
  const [page, setPage] = useState(1);
  const [msg, setMsg] = useState("");
  useEffect(() => {
    get(`/api/documents/${id}`).then(setD);
  }, [id]);
  if (!d) return <div className="muted">Loading…</div>;
  const isScan = d.kind === "image" || d.kind === "pdf";
  return (
    <div className="viewer">
      <div className="viewer-head">
        <div>
          <h3>{d.filename}</h3>
          <div className="muted small">{d.kind} · {(d.size / 1024).toFixed(0)} KB · {d.state.replace(/_/g, " ")} · source: {d.source} · sha256 {d.sha256.slice(0, 12)}…</div>
        </div>
        {d.has_original && <a className="button" href={`/api/documents/${id}/original`} download>Download original</a>}
      </div>
      {d.notes.map((n: string) => <div key={n} className="warn small">{n}</div>)}
      {d.quality?.[page - 1]?.warnings?.map((w: string) => <div key={w} className="warn small">{w}</div>)}
      {isScan && d.pages > 0 && (
        <>
          <div className="row gap small">
            <button className="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button> Page {page} / {d.pages}
            <button className="small" disabled={page >= d.pages} onClick={() => setPage(page + 1)}>›</button>
            <button className="small primary" onClick={() => onSelect({ type: "review", id })}>Review extraction</button>
            <button className="small" onClick={async () => {
              await post(`/api/documents/${id}/reprocess`, { rotate: 90 });
              setMsg("Reprocessing with 90° rotation…");
              onChanged();
            }}>Rotate & re-run OCR</button>
          </div>
          <ImageView src={`/api/documents/${id}/pages/${page}.png`} />
        </>
      )}
      {msg && <div className="muted small">{msg}</div>}
      {d.datasets?.length > 0 && (
        <div>
          <h4>Tables parsed directly (no OCR)</h4>
          {d.datasets.map((ds: string) => <button key={ds} className="chip link" onClick={() => onSelect({ type: "dataset", id: ds })}>{ds}</button>)}
        </div>
      )}
      {d.embedded_images > 0 && (
        <div>
          <h4>Embedded images ({d.embedded_images})</h4>
          <p className="muted small">Native cells were read directly. Images inside the workbook can be OCR'd separately.</p>
          {Array.from({ length: d.embedded_images }, (_, n) => (
            <button key={n} className="small" onClick={async () => {
              await post(`/api/documents/${id}/embedded-images/${n}/ocr`);
              setMsg(`Image ${n + 1} queued for OCR as a new record.`);
              onChanged();
            }}>OCR image {n + 1}</button>
          ))}
        </div>
      )}
    </div>
  );
}

export type { Doc };
