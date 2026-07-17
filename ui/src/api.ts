export type Customer = { id: string; name: string; memory_count: number };

export type InitialMemory = { text: string; category: string };

export type MemoryItem = {
  id: string;
  text: string;
  category: string;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
};

export type MemEvent = { op: string; memory_id?: string; text: string; old_text?: string };

export type HistoryEntry = {
  op: string;
  old_text: string | null;
  new_text: string | null;
  source: string;
  created_at: string;
};

export type Recalled = { id: string; text: string; category: string; score?: number };

// One rubric criterion's verdict, and one attempt in the draft->grade->revise loop.
// `verified` is whether the grader actually evaluated this criterion (vs. failing closed).
export type GroundingCheck = { name: string; passed: boolean; reason: string; verified?: boolean };
export type GroundingAttempt = {
  attempt: number;
  reply: string;
  passed: boolean;
  checks: GroundingCheck[];
};

export type ChatResponse = {
  reply: string;
  memories_used: Recalled[];
  events: MemEvent[];
  redactions: string[];
  grounding: GroundingAttempt[];
  grounded: boolean;
};

export type Session = {
  id: string;
  title: string | null;
  created_at: string;
  message_count: number;
};

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json" },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}) as { detail?: string });
    throw new Error(body.detail ?? `request failed (${res.status})`);
  }
  return res.json();
}

export const api = {
  customers: () => req<Customer[]>("/api/customers"),
  createCustomer: (name: string, memories: InitialMemory[] = []) =>
    req<Customer>("/api/customers", { method: "POST", body: JSON.stringify({ name, memories }) }),
  newSession: (customer_id: string) =>
    req<{ session_id: string }>("/api/sessions", { method: "POST", body: JSON.stringify({ customer_id }) }),
  listSessions: (customer_id: string) =>
    req<Session[]>(`/api/customers/${customer_id}/sessions`),
  sessionMessages: (session_id: string) =>
    req<{ role: string; content: string }[]>(`/api/sessions/${session_id}/messages`),
  chat: (body: { customer_id: string; session_id: string; message: string }) =>
    req<ChatResponse>("/api/chat", { method: "POST", body: JSON.stringify(body) }),
  memories: (customerId: string) => req<MemoryItem[]>(`/api/memories/${customerId}`),
  history: (memoryId: string) => req<HistoryEntry[]>(`/api/memory/${memoryId}/history`),
  forget: (memoryId: string) => req<{ ok: boolean }>(`/api/memory/${memoryId}`, { method: "DELETE" }),
  deleteCustomer: (customer_id: string) => req<{ ok: boolean }>(`/api/customers/${customer_id}`, { method: "DELETE" }),
  deleteSession: (session_id: string) => req<{ ok: boolean }>(`/api/sessions/${session_id}`, { method: "DELETE" }),
  renameSession: (session_id: string, title: string) =>
    req<{ ok: boolean; title: string }>(`/api/sessions/${session_id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
};

export const OP_STYLE: Record<string, string> = {
  ADD: "bg-emerald-100 text-emerald-800",
  UPDATE: "bg-amber-100 text-amber-800",
  DELETE: "bg-red-100 text-red-700",
  EXPIRE: "bg-slate-200 text-slate-600",
  EVICT: "bg-slate-200 text-slate-600",
  NOOP: "bg-slate-100 text-slate-500",
};

export const fmtDay = (iso: string) =>
  new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
