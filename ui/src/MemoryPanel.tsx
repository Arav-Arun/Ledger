import { useEffect, useState } from "react";
import {
  api,
  fmtDay,
  OP_STYLE,
  type HistoryEntry,
  type MemoryItem,
} from "./api";

const CATEGORY_ORDER = ["issue", "commitment", "preference", "profile", "episode"];

// category icons
const CATEGORY_ICONS: Record<string, React.ReactNode> = {
  issue: (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
    </svg>
  ),
  commitment: (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
    </svg>
  ),
  preference: (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z" />
    </svg>
  ),
  profile: (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
    </svg>
  ),
  episode: (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
};

export function MemoryPanel(props: { customerId: string; refresh: number; usedIds: Set<string> }) {
  const { customerId, refresh, usedIds } = props;
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [histories, setHistories] = useState<Record<string, HistoryEntry[]>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .memories(customerId)
      .then((m) => {
        setItems(m);
        setError("");
      })
      .catch(() => setError("couldn't load memories"));
  }, [customerId, refresh]);

  async function toggle(id: string) {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    if (!histories[id]) {
      const h = await api.history(id).catch(() => []);
      setHistories((prev) => ({ ...prev, [id]: h }));
    }
  }

  const [confirmForgetId, setConfirmForgetId] = useState<string | null>(null);

  async function forget(id: string) {
    await api.forget(id).catch(() => undefined);
    setItems((prev) => prev.filter((m) => m.id !== id));
    setConfirmForgetId(null);
  }

  const categories = CATEGORY_ORDER.filter((c) => items.some((m) => m.category === c));

  return (
    <div className="p-4 flex flex-col h-full">
      <div className="flex items-center justify-end pb-3 mb-2 border-b border-slate-100 shrink-0">
        <span className="text-xs font-medium px-2 py-0.5 bg-slate-100 text-slate-600 rounded-full">
          {items.length} fact{items.length === 1 ? "" : "s"}
        </span>
      </div>

      {error && (
        <div className="p-3 mb-4 text-xs text-red-700 bg-red-50 border border-red-100 rounded-xl">
          {error}
        </div>
      )}

      <div className="flex-1 overflow-y-auto pr-1 space-y-4">
        {!error && items.length === 0 && (
          <div className="flex flex-col items-center justify-center text-center h-48 px-4 border border-dashed border-slate-200 rounded-2xl bg-white/60">
            <svg className="w-8 h-8 text-slate-300 mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
            <p className="text-xs text-slate-500 font-medium">No facts stored yet</p>
            <p className="text-[11px] text-slate-400 mt-1 max-w-[200px] leading-normal">
              Talk to the support bot - durable facts and preferences will show up here.
            </p>
          </div>
        )}

        {categories.map((cat) => (
          <div key={cat} className="space-y-2">
            <div className="flex items-center gap-1.5 px-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
              {CATEGORY_ICONS[cat]}
              <span>{cat}s</span>
            </div>
            <div className="space-y-2">
              {items
                .filter((m) => m.category === cat)
                .map((m) => (
                  <div
                    key={m.id}
                    className={`group border rounded-xl p-3 text-[13px] leading-snug transition-all duration-200 hover:shadow-sm bg-white ${
                      usedIds.has(m.id)
                        ? "border-indigo-300 ring-2 ring-indigo-50"
                        : "border-slate-100 hover:border-slate-200"
                    }`}
                  >
                    <button onClick={() => toggle(m.id)} className="text-left w-full block">
                      <span className="text-slate-700 font-medium">{m.text}</span>
                    </button>
                    
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-2 pt-2 border-t border-slate-50 text-[10px] text-slate-400">
                      <span>{fmtDay(m.updated_at)}</span>
                      {m.expires_at && (
                        <>
                          <span className="text-slate-300">•</span>
                          <span className="text-amber-600 font-medium">Expires {fmtDay(m.expires_at)}</span>
                        </>
                      )}
                      
                      {confirmForgetId === m.id ? (
                        <div className="ml-auto flex items-center gap-1.5 shrink-0">
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              forget(m.id);
                            }}
                            className="text-[10px] font-bold text-red-600 bg-red-50 hover:bg-red-100 px-1.5 py-0.5 rounded border border-red-200"
                          >
                            Confirm
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmForgetId(null);
                            }}
                            className="text-[10px] font-medium text-slate-500 bg-slate-100 hover:bg-slate-200 px-1.5 py-0.5 rounded border border-slate-200"
                          >
                            Cancel
                          </button>
                        </div>
                      ) : (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            setConfirmForgetId(m.id);
                          }}
                          title="Forget this fact"
                          className="ml-auto text-slate-400 hover:text-red-500 transition-colors p-0.5 rounded"
                        >
                          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                          </svg>
                        </button>
                      )}
                    </div>

                    {open === m.id && (
                      <div className="mt-2.5 pt-2.5 border-t border-slate-100 space-y-2 bg-slate-50/50 rounded-lg p-2">
                        <div className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider mb-1">Audit Trail</div>
                        {(histories[m.id] ?? []).map((h, i) => (
                          <div key={i} className="text-[11px] leading-snug text-slate-600 border-l-2 border-slate-200 pl-2 py-0.5">
                            <div className="flex items-center gap-1.5 mb-0.5">
                              <span className={`px-1.5 py-0.5 text-[9px] font-bold rounded ${OP_STYLE[h.op] ?? OP_STYLE.NOOP}`}>
                                {h.op}
                              </span>
                              <span className="text-[9px] text-slate-400">{fmtDay(h.created_at)}</span>
                            </div>
                            <span className="italic">"{h.new_text ?? h.old_text}"</span>
                            {h.source && h.source !== "seed" && (
                              <div className="text-[9px] text-slate-400 mt-0.5">
                                Source: "{h.source.length > 50 ? h.source.slice(0, 50) + "..." : h.source}"
                              </div>
                            )}
                          </div>
                        ))}
                        {!histories[m.id]?.length && (
                          <div className="text-[11px] text-slate-400 animate-pulse pl-1">Loading audit logs...</div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
