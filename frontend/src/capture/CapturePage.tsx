import { useEffect, useRef, useState } from "react";
import { get, newKey, post, uploadFiles } from "../api";

type Page = { id: string; blob: Blob; url: string; rotate: number; crop: { t: number; r: number; b: number; l: number }; warnings: string[] };

// Phone capture: pair with a single-use code, then capture pages with an
// in-browser camera stream (avoids deliberately saving to the photo library).
export default function CapturePage() {
  const [phase, setPhase] = useState<"pairing" | "waiting" | "ready" | "expired" | "revoked" | "error">("pairing");
  const [caseLabel, setCaseLabel] = useState("");
  const [pages, setPages] = useState<Page[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [retakeId, setRetakeId] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [receipts, setReceipts] = useState<any[]>([]);
  const [msg, setMsg] = useState("");
  const [camError, setCamError] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);

  useEffect(() => {
    const m = window.location.hash.match(/t=([A-Za-z0-9_\-]+)/);
    // Remove the token from the address bar and history immediately.
    history.replaceState(null, "", "/capture");
    const exchange = async () => {
      if (m) {
        try {
          await post("/api/pairings/exchange", { token: m[1], device_label: deviceLabel() });
        } catch {
          /* fall through to session check */
        }
      }
      setPhase("waiting");
    };
    exchange();
  }, []);

  useEffect(() => {
    if (phase !== "waiting" && phase !== "ready") return;
    const poll = async () => {
      try {
        const s = await get("/api/phone/session");
        if (s.state === "confirmed") {
          setCaseLabel(s.case_label);
          setReceipts(s.uploads);
          setPhase("ready");
        } else if (s.state === "revoked") setPhase("revoked");
      } catch {
        setPhase((p) => (p === "ready" ? "revoked" : "expired"));
      }
    };
    poll();
    const t = setInterval(poll, phase === "ready" ? 5000 : 1500);
    return () => clearInterval(t);
  }, [phase]);

  useEffect(() => {
    if (phase !== "ready") return;
    if (!navigator.mediaDevices?.getUserMedia) {
      setCamError("Live camera is not available in this browser (HTTPS is required). Use the file picker below.");
      return;
    }
    navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 2560 }, height: { ideal: 1920 } }, audio: false })
      .then((s) => {
        stream.current = s;
        if (video.current) video.current.srcObject = s;
      })
      .catch(() => setCamError("Camera permission was denied or unavailable. Use the file picker below."));
    return () => stream.current?.getTracks().forEach((t) => t.stop());
  }, [phase]);

  const addBlob = async (blob: Blob) => {
    const warnings = await qualityWarnings(blob);
    const p: Page = { id: newKey(), blob, url: URL.createObjectURL(blob), rotate: 0, crop: { t: 0, r: 0, b: 0, l: 0 }, warnings };
    setPages((ps) => {
      if (retakeId) {
        const i = ps.findIndex((x) => x.id === retakeId);
        if (i >= 0) {
          const copy = [...ps];
          URL.revokeObjectURL(copy[i].url);
          copy[i] = p;
          return copy;
        }
      }
      return [...ps, p];
    });
    setRetakeId(null);
  };

  const capture = () => {
    const v = video.current;
    if (!v || !v.videoWidth) return;
    const c = document.createElement("canvas");
    c.width = v.videoWidth;
    c.height = v.videoHeight;
    c.getContext("2d")!.drawImage(v, 0, 0);
    c.toBlob((b) => b && addBlob(b), "image/jpeg", 0.92);
  };

  const move = (i: number, d: number) => setPages((ps) => {
    const j = i + d;
    if (j < 0 || j >= ps.length) return ps;
    const copy = [...ps];
    [copy[i], copy[j]] = [copy[j], copy[i]];
    return copy;
  });

  const upload = async () => {
    setMsg("");
    setProgress(0);
    try {
      const blobs = await Promise.all(pages.map(render));
      const files = blobs.map((b, i) => new File([b], `page-${i + 1}.jpg`, { type: "image/jpeg" }));
      const r = await uploadFiles("/api/phone/uploads", files, setProgress, newKey());
      setMsg(`Uploaded ${r.documents.length} page(s). They are queued for processing.`);
      pages.forEach((p) => URL.revokeObjectURL(p.url));
      setPages([]);
      const s = await get("/api/phone/session");
      setReceipts(s.uploads);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setProgress(null);
    }
  };

  if (phase === "pairing" || phase === "waiting") {
    return <div className="capture center"><div className="card"><h2>Pairing…</h2>
      <p>Confirm this phone on the workstation to continue.</p><span className="spinner" /></div></div>;
  }
  if (phase === "expired") return <div className="capture center"><div className="card"><h2>Code expired</h2><p>Ask for a new QR code on the workstation.</p></div></div>;
  if (phase === "revoked") return <div className="capture center"><div className="card"><h2>Access ended</h2><p>This phone's upload access was revoked or expired.</p></div></div>;

  const ed = pages.find((p) => p.id === editing);
  return (
    <div className="capture">
      <header className="capture-head">Uploading to <strong>{caseLabel}</strong></header>
      {!camError ? (
        <div className="camera">
          <video ref={video} autoPlay playsInline muted />
          <button className="shutter" onClick={capture} aria-label="Capture page">{retakeId ? "Retake" : ""}</button>
        </div>
      ) : (
        <div className="warn">{camError}</div>
      )}
      <label className="button">Choose photo
        <input type="file" accept="image/*" capture="environment" hidden onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) addBlob(f);
          e.target.value = "";
        }} />
      </label>
      <p className="muted tiny">The app does not save captures to your photo library. Screenshots, gallery uploads and device backups are outside its control.</p>
      <div className="pages">
        {pages.map((p, i) => (
          <div key={p.id} className="page-thumb">
            <img src={p.url} style={{ transform: `rotate(${p.rotate}deg)` }} alt={`Page ${i + 1}`} />
            <div className="tiny">Page {i + 1}</div>
            {p.warnings.map((w) => <div key={w} className="warn tiny">{w}</div>)}
            <div className="row wrap gap">
              <button className="tiny" onClick={() => move(i, -1)}>↑</button>
              <button className="tiny" onClick={() => move(i, 1)}>↓</button>
              <button className="tiny" onClick={() => setPages((ps) => ps.map((x) => x.id === p.id ? { ...x, rotate: (x.rotate + 90) % 360 } : x))}>⟳</button>
              <button className="tiny" onClick={() => setEditing(p.id)}>Crop</button>
              <button className="tiny" onClick={() => setRetakeId(p.id)}>Retake</button>
              <button className="tiny danger" onClick={() => setPages((ps) => ps.filter((x) => x.id !== p.id))}>✕</button>
            </div>
          </div>
        ))}
      </div>
      {ed && (
        <div className="crop">
          {(["t", "r", "b", "l"] as const).map((k) => (
            <label key={k} className="small">{({ t: "Top", r: "Right", b: "Bottom", l: "Left" })[k]} {ed.crop[k]}%
              <input type="range" min={0} max={40} value={ed.crop[k]}
                onChange={(e) => setPages((ps) => ps.map((x) => x.id === ed.id ? { ...x, crop: { ...x.crop, [k]: Number(e.target.value) } } : x))} />
            </label>
          ))}
          <button className="small" onClick={() => setEditing(null)}>Done</button>
        </div>
      )}
      {pages.length > 0 && <button className="primary big" disabled={progress !== null} onClick={upload}>Upload {pages.length} page(s)</button>}
      {progress !== null && <div className="upload-bar"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>}
      {msg && <p className="small">{msg}</p>}
      {receipts.length > 0 && (
        <div className="small">
          <h4>Received by the workstation</h4>
          <ul>{receipts.map((r) => <li key={r.id}>{new Date(r.created_at * 1000).toLocaleTimeString()} — {r.state.replace(/_/g, " ")}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

function deviceLabel(): string {
  const ua = navigator.userAgent;
  if (/iPhone|iPad/.test(ua)) return "iPhone/iPad Safari";
  if (/Android/.test(ua)) return "Android browser";
  return "Browser";
}

async function loadImage(blob: Blob): Promise<HTMLImageElement> {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.src = url;
  await img.decode();
  URL.revokeObjectURL(url);
  return img;
}

/** Apply rotation and crop on-device before upload. */
async function render(p: Page): Promise<Blob> {
  const img = await loadImage(p.blob);
  const sx = (img.width * p.crop.l) / 100, sy = (img.height * p.crop.t) / 100;
  const sw = img.width * (1 - (p.crop.l + p.crop.r) / 100), sh = img.height * (1 - (p.crop.t + p.crop.b) / 100);
  const c = document.createElement("canvas");
  const rot = p.rotate % 180 !== 0;
  c.width = rot ? sh : sw;
  c.height = rot ? sw : sh;
  const ctx = c.getContext("2d")!;
  ctx.translate(c.width / 2, c.height / 2);
  ctx.rotate((p.rotate * Math.PI) / 180);
  ctx.drawImage(img, sx, sy, sw, sh, -sw / 2, -sh / 2, sw, sh);
  return new Promise((res) => c.toBlob((b) => res(b!), "image/jpeg", 0.92));
}

/** Rough blur (Laplacian variance) and glare checks on a downscaled copy. */
async function qualityWarnings(blob: Blob): Promise<string[]> {
  try {
    const img = await loadImage(blob);
    const w = 400, h = Math.round((img.height / img.width) * 400);
    const c = document.createElement("canvas");
    c.width = w;
    c.height = h;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(img, 0, 0, w, h);
    const d = ctx.getImageData(0, 0, w, h).data;
    const g = new Float32Array(w * h);
    let bright = 0;
    for (let i = 0; i < w * h; i++) {
      g[i] = 0.299 * d[i * 4] + 0.587 * d[i * 4 + 1] + 0.114 * d[i * 4 + 2];
      if (g[i] >= 250) bright++;
    }
    let sum = 0, sum2 = 0, n = 0;
    for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const lap = g[i - 1] + g[i + 1] + g[i - w] + g[i + w] - 4 * g[i];
      sum += lap;
      sum2 += lap * lap;
      n++;
    }
    const variance = sum2 / n - (sum / n) ** 2;
    const out = [];
    if (variance < 40) out.push("Looks blurry — retake for reliable numbers.");
    if (bright / (w * h) > 0.08) out.push("Glare detected — tilt the page or move away from lights.");
    if (img.width < 1000) out.push("Low resolution.");
    return out;
  } catch {
    return [];
  }
}
