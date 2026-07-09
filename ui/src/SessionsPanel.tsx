import { useState } from "react";

export type SessionTab = {
  id: string;
  label: string;
  createdAt: string;
  messageCount: number;
};

export function SessionsPanel(props: {
  sessions: SessionTab[];
  activeId: string | null;
  latestId: string | null;
  onSwitch: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string, label: string) => void;
  onRename: (id: string, title: string) => void;
}) {
  const { sessions, activeId, latestId, onSwitch, onNew, onDelete, onRename } = props;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  function startEdit(s: SessionTab) {
    setEditingId(s.id);
    setDraft(s.label);
  }

  function commit(id: string) {
    const next = draft.trim();
    const current = sessions.find((s) => s.id === id);
    if (next && current && next !== current.label) onRename(id, next);
    setEditingId(null);
  }

  return (
    <div className="flex flex-col h-full">
      <div className="p-3 shrink-0">
        <button
          onClick={onNew}
          className="w-full flex items-center justify-center gap-1.5 text-xs font-semibold px-3 py-2 bg-indigo-600 text-white hover:bg-indigo-700 rounded-xl transition-colors shadow-sm shadow-indigo-100"
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          New session
        </button>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-2 pb-2 space-y-1">
        {sessions.length === 0 && (
          <p className="text-xs text-slate-400 text-center px-4 py-8 leading-relaxed">
            No sessions yet. Start one to talk to the bot.
          </p>
        )}
        {sessions.map((s) => {
          const isActive = s.id === activeId;
          const isEditing = s.id === editingId;
          const when = new Date(s.createdAt);
          const fmt = `${when.toLocaleDateString("en-IN", { day: "numeric", month: "short" })}, ${when.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" })}`;
          const turns = Math.ceil(s.messageCount / 2);
          return (
            <div
              key={s.id}
              className={`group flex items-center gap-2 rounded-xl border pl-3 pr-1.5 py-2 transition-all ${
                isEditing ? "cursor-default" : "cursor-pointer"
              } ${
                isActive
                  ? "border-indigo-300 bg-indigo-50/60 ring-1 ring-indigo-100"
                  : "border-transparent hover:bg-slate-50"
              }`}
              onClick={() => {
                if (!isEditing) onSwitch(s.id);
              }}
            >
              <div className="flex-1 min-w-0">
                {isEditing ? (
                  <input
                    autoFocus
                    value={draft}
                    maxLength={60}
                    onChange={(e) => setDraft(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") commit(s.id);
                      else if (e.key === "Escape") setEditingId(null);
                    }}
                    onBlur={() => commit(s.id)}
                    className="w-full text-xs font-semibold text-slate-700 bg-white border border-indigo-300 rounded-md px-1.5 py-0.5 outline-none focus:ring-1 focus:ring-indigo-400"
                  />
                ) : (
                  <>
                    <div className="flex items-center gap-1.5">
                      <span className={`text-xs font-semibold truncate ${isActive ? "text-indigo-700" : "text-slate-600"}`}>
                        {s.label}
                      </span>
                      {s.id === latestId && (
                        <span className="text-[9px] font-bold text-emerald-700 bg-emerald-50 border border-emerald-100 px-1.5 py-px rounded-full">
                          Live
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5 text-[10px] text-slate-400 mt-0.5">
                      <span>{fmt}</span>
                      {turns > 0 && (
                        <>
                          <span className="text-slate-300">•</span>
                          <span>{turns} msg{turns !== 1 ? "s" : ""}</span>
                        </>
                      )}
                    </div>
                  </>
                )}
              </div>
              {!isEditing && (
                <div className="flex items-center shrink-0">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      startEdit(s);
                    }}
                    title="Rename session"
                    className="text-slate-300 hover:text-indigo-500 opacity-0 group-hover:opacity-100 transition-all p-1 rounded hover:bg-slate-200/50"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(s.id, s.label);
                    }}
                    title="Delete session"
                    className="text-slate-300 hover:text-red-500 opacity-0 group-hover:opacity-100 transition-all p-1 rounded hover:bg-slate-200/50"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
