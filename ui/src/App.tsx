import { useEffect, useState } from "react";
import { api, type ChatResponse, type Customer, type InitialMemory } from "./api";
import { Chat } from "./Chat";
import { MemoryPanel } from "./MemoryPanel";
import { SidePanel } from "./SidePanel";
import { SessionsPanel, type SessionTab } from "./SessionsPanel";

type Stage =
  | { view: "boot" }
  | { view: "pick" }
  | { view: "chat"; customer: Customer };

export default function App() {
  const [stage, setStage] = useState<Stage>({ view: "boot" });
  const [panelRefresh, setPanelRefresh] = useState(0);
  const [usedIds, setUsedIds] = useState<Set<string>>(new Set());
  // On phones only one column fits; the bottom nav switches between these.
  const [mobileView, setMobileView] = useState<"chat" | "sessions" | "memory">("chat");

  const [sessions, setSessions] = useState<SessionTab[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  useEffect(() => {
    setStage({ view: "pick" });
  }, []);

  const [deleteSessionTarget, setDeleteSessionTarget] = useState<{
    id: string;
    name: string;
  } | null>(null);

  async function enterCustomer(customer: Customer) {
    const existingSessions = await api.listSessions(customer.id).catch(() => []);
    const tabs: SessionTab[] = existingSessions.map((s, i) => ({
      id: s.id,
      label: s.title || `Session ${existingSessions.length - i}`,
      createdAt: s.created_at,
      messageCount: s.message_count,
    }));

    // always open a fresh session on entry; older ones stay switchable
    const { session_id } = await api.newSession(customer.id);
    const newTab: SessionTab = {
      id: session_id,
      label: `Session ${tabs.length + 1}`,
      createdAt: new Date().toISOString(),
      messageCount: 0,
    };
    tabs.unshift(newTab);

    setSessions(tabs);
    setActiveSessionId(session_id);
    setUsedIds(new Set());
    setPanelRefresh((n) => n + 1);
    setStage({ view: "chat", customer });
  }

  async function addNewSession(customer: Customer) {
    const { session_id } = await api.newSession(customer.id);
    const newTab: SessionTab = {
      id: session_id,
      label: `Session ${sessions.length + 1}`,
      createdAt: new Date().toISOString(),
      messageCount: 0,
    };
    setSessions((prev) => [newTab, ...prev]);
    setActiveSessionId(session_id);
    setUsedIds(new Set());
    setPanelRefresh((n) => n + 1);
  }

  async function executeDeleteSession() {
    if (!deleteSessionTarget) return;
    const { id } = deleteSessionTarget;
    setDeleteSessionTarget(null);
    
    try {
      await api.deleteSession(id);
      const updated = sessions.filter((s) => s.id !== id);
      setSessions(updated);
      
      if (activeSessionId === id) {
        if (updated.length > 0) {
          setActiveSessionId(updated[0].id);
        } else {
          setActiveSessionId(null);
        }
      }
      setPanelRefresh((n) => n + 1);
    } catch (e) {
      console.error("Failed to delete session", e);
    }
  }

  function switchSession(sessionId: string) {
    setActiveSessionId(sessionId);
    setPanelRefresh((n) => n + 1);
  }

  async function renameSession(id: string, title: string) {
    const trimmed = title.trim();
    if (!trimmed) return;
    const prev = sessions.find((s) => s.id === id)?.label;
    // optimistic: update the label immediately, roll back if the request fails
    setSessions((list) => list.map((s) => (s.id === id ? { ...s, label: trimmed } : s)));
    try {
      await api.renameSession(id, trimmed);
    } catch (e) {
      console.error("Failed to rename session", e);
      if (prev !== undefined) {
        setSessions((list) => list.map((s) => (s.id === id ? { ...s, label: prev } : s)));
      }
    }
  }

  function onTurn(r: ChatResponse) {
    setUsedIds(new Set(r.memories_used.map((m) => m.id)));
    setPanelRefresh((n) => n + 1);
    setSessions((prev) =>
      prev.map((s) =>
        s.id === activeSessionId ? { ...s, messageCount: s.messageCount + 2 } : s
      )
    );
  }

  if (stage.view === "boot") return <Center>loading…</Center>;
  if (stage.view === "pick") return <Picker onPick={enterCustomer} />;

  const { customer } = stage;

  return (
    <div className="h-dvh flex flex-col bg-slate-50 text-slate-900 font-sans antialiased">
      <header className="flex items-center gap-2 md:gap-3 px-3 md:px-6 h-16 bg-white border-b border-slate-200/80 shrink-0 shadow-sm z-10">
        <div className="flex items-center gap-2">
          <div className="bg-indigo-600 text-white p-1.5 rounded-lg">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <span className="font-bold tracking-tight text-lg bg-gradient-to-r from-indigo-600 to-indigo-800 bg-clip-text text-transparent">Ledger</span>
        </div>
        <span className="text-slate-300">|</span>
        <span className="text-sm font-semibold text-slate-700">{customer.name}</span>

        <div className="ml-auto">
          <button
            onClick={() => {
              setSessions([]);
              setActiveSessionId(null);
              setStage({ view: "pick" });
            }}
            className="flex items-center gap-1.5 text-xs font-semibold px-2.5 md:px-3.5 py-2 border border-slate-200 text-slate-600 hover:bg-slate-50 rounded-xl transition-all duration-200"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
            </svg>
            <span className="hidden sm:inline">Switch Customer</span>
          </button>
        </div>
      </header>

      <div className="flex flex-1 min-h-0 bg-slate-50 p-2 md:p-4 gap-2 md:gap-4">
        {/* Left panel: desktop only; on mobile it's the "Sessions" tab below */}
        <div className="hidden md:flex shrink-0">
          <SidePanel
            side="left"
            title="Sessions"
            storageKey="ledger:sessions"
            defaultWidth={280}
            icon={
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            }
          >
            <SessionsPanel
              sessions={sessions}
              activeId={activeSessionId}
              latestId={sessions[0]?.id ?? null}
              onSwitch={switchSession}
              onNew={() => addNewSession(customer)}
              onDelete={(id, label) => setDeleteSessionTarget({ id, name: label })}
              onRename={renameSession}
            />
          </SidePanel>
        </div>

        <main
          className={`${
            mobileView === "chat" ? "flex" : "hidden"
          } md:flex flex-1 min-w-0 flex-col bg-white rounded-2xl border border-slate-200/60 shadow-sm overflow-hidden`}
        >
          {activeSessionId ? (
            <Chat
              key={activeSessionId}
              customer={customer}
              sessionId={activeSessionId}
              onTurn={onTurn}
            />
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-slate-400">
              <svg className="w-12 h-12 text-slate-300 mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
              <p className="text-sm font-medium">No session active</p>
              <button
                onClick={() => addNewSession(customer)}
                className="mt-3 text-xs bg-indigo-600 hover:bg-indigo-700 text-white font-semibold px-4 py-2 rounded-xl"
              >
                Create new session
              </button>
            </div>
          )}
        </main>

        {/* Mobile-only Sessions view (desktop uses the left panel) */}
        <section
          className={`${
            mobileView === "sessions" ? "flex" : "hidden"
          } md:hidden flex-1 min-w-0 flex-col bg-white rounded-2xl border border-slate-200/60 shadow-sm overflow-hidden`}
        >
          <div className="flex-1 min-h-0 flex flex-col">
            <SessionsPanel
              sessions={sessions}
              activeId={activeSessionId}
              latestId={sessions[0]?.id ?? null}
              onSwitch={(id) => {
                switchSession(id);
                setMobileView("chat");
              }}
              onNew={() => {
                addNewSession(customer);
                setMobileView("chat");
              }}
              onDelete={(id, label) => setDeleteSessionTarget({ id, name: label })}
              onRename={renameSession}
            />
          </div>
        </section>

        {/* Mobile-only Memory view (desktop uses the right panel) */}
        <section
          className={`${
            mobileView === "memory" ? "flex" : "hidden"
          } md:hidden flex-1 min-w-0 flex-col bg-white rounded-2xl border border-slate-200/60 shadow-sm overflow-hidden`}
        >
          <div className="flex-1 min-h-0 flex flex-col">
            <MemoryPanel customerId={customer.id} refresh={panelRefresh} usedIds={usedIds} />
          </div>
        </section>

        {/* Right panel: desktop only; on mobile it's the "Memory" tab below */}
        <div className="hidden md:flex shrink-0">
          <SidePanel
            side="right"
            title="Memory"
            storageKey="ledger:memory"
            defaultWidth={340}
            icon={
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            }
          >
            <MemoryPanel customerId={customer.id} refresh={panelRefresh} usedIds={usedIds} />
          </SidePanel>
        </div>
      </div>

      {/* Mobile bottom navigation: one column at a time on phones */}
      <nav className="md:hidden shrink-0 flex items-stretch bg-white border-t border-slate-200/80 px-2 py-1.5 gap-1">
        {MOBILE_TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setMobileView(t.key)}
            className={`flex-1 flex flex-col items-center justify-center gap-0.5 py-1.5 rounded-xl text-[11px] font-semibold transition-colors ${
              mobileView === t.key
                ? "bg-indigo-50 text-indigo-700"
                : "text-slate-500 hover:bg-slate-50"
            }`}
          >
            <MobileNavIcon which={t.key} />
            {t.label}
          </button>
        ))}
      </nav>

      {/* Custom Confirmation Modal */}
      {deleteSessionTarget && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-[2px] flex items-center justify-center z-50">
          <div className="bg-white border border-slate-200/80 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-xl">
            <h3 className="text-sm font-bold text-slate-800">
              Delete Session Archive?
            </h3>
            <p className="text-xs text-slate-500 mt-2 leading-relaxed">
              Are you sure you want to delete <span className="font-semibold text-slate-700">{deleteSessionTarget.name}</span>? This action will permanently remove all associated messages and data.
            </p>
            <div className="flex justify-end gap-2.5 mt-5 pt-3 border-t border-slate-100">
              <button
                onClick={() => setDeleteSessionTarget(null)}
                className="text-xs font-semibold text-slate-500 bg-slate-100 hover:bg-slate-200 px-3.5 py-2 rounded-xl transition-all"
              >
                Cancel
              </button>
              <button
                onClick={executeDeleteSession}
                className="text-xs font-semibold text-white bg-red-600 hover:bg-red-700 px-3.5 py-2 rounded-xl transition-all shadow-sm"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const CHANNEL_PHRASE: Record<string, string> = {
  email: "by email", sms: "by SMS", whatsapp: "on WhatsApp", phone: "by phone call",
};
const DELIVERY_TEXT: Record<string, string> = {
  security: "Customer wants deliveries left with building security.",
  signature: "Customer wants deliveries to require a signature on arrival.",
  door: "Customer is fine with deliveries left at the door.",
  pickup: "Customer prefers to collect orders from a pickup point.",
};
const VALUES_TEXT: Record<string, string> = {
  vegetarian: "Customer is vegetarian.",
  leather: "Customer avoids leather products.",
  eco: "Customer wants eco-friendly, plastic-free packaging.",
};

type OnboardFields = { channel: string; tone: string; delivery: string; values: string; note: string };
const EMPTY_FIELDS: OnboardFields = { channel: "", tone: "", delivery: "", values: "", note: "" };

function buildMemories(f: OnboardFields): InitialMemory[] {
  const out: InitialMemory[] = [];
  if (f.channel) out.push({ text: `Customer prefers to be contacted ${CHANNEL_PHRASE[f.channel]}.`, category: "preference" });
  if (f.tone === "short") out.push({ text: "Customer prefers short, to-the-point answers.", category: "preference" });
  if (f.tone === "detailed") out.push({ text: "Customer prefers detailed, thorough explanations.", category: "preference" });
  if (f.delivery) out.push({ text: DELIVERY_TEXT[f.delivery], category: "preference" });
  if (f.values) out.push({ text: VALUES_TEXT[f.values], category: "profile" });
  if (f.note.trim()) out.push({ text: f.note.trim(), category: "issue" });
  return out;
}

function Labeled(props: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{props.label}</span>
      <div className="mt-1">{props.children}</div>
    </label>
  );
}

function Picker(props: {
  onPick: (c: Customer) => void;
}) {
  const { onPick } = props;
  const [customers, setCustomers] = useState<Customer[] | null>(null);
  const [name, setName] = useState("");
  const [fields, setFields] = useState<OnboardFields>(EMPTY_FIELDS);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string } | null>(null);

  async function loadCustomers() {
    api.customers().then(setCustomers).catch(() => setError("can't reach the server"));
  }

  useEffect(() => {
    loadCustomers();
  }, []);

  function resetForm() {
    setName("");
    setFields(EMPTY_FIELDS);
    setShowForm(false);
  }

  async function create() {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      const memories = buildMemories(fields);
      const c = await api.createCustomer(name.trim(), memories);
      resetForm();
      await loadCustomers();
      onPick({ ...c, memory_count: memories.length });
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to create customer");
    } finally {
      setBusy(false);
    }
  }

  async function executeDelete() {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await api.deleteCustomer(deleteTarget.id);
      setDeleteTarget(null);
      await loadCustomers();
    } catch (e) {
      setError("Could not delete customer");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-dvh flex items-center justify-center bg-slate-50 px-4 py-10">
      <div className="w-full max-w-[30rem] bg-white border border-slate-200/80 rounded-3xl p-6 sm:p-8 shadow-md">
        <h1 className="font-bold text-xl tracking-tight text-slate-800">Support Workspace</h1>
        <p className="text-sm text-slate-500 mt-1 mb-6">
          Pick a customer to open their chat. Ledger keeps a separate memory per customer.
        </p>
        
        {error && (
          <div className="p-3 mb-4 text-xs font-semibold text-red-700 bg-red-50 border border-red-100 rounded-xl">
            {error}
          </div>
        )}
        
        {!customers && !error && (
          <div className="flex items-center justify-center py-10">
            <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-indigo-600"></div>
          </div>
        )}

        <div className="space-y-2.5 max-h-80 overflow-y-auto pr-1">
          {customers?.map((c) => (
            <div
              key={c.id}
              onClick={() => onPick(c)}
              className="w-full flex items-center justify-between border border-slate-100 bg-slate-50/30 hover:border-indigo-300 hover:bg-indigo-50/40 rounded-xl px-4 py-3.5 text-left transition-all duration-200 cursor-pointer group"
            >
              <div>
                <div className="text-sm font-semibold text-slate-800">{c.name}</div>
                <div className="text-xs text-slate-400 font-mono mt-0.5">{c.id}</div>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-[11px] font-semibold text-indigo-700 bg-indigo-50 border border-indigo-100 rounded-full px-3 py-1">
                  {c.memory_count} {Number(c.memory_count) === 1 ? "memory" : "memories"}
                </span>
                
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteTarget({ id: c.id, name: c.name });
                  }}
                  title={`Delete ${c.name}`}
                  className="text-slate-400 hover:text-red-500 p-1.5 rounded-lg hover:bg-slate-100 transition-all shrink-0"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
            </div>
          ))}
        </div>

        <button
          onClick={() => setShowForm(true)}
          className="w-full mt-6 pt-5 flex items-center justify-center gap-1.5 border-t border-slate-100 text-sm font-semibold text-indigo-600 hover:text-indigo-700"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          New customer
        </button>
      </div>

      {showForm && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-[2px] flex items-center justify-center z-50 p-4">
          <div className="bg-white border border-slate-200/80 rounded-2xl p-6 w-full max-w-md shadow-xl max-h-[90vh] overflow-y-auto">
            <h3 className="text-sm font-bold text-slate-800">New customer</h3>
            <p className="text-xs text-slate-500 mt-1 mb-4 leading-relaxed">
              Anything you set here becomes an initial memory. You'll see it in the memory
              panel right away. Everything except the name is optional.
            </p>
            <div className="space-y-3">
              <Labeled label="Name">
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  autoFocus
                  placeholder="e.g. Neha Kapoor"
                  className="field-input"
                />
              </Labeled>
              <Labeled label="Preferred contact channel">
                <select value={fields.channel} onChange={(e) => setFields({ ...fields, channel: e.target.value })} className="field-input">
                  <option value="">No preference</option>
                  <option value="email">Email</option>
                  <option value="sms">SMS</option>
                  <option value="whatsapp">WhatsApp</option>
                  <option value="phone">Phone call</option>
                </select>
              </Labeled>
              <Labeled label="Communication tone">
                <select value={fields.tone} onChange={(e) => setFields({ ...fields, tone: e.target.value })} className="field-input">
                  <option value="">No preference</option>
                  <option value="short">Short &amp; to-the-point</option>
                  <option value="detailed">Detailed explanations</option>
                </select>
              </Labeled>
              <Labeled label="Delivery preference">
                <select value={fields.delivery} onChange={(e) => setFields({ ...fields, delivery: e.target.value })} className="field-input">
                  <option value="">No preference</option>
                  <option value="security">Leave with building security</option>
                  <option value="signature">Require a signature</option>
                  <option value="door">Leave at the door</option>
                  <option value="pickup">Collect from a pickup point</option>
                </select>
              </Labeled>
              <Labeled label="Values">
                <select value={fields.values} onChange={(e) => setFields({ ...fields, values: e.target.value })} className="field-input">
                  <option value="">None</option>
                  <option value="vegetarian">Vegetarian</option>
                  <option value="leather">Avoids leather</option>
                  <option value="eco">Eco-friendly packaging</option>
                </select>
              </Labeled>
              <Labeled label="Current issue or note (optional)">
                <textarea
                  value={fields.note}
                  onChange={(e) => setFields({ ...fields, note: e.target.value })}
                  rows={2}
                  placeholder="e.g. Waiting on a refund for order ORD-1234"
                  className="field-input resize-none"
                />
              </Labeled>
            </div>
            <div className="flex justify-end gap-2.5 mt-5 pt-3 border-t border-slate-100">
              <button onClick={resetForm} className="text-xs font-semibold text-slate-500 bg-slate-100 hover:bg-slate-200 px-3.5 py-2 rounded-xl transition-all">
                Cancel
              </button>
              <button
                onClick={create}
                disabled={busy || !name.trim()}
                className="text-xs font-semibold text-white bg-indigo-600 hover:bg-indigo-700 disabled:opacity-50 px-3.5 py-2 rounded-xl transition-all shadow-sm"
              >
                {busy ? "Creating…" : "Create customer"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Custom Customer Confirmation Modal */}
      {deleteTarget && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-[2px] flex items-center justify-center z-50">
          <div className="bg-white border border-slate-200/80 rounded-2xl p-6 max-w-sm w-full mx-4 shadow-xl">
            <h3 className="text-sm font-bold text-slate-800">
              Delete Customer Profile?
            </h3>
            <p className="text-xs text-slate-500 mt-2 leading-relaxed">
              Are you sure you want to delete <span className="font-semibold text-slate-700">{deleteTarget.name}</span>? This action will permanently remove all associated messages and data.
            </p>
            <div className="flex justify-end gap-2.5 mt-5 pt-3 border-t border-slate-100">
              <button
                onClick={() => setDeleteTarget(null)}
                className="text-xs font-semibold text-slate-500 bg-slate-100 hover:bg-slate-200 px-3.5 py-2 rounded-xl transition-all"
              >
                Cancel
              </button>
              <button
                onClick={executeDelete}
                className="text-xs font-semibold text-white bg-red-600 hover:bg-red-700 px-3.5 py-2 rounded-xl transition-all shadow-sm"
              >
                Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Center(props: { children: React.ReactNode }) {
  return (
    <div className="h-dvh flex items-center justify-center bg-slate-50 text-slate-500 text-sm font-medium">
      {props.children}
    </div>
  );
}

const MOBILE_TABS = [
  { key: "chat", label: "Chat" },
  { key: "sessions", label: "Sessions" },
  { key: "memory", label: "Memory" },
] as const;

function MobileNavIcon({ which }: { which: "chat" | "sessions" | "memory" }) {
  const paths: Record<string, string> = {
    chat: "M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z",
    sessions: "M4 6h16M4 12h16M4 18h7",
    memory: "M13 10V3L4 14h7v7l9-11h-7z",
  };
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d={paths[which]} />
    </svg>
  );
}
