import { useState } from "react";
import { send } from "../api";
import type { Case, Conversation } from "../types";
import { FolderIcon, LogoIcon, LogoutIcon, NewChatIcon, PlusIcon, SidebarIcon, TrashIcon } from "./icons";

type Props = {
  open: boolean;
  onToggle: () => void;
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
  const [adding, setAdding] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [note, setNote] = useState("");

  return (
    <nav className={`sidebar ${p.open ? "open" : ""}`}>
      <div className="sidebar-top">
        <span className="brand-mark" title="Local Medical Assistant"><LogoIcon size={22} /></span>
        <button className="icon-btn" title="Close sidebar" onClick={p.onToggle}><SidebarIcon /></button>
      </div>

      <div className="sidebar-scroll">
        <div className="nav-group">
          <button className="nav-item" onClick={p.onNewConv} disabled={!p.activeCaseId}>
            <NewChatIcon size={18} /> New chat
          </button>
        </div>

        <div className="nav-heading">Cases</div>
        <div className="nav-group">
          {adding ? (
            <form
              className="nav-input"
              onSubmit={async (e) => {
                e.preventDefault();
                if (!label.trim()) return;
                await p.onCreateCase(label.trim());
                setLabel("");
                setAdding(false);
              }}
            >
              <FolderIcon size={18} />
              <input autoFocus placeholder="Case label" value={label} maxLength={120}
                onChange={(e) => setLabel(e.target.value)}
                onBlur={() => !label.trim() && setAdding(false)}
                onKeyDown={(e) => e.key === "Escape" && setAdding(false)} />
            </form>
          ) : (
            <button className="nav-item" onClick={() => setAdding(true)}><PlusIcon size={18} /> New case</button>
          )}
          {p.cases.map((c) => (
            <div key={c.id} className={`nav-item has-actions ${c.id === p.activeCaseId ? "active" : ""}`}>
              <button className="nav-link" onClick={() => p.onSelectCase(c.id)}>
                <FolderIcon size={18} /><span className="truncate">{c.label}</span>
              </button>
              {c.id === p.activeCaseId && confirmDelete !== c.id && (
                <button className="nav-action" title="Delete case" onClick={() => setConfirmDelete(c.id)}><TrashIcon size={16} /></button>
              )}
              {confirmDelete === c.id && (
                <div className="nav-confirm">
                  <span>Delete all records, files and chat?</span>
                  <div className="row gap-sm">
                    <button className="danger small" onClick={async () => {
                      const r = await send("DELETE", `/api/cases/${c.id}`);
                      setNote(r.note);
                      setConfirmDelete(null);
                      p.onCaseDeleted();
                    }}>Delete</button>
                    <button className="small" onClick={() => setConfirmDelete(null)}>Keep</button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
        {note && <p className="muted small nav-note">{note}</p>}

        {p.activeCaseId && p.conversations.length > 0 && (
          <>
            <div className="nav-heading">Chats</div>
            <div className="nav-group">
              {[...p.conversations].reverse().map((c) => (
                <button key={c.id} className={`nav-item ${c.id === p.activeConvId ? "active" : ""}`} onClick={() => p.onSelectConv(c.id)}>
                  <span className="truncate">{c.title}</span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      <div className="sidebar-bottom">
        <button className="nav-item profile" onClick={p.onLogout} title="Sign out">
          <span className="avatar">Dr</span>
          <span className="grow">
            <span className="block">Doctor</span>
            <span className="muted tiny block">Local workstation</span>
          </span>
          <LogoutIcon size={18} />
        </button>
      </div>
    </nav>
  );
}
