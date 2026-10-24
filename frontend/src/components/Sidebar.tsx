import { useState } from "react";
import { send } from "../api";
import type { Case, Conversation } from "../types";

type Props = {
  cases: Case[];
  activeCaseId: string | null;
  onSelectCase: (id: string) => void;
  onCreateCase: (label: string) => Promise<void>;
  onCaseDeleted: () => void;
  conversations: Conversation[];
  activeConvId: string | null;
  onSelectConv: (id: string) => void;
  onNewConv: () => void;
  onLogout: () => void;
};

export default function Sidebar(p: Props) {
  const [label, setLabel] = useState("");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [note, setNote] = useState("");

  return (
    <nav className="sidebar">
      <div className="brand">Medical Assistant <span className="badge local">local</span></div>
      <form
        className="new-case"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!label.trim()) return;
          await p.onCreateCase(label.trim());
          setLabel("");
        }}
      >
        <input placeholder="New case label" value={label} onChange={(e) => setLabel(e.target.value)} maxLength={120} />
        <button>Add</button>
      </form>
      <div className="section-title">Cases</div>
      <ul className="list">
        {p.cases.map((c) => (
          <li key={c.id} className={c.id === p.activeCaseId ? "active" : ""}>
            <button className="link" onClick={() => p.onSelectCase(c.id)}>{c.label}</button>
            {c.id === p.activeCaseId && (
              confirmDelete === c.id ? (
                <span className="confirm">
                  Delete all records, files and chat?
                  <button className="danger small" onClick={async () => {
                    const r = await send("DELETE", `/api/cases/${c.id}`);
                    setNote(r.note);
                    setConfirmDelete(null);
                    p.onCaseDeleted();
                  }}>Delete</button>
                  <button className="small" onClick={() => setConfirmDelete(null)}>Keep</button>
                </span>
              ) : (
                <button className="icon" title="Delete case" onClick={() => setConfirmDelete(c.id)}>×</button>
              )
            )}
          </li>
        ))}
      </ul>
      {note && <p className="muted small">{note}</p>}
      {p.activeCaseId && (
        <>
          <div className="section-title row between">
            Conversations <button className="small" onClick={p.onNewConv}>New</button>
          </div>
          <ul className="list">
            {p.conversations.map((c) => (
              <li key={c.id} className={c.id === p.activeConvId ? "active" : ""}>
                <button className="link" onClick={() => p.onSelectConv(c.id)}>{c.title}</button>
              </li>
            ))}
          </ul>
        </>
      )}
      <div className="grow" />
      <button className="link muted" onClick={p.onLogout}>Sign out</button>
    </nav>
  );
}
