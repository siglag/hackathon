import { useCallback, useEffect, useRef, useState } from "react";
import { get, post, ApiError } from "./api";
import type { Artifact, Case, Conversation, Dataset, Doc, Selection } from "./types";
import Sidebar from "./components/Sidebar";
import Chat from "./components/Chat";
import ArtifactPanel from "./components/ArtifactPanel";
import PairPhone from "./components/PairPhone";
import StatusBar from "./components/StatusBar";
import { FilesIcon, LogoIcon, NewChatIcon, PhoneIcon, SidebarIcon } from "./components/icons";

type AuthState = { setup_needed: boolean; authenticated: boolean; kind: string | null };

export type Items = { documents: Doc[]; datasets: Dataset[]; artifacts: Artifact[] };

export default function App() {
  const [auth, setAuth] = useState<AuthState | null>(null);
  const refreshAuth = useCallback(() => get<AuthState>("/api/auth/state").then(setAuth), []);
  useEffect(() => {
    refreshAuth();
  }, [refreshAuth]);
  if (!auth) return <div className="center muted"><span className="spinner" /></div>;
  if (!auth.authenticated || auth.kind !== "doctor") return <Login setup={auth.setup_needed} onDone={refreshAuth} />;
  return <Workspace onLogout={refreshAuth} />;
}

function Login({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr("");
    try {
      await post(setup ? "/api/auth/setup" : "/api/auth/login", { username, password });
      onDone();
    } catch (x) {
      setErr((x as Error).message);
    }
  };
  return (
    <div className="auth">
      <div className="auth-logo"><LogoIcon size={32} /></div>
      <form className="auth-form" onSubmit={submit}>
        <h1>{setup ? "Create your account" : "Welcome back"}</h1>
        <p className="muted">{setup ? "Set up the doctor account for this workstation." : "Sign in to this workstation."}</p>
        <input placeholder="Username" aria-label="Username" value={username} onChange={(e) => setUsername(e.target.value)}
          autoComplete="username" required />
        <input type="password" placeholder={setup ? "Password (min. 10 characters)" : "Password"} aria-label="Password"
          value={password} onChange={(e) => setPassword(e.target.value)}
          autoComplete={setup ? "new-password" : "current-password"} required />
        {err && <div className="error">{err}</div>}
        <button className="primary big">{setup ? "Create account" : "Continue"}</button>
        <p className="muted small">All records, OCR and model inference stay on this workstation.</p>
      </form>
    </div>
  );
}

function Workspace({ onLogout }: { onLogout: () => void }) {
  const [cases, setCases] = useState<Case[]>([]);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [convs, setConvs] = useState<Conversation[]>([]);
  const [convId, setConvId] = useState<string | null>(null);
  const [items, setItems] = useState<Items>({ documents: [], datasets: [], artifacts: [] });
  const [selection, setSelection] = useState<Selection | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(() => safeGet("sidebarOpen") !== "0" && window.innerWidth > 900);
  const [pairing, setPairing] = useState(false);
  const [panelWidth, setPanelWidth] = useState(() => Number(safeGet("panelWidth")) || 560);
  const dragging = useRef(false);

  const loadCases = useCallback(async () => {
    const cs = await get<Case[]>("/api/cases");
    setCases(cs);
    return cs;
  }, []);

  const loadItems = useCallback(async () => {
    if (!caseId) return;
    try {
      setItems(await get<Items>(`/api/cases/${caseId}/items`));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) onLogout();
    }
  }, [caseId, onLogout]);

  useEffect(() => {
    loadCases().then((cs) => cs.length && setCaseId((c) => c ?? cs[0].id));
  }, [loadCases]);

  useEffect(() => {
    if (!caseId) return;
    setSelection(null);
    setPanelOpen(false);
    get(`/api/cases/${caseId}`).then((c) => {
      setConvs(c.conversations);
      setConvId(c.conversations[c.conversations.length - 1]?.id ?? null);
    });
    loadItems();
  }, [caseId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Poll items while background work is in progress (uploads from the phone arrive this way).
  useEffect(() => {
    const t = setInterval(loadItems, 4000);
    return () => clearInterval(t);
  }, [loadItems]);

  // Like a canvas: opening an item slides the side panel in.
  const select = useCallback((s: Selection | null) => {
    setSelection(s);
    if (s) setPanelOpen(true);
  }, []);

  const toggleSidebar = () => {
    setSidebarOpen((o) => {
      safeSet("sidebarOpen", o ? "0" : "1");
      return !o;
    });
  };
  const closeSidebarOnMobile = () => window.innerWidth <= 900 && setSidebarOpen(false);

  const createCase = async (label: string) => {
    const c = await post("/api/cases", { label });
    await loadCases();
    setCaseId(c.id);
    closeSidebarOnMobile();
  };
  const newConversation = async () => {
    if (!caseId) return;
    const c = await post(`/api/cases/${caseId}/conversations`);
    const full = await get(`/api/cases/${caseId}`);
    setConvs(full.conversations);
    setConvId(c.id);
    closeSidebarOnMobile();
  };

  const activeCase = cases.find((c) => c.id === caseId) ?? null;
  const pendingReview = items.documents.filter((d) => d.unreviewed_critical > 0).length;

  const onDrag = (e: React.PointerEvent) => {
    if (!dragging.current) return;
    const w = Math.min(Math.max(window.innerWidth - e.clientX, 320), window.innerWidth - 480);
    setPanelWidth(w);
  };

  return (
    <div className={`app ${sidebarOpen ? "" : "sidebar-closed"}`} onPointerMove={onDrag} onPointerUp={() => {
      if (dragging.current) safeSet("panelWidth", String(panelWidth));
      dragging.current = false;
    }}>
      {sidebarOpen && <div className="scrim" onClick={toggleSidebar} />}
      <Sidebar
        open={sidebarOpen}
        onToggle={toggleSidebar}
        cases={cases}
        activeCaseId={caseId}
        onSelectCase={(id) => {
          setCaseId(id);
          closeSidebarOnMobile();
        }}
        onCreateCase={createCase}
        onCaseDeleted={async () => {
          const cs = await loadCases();
          setCaseId(cs[0]?.id ?? null);
          setItems({ documents: [], datasets: [], artifacts: [] });
        }}
        conversations={convs}
        activeConvId={convId}
        onSelectConv={(id) => {
          setConvId(id);
          closeSidebarOnMobile();
        }}
        onNewConv={newConversation}
        onLogout={async () => {
          await post("/api/auth/logout");
          onLogout();
        }}
      />
      <main className="main">
        <header className="topbar">
          <div className="row">
            {!sidebarOpen && (
              <>
                <button className="icon-btn" title="Open sidebar" onClick={toggleSidebar}><SidebarIcon /></button>
                {activeCase && <button className="icon-btn" title="New chat" onClick={newConversation}><NewChatIcon /></button>}
              </>
            )}
            <div className="title-pill">
              <span className="title-text">{activeCase?.label ?? "Medical Assistant"}</span>
              <StatusBar />
            </div>
          </div>
          {activeCase && (
            <div className="row gap-sm">
              <button className="ghost" onClick={() => setPairing(true)} title="Scan with phone">
                <PhoneIcon size={18} /><span className="hide-sm">Scan with phone</span>
              </button>
              <button className={`icon-btn ${panelOpen ? "on" : ""}`} title={panelOpen ? "Close files" : "Case files"}
                onClick={() => setPanelOpen(!panelOpen)}>
                <FilesIcon />
                {pendingReview > 0 && <span className="dot-badge">{pendingReview}</span>}
              </button>
            </div>
          )}
        </header>
        {activeCase ? (
          convId && (
            <Chat
              key={convId}
              caseId={activeCase.id}
              conversationId={convId}
              items={items}
              onItemsChanged={loadItems}
              onSelect={select}
            />
          )
        ) : (
          <div className="hero">
            <h1>What case are we working on?</h1>
            <p className="muted">Create a case in the sidebar to begin. Each case keeps its own records, chat and files.</p>
            {!sidebarOpen && <button className="primary" onClick={toggleSidebar}>Open sidebar</button>}
          </div>
        )}
      </main>
      {activeCase && panelOpen && (
        <>
          <div className="resizer" onPointerDown={(e) => {
            dragging.current = true;
            (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
          }} />
          <aside className="panel" style={{ width: panelWidth }}>
            <ArtifactPanel caseId={activeCase.id} items={items} selection={selection} onSelect={setSelection}
              onChanged={loadItems} onClose={() => {
                setPanelOpen(false);
                setSelection(null);
              }} />
          </aside>
        </>
      )}
      {pairing && activeCase && <PairPhone caseId={activeCase.id} caseLabel={activeCase.label} onClose={() => {
        setPairing(false);
        loadItems();
      }} />}
    </div>
  );
}

function safeGet(k: string): string | null {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
}
function safeSet(k: string, v: string) {
  try {
    localStorage.setItem(k, v);
  } catch {
    /* ignore */
  }
}
