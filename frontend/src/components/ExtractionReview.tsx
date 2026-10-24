import { useEffect, useState } from "react";
import { get, send, post } from "../api";
import type { Selection } from "../types";
import ImageView, { type Box } from "./ImageView";

type Field = {
  raw_text: string | null;
  value: any;
  source: { page: number; bbox: number[] } | null;
  ocr_score: number | null;
  review_state: string;
  critical: boolean;
  issues: string[];
  comparator?: string;
};
type XRow = { id: string; page: number; proposed_by: string; fields: Record<string, Field> };
type Extraction = {
  document_id: string;
  version: number;
  schema: string;
  review_state: string;
  engine: string;
  pages: { page: number; width: number; height: number; spans: { id: string; bbox: number[]; text: string; score: number | null }[] }[];
  header: Record<string, Field>;
  rows: XRow[];
  fields: string[];
  unreviewed_critical: number;
};

export default function ExtractionReview({ documentId, onSelect, onChanged }: { documentId: string; onSelect: (s: Selection) => void; onChanged: () => void }) {
  const [ex, setEx] = useState<Extraction | null>(null);
  const [page, setPage] = useState(1);
  const [focus, setFocus] = useState<string | null>(null);
  const [editing, setEditing] = useState<{ row: string; field: string; value: string } | null>(null);
  const [err, setErr] = useState("");
  const [showAllSpans, setShowAllSpans] = useState(false);

  useEffect(() => {
    get<Extraction>(`/api/documents/${documentId}/extraction`).then(setEx).catch((e) => setErr(e.message));
  }, [documentId]);

  if (err && !ex) return <div className="error">{err}</div>;
  if (!ex) return <div className="muted">Loading extraction…</div>;

  const patch = async (body: any) => {
    setErr("");
    try {
      const r = await send<Extraction>("PATCH", `/api/documents/${documentId}/extraction`, { version: ex.version, ...body });
      setEx(r);
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
      if ((e as Error).message.includes("changed")) get<Extraction>(`/api/documents/${documentId}/extraction`).then(setEx);
    }
  };
  const act = (row: string, field: string, action: string, raw_text?: string) => patch({ updates: [{ row_id: row, field, action, raw_text }] });

  const freeze = async (allow_draft: boolean) => {
    setErr("");
    try {
      const d = await post(`/api/documents/${documentId}/extraction/freeze`, { allow_draft });
      onChanged();
      onSelect({ type: "dataset", id: d.id });
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const pg = ex.pages.find((p) => p.page === page) ?? ex.pages[0];
  const boxes: Box[] = [];
  const addBox = (key: string, f: Field) => {
    if (f.source && f.source.page === page) {
      boxes.push({ id: key, bbox: f.source.bbox, label: `${key}: ${f.raw_text ?? ""}`,
        tone: f.review_state === "unreviewed" ? "warn" : "ok" });
    }
  };
  Object.entries(ex.header).forEach(([k, f]) => addBox(`header.${k}`, f));
  ex.rows.forEach((r) => Object.entries(r.fields).forEach(([k, f]) => k !== "collection_date" && addBox(`${r.id}.${k}`, f)));
  if (showAllSpans) pg.spans.forEach((s) => boxes.push({ id: s.id, bbox: s.bbox, label: `${s.text} (${s.score ?? "text"})` }));

  const cell = (rowId: string, name: string, f: Field | undefined) => {
    if (!f) return <td key={name} />;
    const key = `${rowId}.${name}`;
    const isEditing = editing?.row === rowId && editing.field === name;
    return (
      <td key={name} className={`xf ${f.review_state} ${focus === key ? "focus" : ""}`} onMouseEnter={() => {
        setFocus(key);
        if (f.source && f.source.page !== page) setPage(f.source.page);
      }}>
        {isEditing ? (
          <input autoFocus value={editing.value} onChange={(e) => setEditing({ ...editing, value: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                act(rowId, name, "correct", editing.value);
                setEditing(null);
              }
              if (e.key === "Escape") setEditing(null);
            }} />
        ) : (
          <span className="raw" title={[`raw: ${f.raw_text ?? "(missing)"}`, `parsed: ${JSON.stringify(f.value)}`,
            f.ocr_score != null ? `engine score: ${f.ocr_score}` : "", ...f.issues].filter(Boolean).join("\n")}>
            {f.raw_text ?? <em className="muted">unknown</em>}
          </span>
        )}
        <span className="xf-actions">
          {f.review_state === "unreviewed" && f.raw_text != null && <button className="tiny" title="Confirm matches the record" onClick={() => act(rowId, name, "confirm")}>✓</button>}
          <button className="tiny" title="Correct value" onClick={() => setEditing({ row: rowId, field: name, value: f.raw_text ?? "" })}>✎</button>
          {f.review_state === "unreviewed" && <button className="tiny" title="Mark unreadable / unknown" onClick={() => act(rowId, name, "unknown")}>?</button>}
        </span>
        {f.issues.length > 0 && <span className="issue" title={f.issues.join("\n")}>!</span>}
      </td>
    );
  };

  const confirmRow = (r: XRow) => patch({
    updates: Object.entries(r.fields).filter(([, f]) => f.review_state === "unreviewed" && f.raw_text != null)
      .map(([k]) => ({ row_id: r.id, field: k, action: "confirm" })),
  });

  return (
    <div className="review">
      <div className="viewer-head">
        <div>
          <h3>Review extraction</h3>
          <div className="muted small">{ex.schema.replace("_", " ")} · engine {ex.engine} · version {ex.version} ·{" "}
            {ex.unreviewed_critical > 0 ? <span className="badge bad">{ex.unreviewed_critical} clinically material field(s) unreviewed</span> : <span className="badge ok">all material fields reviewed</span>}</div>
        </div>
      </div>
      <p className="muted small">Compare each value with the image. Engine scores are not probabilities of correctness; every medication, dose, lab value, unit, date, allergy and patient association needs your review. Unreadable values stay unknown.</p>
      <div className="review-split">
        <div className="review-image">
          {ex.pages.length > 1 && (
            <div className="row gap small">
              <button className="small" disabled={page <= 1} onClick={() => setPage(page - 1)}>‹</button> Page {page}/{ex.pages.length}
              <button className="small" disabled={page >= ex.pages.length} onClick={() => setPage(page + 1)}>›</button>
            </div>
          )}
          <label className="check small"><input type="checkbox" checked={showAllSpans} onChange={(e) => setShowAllSpans(e.target.checked)} /> show all recognized text</label>
          <ImageView src={`/api/documents/${documentId}/pages/${page}.png`} boxes={boxes} naturalWidth={pg.width} naturalHeight={pg.height}
            focusId={focus} onBoxClick={setFocus} />
        </div>
        <div className="review-fields">
          <h4>Document fields</h4>
          <table className="simple xt">
            <tbody>
              {Object.entries(ex.header).map(([k, f]) => (
                <tr key={k}><th>{k.replace(/_/g, " ")}</th>{cell("header", k, f)}</tr>
              ))}
              {Object.keys(ex.header).length === 0 && <tr><td className="muted">No patient/header fields detected.</td></tr>}
            </tbody>
          </table>
          <h4>Rows ({ex.rows.length})</h4>
          <div className="scroll-x">
            <table className="simple xt">
              <thead><tr><th />{ex.fields.map((f) => <th key={f}>{f.replace(/_/g, " ")}</th>)}<th /></tr></thead>
              <tbody>
                {ex.rows.map((r) => (
                  <tr key={r.id}>
                    <td className="muted tiny" title={`proposed by ${r.proposed_by}`}>{r.id}</td>
                    {ex.fields.map((f) => cell(r.id, f, r.fields[f]))}
                    <td>
                      <button className="tiny" onClick={() => confirmRow(r)} title="Confirm every non-empty field in this row after checking it">✓ row</button>
                      <button className="tiny" onClick={() => patch({ delete_rows: [r.id] })} title="Delete row (not a real result)">✕</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button className="small" onClick={() => patch({ add_row: true })}>+ Add row manually</button>
          {err && <div className="error">{err}</div>}
          <div className="row gap freeze">
            <button className="primary" disabled={ex.unreviewed_critical > 0} onClick={() => freeze(false)}>Create reviewed dataset</button>
            <button onClick={() => freeze(true)}>Create draft dataset (marked unreviewed)</button>
          </div>
        </div>
      </div>
    </div>
  );
}
