import { useState } from "react";

export type Box = { id: string; bbox: number[]; label?: string; tone?: "ok" | "warn" | "focus" };

/** Zoom, rotate (view only) and OCR bounding-box overlays in image pixel space. */
export default function ImageView({ src, boxes = [], naturalWidth, naturalHeight, focusId, onBoxClick }: {
  src: string;
  boxes?: Box[];
  naturalWidth?: number;
  naturalHeight?: number;
  focusId?: string | null;
  onBoxClick?: (id: string) => void;
}) {
  const [zoom, setZoom] = useState(1);
  const [rot, setRot] = useState(0);
  const [nat, setNat] = useState<{ w: number; h: number } | null>(naturalWidth && naturalHeight ? { w: naturalWidth, h: naturalHeight } : null);
  const [showBoxes, setShowBoxes] = useState(true);
  return (
    <div className="image-view">
      <div className="row gap small">
        <button className="small" onClick={() => setZoom(Math.max(0.25, zoom - 0.25))}>−</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button className="small" onClick={() => setZoom(Math.min(4, zoom + 0.25))}>+</button>
        <button className="small" onClick={() => setRot((rot + 90) % 360)}>Rotate view</button>
        {boxes.length > 0 && (
          <label className="check"><input type="checkbox" checked={showBoxes} onChange={(e) => setShowBoxes(e.target.checked)} /> OCR boxes</label>
        )}
      </div>
      <div className="image-scroll">
        <div className="image-stage" style={{ width: `${zoom * 100}%`, transform: `rotate(${rot}deg)` }}>
          <img src={src} alt="Record page" onLoad={(e) => {
            const im = e.currentTarget;
            if (!nat) setNat({ w: im.naturalWidth, h: im.naturalHeight });
          }} />
          {showBoxes && nat && boxes.map((b) => (
            <div key={b.id} className={`ocr-box ${b.tone ?? ""} ${focusId === b.id ? "focus" : ""}`} title={b.label}
              onClick={() => onBoxClick?.(b.id)}
              style={{
                left: `${(b.bbox[0] / nat.w) * 100}%`, top: `${(b.bbox[1] / nat.h) * 100}%`,
                width: `${((b.bbox[2] - b.bbox[0]) / nat.w) * 100}%`, height: `${((b.bbox[3] - b.bbox[1]) / nat.h) * 100}%`,
              }} />
          ))}
        </div>
      </div>
    </div>
  );
}
