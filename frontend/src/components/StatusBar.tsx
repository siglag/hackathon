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
    <span className={`status ${ok ? "ok" : "bad"}`} title={title}><i />{label}</span>
  );
  return (
    <span className="status-row">
      {(s.model.cloud || s.ocr.cloud) && (
        <span className="cloud-label" title="Cloud chat sends case content, and cloud OCR sends page images, to OpenRouter. Use synthetic data only.">Cloud processing</span>
      )}
      {dot(s.model.ok, s.model.cloud ? "DeepSeek" : "Model", s.model.ok ? (s.model.cloud ? `${s.model.model} via OpenRouter (cloud)` : `${s.model.model} via local Ollama ${s.model.version ?? ""}`) : `Model unavailable: ${s.model.error ?? ""}`)}
      {dot(s.ocr.ok, s.ocr.cloud ? "Cloud OCR" : "OCR", s.ocr.ok ? s.ocr.engine : `OCR unavailable: ${s.ocr.engine}`)}
      {dot(!!s.gateway.ok, "Evidence", s.gateway.ok ? `Gateway up · ${s.gateway.provider}` : "Evidence gateway offline; local work continues")}
    </span>
  );
}
