import { useEffect, useRef, useState } from "react";
import { api, type ChatResponse, type Customer, type MemEvent } from "./api";

type Turn = {
  role: "user" | "assistant";
  content: string;
  events?: MemEvent[];
  redactions?: string[];
};

export function Chat(props: {
  customer: Customer;
  sessionId: string;
  onTurn: (r: ChatResponse) => void;
}) {
  const { customer, sessionId, onTurn } = props;
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setTurns([]);
    setError("");
    setLoading(true);
    api
      .sessionMessages(sessionId)
      .then((msgs) => {
        if (msgs.length > 0) {
          setTurns(msgs.map((m) => ({ role: m.role as "user" | "assistant", content: m.content })));
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [sessionId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, busy]);

  async function send() {
    const message = input.trim();
    if (!message || busy) return;
    setInput("");
    setError("");
    setTurns((t) => [...t, { role: "user", content: message }]);
    setBusy(true);
    try {
      const r = await api.chat({ customer_id: customer.id, session_id: sessionId, message });
      setTurns((t) => [
        ...t,
        { role: "assistant", content: r.reply, events: r.events, redactions: r.redactions },
      ]);
      onTurn(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex flex-col min-h-0 bg-slate-50/10">
      {/* Message Feed */}
      <div className="flex-1 overflow-y-auto px-3 md:px-6 py-4 md:py-6 space-y-6">
        {loading && (
          <div className="flex flex-col items-center justify-center py-24 space-y-3">
            <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-indigo-600"></div>
            <span className="text-[11px] text-slate-400 font-medium tracking-wide uppercase">Loading messages…</span>
          </div>
        )}
        {!loading && turns.length === 0 && (
          <div className="flex flex-col items-center justify-center text-center py-20 px-8 max-w-md mx-auto">
            <div className="bg-indigo-50/50 text-indigo-600 p-3 rounded-2xl mb-4 border border-indigo-100/30">
              <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
              </svg>
            </div>
            <h3 className="text-sm font-semibold text-slate-700">Start of session</h3>
            <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
              No messages yet. Type as {customer.name}. The assistant recalls what it
              already knows and saves any new facts automatically.
            </p>
          </div>
        )}

        {turns.map((t, i) => {
          const isUser = t.role === "user";
          const hasRedactions = !!t.redactions?.length;
          const hasEvents = !!t.events?.filter((e) => e.op !== "NOOP").length;

          return (
            <div key={i} className={`flex gap-3 ${isUser ? "justify-end" : "justify-start"}`}>
              {!isUser && (
                <div className="w-8 h-8 rounded-full bg-slate-900 flex items-center justify-center text-white text-[11px] font-bold shrink-0 shadow-sm">
                  AI
                </div>
              )}
              
              <div className="max-w-[85%] md:max-w-[70%] space-y-1.5">
                <div
                  className={`px-4 py-3 text-sm whitespace-pre-wrap leading-relaxed relative ${
                    isUser
                      ? "bg-slate-900 text-white rounded-2xl rounded-tr-sm shadow-sm"
                      : "bg-white border border-slate-100 text-slate-800 rounded-2xl rounded-tl-sm shadow-sm"
                  }`}
                >
                  {t.content}

                  {/* PII-redaction indicator */}
                  {isUser && hasRedactions && (
                    <span 
                      className="absolute -bottom-1.5 -left-1.5 bg-amber-500 text-white rounded-full p-0.5 shadow-sm"
                      title={`Secure Mode: PII scrubbed (${t.redactions?.join(", ")})`}
                    >
                      <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                      </svg>
                    </span>
                  )}
                </div>

                {/* memory-reconciled indicator */}
                {!isUser && hasEvents && (
                  <div className="flex items-center gap-1.5 px-1.5 text-[10px] text-indigo-600 font-semibold transition-all">
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-indigo-400 opacity-75"></span>
                      <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-indigo-500"></span>
                    </span>
                    <span>Memory reconciled</span>
                  </div>
                )}
              </div>

              {isUser && (
                <div className="w-8 h-8 rounded-full bg-indigo-50 border border-indigo-100 flex items-center justify-center text-indigo-700 text-[10px] font-bold shrink-0 shadow-sm uppercase tracking-wider">
                  {customer.name.slice(0, 2)}
                </div>
              )}
            </div>
          );
        })}

        {busy && (
          <div className="flex gap-3 justify-start">
            <div className="w-8 h-8 rounded-full bg-slate-900 flex items-center justify-center text-white text-[11px] font-bold shrink-0 animate-pulse">
              AI
            </div>
            <div className="bg-white border border-slate-100 rounded-2xl rounded-tl-sm px-4 py-3 shadow-sm">
              <div className="flex gap-1 items-center h-5">
                <span className="bounce-dot bounce-dot-1"></span>
                <span className="bounce-dot bounce-dot-2"></span>
                <span className="bounce-dot bounce-dot-3"></span>
              </div>
            </div>
          </div>
        )}

        {error && (
          <div className="flex gap-3 justify-start">
            <div className="bg-red-50 text-red-700 text-xs font-semibold px-4 py-2.5 rounded-xl border border-red-100/50 shadow-sm">
              Failed to deliver message: {error}
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Input bar */}
      <div className="border-t border-slate-200/80 bg-white px-3 md:px-6 py-3 md:py-4 flex gap-2 md:gap-3 shrink-0 items-center">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && send()}
            placeholder={`Message as ${customer.name}...`}
            disabled={busy}
            className="flex-1 border border-slate-200 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 rounded-xl px-4 py-3 text-sm outline-none bg-slate-50/50 focus:bg-white transition-all disabled:opacity-50"
          />
          <button
            onClick={send}
            disabled={busy || !input.trim()}
            className="bg-slate-900 hover:bg-slate-800 disabled:opacity-50 text-white font-semibold text-xs rounded-xl px-5 py-3 transition-colors shrink-0 flex items-center gap-1.5 shadow-sm"
          >
            <span>Send</span>
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3" />
            </svg>
          </button>
        </div>
    </div>
  );
}
