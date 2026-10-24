import { useState } from "react";

/** Expandable tree, plus a tabular view for arrays of objects. */
export default function JsonView({ value }: { value: unknown }) {
  const tableable = findTable(value);
  const [mode, setMode] = useState<"tree" | "table">(tableable ? "table" : "tree");
  return (
    <div className="json-view">
      {tableable && (
        <div className="row gap small">
          <button className={`small ${mode === "tree" ? "primary" : ""}`} onClick={() => setMode("tree")}>Tree</button>
          <button className={`small ${mode === "table" ? "primary" : ""}`} onClick={() => setMode("table")}>Table</button>
        </div>
      )}
      {mode === "tree" || !tableable ? <Node k="$" v={value} depth={0} /> : <ArrayTable rows={tableable} />}
    </div>
  );
}

function findTable(v: any): Record<string, unknown>[] | null {
  if (Array.isArray(v) && v.length && v.every((x) => x && typeof x === "object" && !Array.isArray(x))) return v;
  if (v && typeof v === "object" && Array.isArray(v.rows)) {
    // Exported datasets: {rows:[{id, cells:{col:{v}}}]}
    return v.rows.map((r: any) => (r.cells ? Object.fromEntries(Object.entries(r.cells).map(([k, c]: any) => [k, c?.v])) : r));
  }
  return null;
}

function ArrayTable({ rows }: { rows: Record<string, unknown>[] }) {
  const cols = Array.from(new Set(rows.slice(0, 500).flatMap((r) => Object.keys(r))));
  return (
    <div className="scroll-x">
      <table className="simple">
        <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {rows.slice(0, 1000).map((r, i) => (
            <tr key={i}>{cols.map((c) => <td key={c}>{typeof r[c] === "object" && r[c] !== null ? JSON.stringify(r[c]) : String(r[c] ?? "")}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {rows.length > 1000 && <div className="muted small">Showing first 1000 of {rows.length} rows.</div>}
    </div>
  );
}

function Node({ k, v, depth }: { k: string; v: unknown; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  if (v === null || typeof v !== "object") {
    return (
      <div className="json-leaf" style={{ paddingLeft: depth * 14 }}>
        <span className="json-key">{k}</span>: <span className={`json-${v === null ? "null" : typeof v}`}>{JSON.stringify(v)}</span>
      </div>
    );
  }
  const entries = Array.isArray(v) ? v.map((x, i) => [String(i), x] as const) : Object.entries(v as object);
  return (
    <div>
      <div className="json-branch" style={{ paddingLeft: depth * 14 }} onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} <span className="json-key">{k}</span> <span className="muted tiny">{Array.isArray(v) ? `[${entries.length}]` : `{${entries.length}}`}</span>
      </div>
      {open && entries.slice(0, 500).map(([ck, cv]) => <Node key={ck} k={ck} v={cv} depth={depth + 1} />)}
      {open && entries.length > 500 && <div className="muted tiny" style={{ paddingLeft: (depth + 1) * 14 }}>… {entries.length - 500} more</div>}
    </div>
  );
}
