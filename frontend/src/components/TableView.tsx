import { useCallback, useEffect, useRef, useState } from "react";
import { get, post } from "../api";
import type { Dataset, Row } from "../types";

const ROW_H = 30;
const BLOCK = 200;

type Props = { dataset: Dataset; readOnly?: boolean; onNewVersion?: (id: string) => void };

/** Virtualized, server-paged table with sort, search, filter and cell editing (edits create a new version). */
export default function TableView({ dataset, readOnly, onNewVersion }: Props) {
  const [total, setTotal] = useState(dataset.row_count);
  const [blocks, setBlocks] = useState<Record<number, Row[]>>({});
  const [sort, setSort] = useState<{ col: string; desc: boolean } | null>(null);
  const [search, setSearch] = useState("");
  const [scrollTop, setScrollTop] = useState(0);
  const [height, setHeight] = useState(400);
  const [edit, setEdit] = useState<{ row: string; col: string; value: string } | null>(null);
  const [filter, setFilter] = useState({ col: dataset.columns[0]?.name ?? "", op: "contains", value: "" });
  const [err, setErr] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const loading = useRef(new Set<number>());

  const key = `${dataset.id}|${sort?.col}|${sort?.desc}|${search}`;
  useEffect(() => {
    setBlocks({});
    loading.current.clear();
  }, [key]);

  const load = useCallback(async (b: number) => {
    if (loading.current.has(b)) return;
    loading.current.add(b);
    const qs = new URLSearchParams({ offset: String(b * BLOCK), limit: String(BLOCK) });
    if (sort) {
      qs.set("sort", sort.col);
      qs.set("desc", String(sort.desc));
    }
    if (search) qs.set("q", search);
    const r = await get(`/api/datasets/${dataset.id}/rows?${qs}`);
    setTotal(r.total);
    setBlocks((x) => ({ ...x, [b]: r.rows }));
  }, [dataset.id, sort, search]);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setHeight(el.clientHeight));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const first = Math.floor(scrollTop / ROW_H);
  const count = Math.ceil(height / ROW_H) + 10;
  useEffect(() => {
    const b0 = Math.floor(first / BLOCK);
    const b1 = Math.floor((first + count) / BLOCK);
    for (let b = b0; b <= b1; b++) if (!blocks[b]) load(b);
  }, [first, count, blocks, load]);

  const rowAt = (i: number): Row | undefined => blocks[Math.floor(i / BLOCK)]?.[i % BLOCK];
  const cols = dataset.columns;

  const transform = async (ops: any[]) => {
    setErr("");
    try {
      const r = await post(`/api/datasets/${dataset.id}/transform`, { ops });
      onNewVersion?.(r.id);
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  const visible = [];
  for (let i = first; i < Math.min(total, first + count); i++) visible.push(i);

  return (
    <div className="table-view">
      <div className="row wrap gap small toolbar">
        <input placeholder="Search" value={search} onChange={(e) => setSearch(e.target.value)} />
        {!readOnly && (
          <>
            <select value={filter.col} onChange={(e) => setFilter({ ...filter, col: e.target.value })}>
              {cols.map((c) => <option key={c.name}>{c.name}</option>)}
            </select>
            <select value={filter.op} onChange={(e) => setFilter({ ...filter, op: e.target.value })}>
              {["contains", "eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"].map((o) => <option key={o}>{o}</option>)}
            </select>
            {!["is_null", "not_null"].includes(filter.op) && (
              <input placeholder="value" value={filter.value} onChange={(e) => setFilter({ ...filter, value: e.target.value })} />
            )}
            <button className="small" onClick={() => transform([{ op: "filter", column: filter.col, operator: filter.op,
              value: filter.value !== "" && !isNaN(Number(filter.value)) && ["gt", "gte", "lt", "lte"].includes(filter.op) ? Number(filter.value) : filter.value }])}>
              Filter → new version
            </button>
            {sort && <button className="small" onClick={() => transform([{ op: "sort", column: sort.col, direction: sort.desc ? "desc" : "asc" }])}>Save sort as version</button>}
          </>
        )}
        <span className="muted">{total} rows</span>
      </div>
      {err && <div className="error small">{err}</div>}
      <div className="grid-head" style={{ gridTemplateColumns: `60px repeat(${cols.length}, minmax(110px, 1fr))` }}>
        <div>#</div>
        {cols.map((c) => (
          <div key={c.name} className="th" onClick={() => setSort(sort?.col === c.name ? (sort.desc ? null : { col: c.name, desc: true }) : { col: c.name, desc: false })}>
            {c.name} <span className="muted tiny">{c.type}</span> {sort?.col === c.name ? (sort.desc ? "▼" : "▲") : ""}
          </div>
        ))}
      </div>
      <div className="grid-body" ref={box} onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}>
        <div style={{ height: total * ROW_H, position: "relative" }}>
          {visible.map((i) => {
            const r = rowAt(i);
            return (
              <div key={i} className="grid-row" style={{ top: i * ROW_H, height: ROW_H, gridTemplateColumns: `60px repeat(${cols.length}, minmax(110px, 1fr))` }}>
                <div className="muted tiny">{r?.id ?? "…"}</div>
                {cols.map((c) => {
                  const cell = r?.cells[c.name];
                  const editing = edit && r && edit.row === r.id && edit.col === c.name;
                  const unrev = cell?.rs === "unreviewed";
                  const title = [cell?.r !== undefined && cell.r !== null ? `raw: ${cell.r}` : "", cell?.unit ? `unit: ${cell.unit}` : "",
                    cell?.src ? `source: ${JSON.stringify(cell.src)}` : "", cell?.unavailable ?? "", cell?.formula ? `formula: ${cell.formula}` : "",
                    unrev ? "UNREVIEWED" : ""].filter(Boolean).join("\n");
                  return (
                    <div key={c.name} className={`td ${unrev ? "unreviewed" : ""} ${cell?.unavailable ? "unavailable" : ""}`} title={title}
                      onDoubleClick={() => !readOnly && r && setEdit({ row: r.id, col: c.name, value: cell?.v == null ? "" : String(cell.v) })}>
                      {editing ? (
                        <input autoFocus value={edit.value} onChange={(e) => setEdit({ ...edit, value: e.target.value })}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setEdit(null);
                            if (e.key === "Enter") {
                              const v = edit.value === "" ? null : !isNaN(Number(edit.value)) && typeof cell?.v === "number" ? Number(edit.value) : edit.value;
                              transform([{ op: "edit_cell", row_id: edit.row, column: edit.col, value: v }]);
                              setEdit(null);
                            }
                          }} onBlur={() => setEdit(null)} />
                      ) : cell?.unavailable ? "[unavailable]" : fmt(cell?.v)}
                      {cell?.unit && !editing ? <span className="muted tiny"> {cell.unit}</span> : null}
                    </div>
                  );
                })}
              </div>
            );
          })}
        </div>
      </div>
      {!readOnly && <div className="muted tiny">Double-click a cell to correct it (creates a new version). Hover for raw text and source.</div>}
    </div>
  );
}

function fmt(v: any): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}
