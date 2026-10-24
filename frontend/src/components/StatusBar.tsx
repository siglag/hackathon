import { useEffect, useState } from "react";
import { get } from "../api";

export default function StatusBar() {
  const [s, setS] = useState<any>(null);
  useEffect(() => {
    const load = () => get("/api/system/status").then(setS).catch(() => setS(null));
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);
  if (!s) return null;
  const dot = (ok: boolean, label: string, title: string) => (
    <span className={`status ${ok ? "ok" : "bad"}`} title={title}>{label}</span>
  );
  return (
    <span className="row gap small">
      {dot(s.model.ok, "Model", s.model.ok ? `${s.model.model} via local Ollama ${s.model.version ?? ""}` : "Local model unavailable")}
      {dot(s.ocr.ok, "OCR", s.ocr.ok ? s.ocr.engine : `OCR unavailable: ${s.ocr.engine}`)}
      {dot(!!s.gateway.ok, "Evidence", s.gateway.ok ? `Gateway up · ${s.gateway.provider}` : "Evidence gateway offline; local work continues")}
    </span>
  );
}
