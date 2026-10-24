import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { get, post } from "../api";

/** Issue a short-lived, single-use pairing code (rendered locally as a QR). */
export default function PairPhone({ caseId, caseLabel, onClose }: { caseId: string; caseLabel: string; onClose: () => void }) {
  const [pairing, setPairing] = useState<{ pairing_id: string; url: string; expires_at: number } | null>(null);
  const [state, setState] = useState("issued");
  const [device, setDevice] = useState("");
  const [err, setErr] = useState("");
  const canvas = useRef<HTMLCanvasElement>(null);
  const insecure = window.location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(window.location.hostname);

  useEffect(() => {
    post("/api/pairings", { case_id: caseId }).then((r) => {
      // The token is in the URL fragment: browsers never send it to the server, so it cannot appear in access logs.
      setPairing({ pairing_id: r.pairing_id, url: `${window.location.origin}${r.path}`, expires_at: r.expires_at });
    }).catch((e) => setErr(e.message));
  }, [caseId]);

  useEffect(() => {
    if (pairing && canvas.current) QRCode.toCanvas(canvas.current, pairing.url, { width: 240, margin: 1 });
  }, [pairing]);

  useEffect(() => {
    if (!pairing) return;
    const t = setInterval(async () => {
      const s = await get(`/api/pairings/${pairing.pairing_id}`);
      setState(s.state);
      setDevice(s.device_label ?? "");
    }, 1500);
    return () => clearInterval(t);
  }, [pairing]);

  const secondsLeft = pairing ? Math.max(0, Math.round(pairing.expires_at - Date.now() / 1000)) : 0;

  return (
    <div className="modal-back" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>Scan with phone</h3>
        <p className="small">Uploads go only into <strong>{caseLabel}</strong>. The phone gets upload-only access and cannot browse cases.</p>
        {insecure && <div className="warn small">This page is not served over HTTPS. Phone cameras require HTTPS with the clinic's local certificate. Do not use a public relay.</div>}
        {err && <div className="error">{err}</div>}
        {pairing && state === "issued" && (
          <>
            <canvas ref={canvas} />
            <p className="muted small">Single-use code · expires in ~{secondsLeft}s · phone must be on the clinic network.</p>
            <details className="small"><summary>Can't scan?</summary><code className="break">{pairing.url}</code></details>
          </>
        )}
        {state === "expired" && <p className="warn">The code expired. Close and try again.</p>}
        {state === "exchanged" && (
          <div>
            <p>A device (<strong>{device}</strong>) opened the pairing link. Is this your phone?</p>
            <div className="row gap">
              <button className="primary" onClick={() => post(`/api/pairings/${pairing!.pairing_id}/confirm`).then(() => setState("confirmed"))}>Confirm pairing</button>
              <button className="danger" onClick={() => post(`/api/pairings/${pairing!.pairing_id}/revoke`).then(() => setState("revoked"))}>Reject</button>
            </div>
          </div>
        )}
        {state === "confirmed" && (
          <div>
            <p className="ok">Phone paired. Captured pages will appear in this case's records.</p>
            <button className="danger small" onClick={() => post(`/api/pairings/${pairing!.pairing_id}/revoke`).then(() => setState("revoked"))}>Revoke phone access</button>
          </div>
        )}
        {state === "revoked" && <p className="muted">Phone access revoked.</p>}
        <div className="row end"><button onClick={onClose}>Close</button></div>
      </div>
    </div>
  );
}
