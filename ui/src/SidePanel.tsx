import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

const MIN = 220;
const MAX = 560;

function Chevron({ dir }: { dir: "left" | "right" }) {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.2}>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d={dir === "left" ? "M15 19l-7-7 7-7" : "M9 5l7 7-7 7"}
      />
    </svg>
  );
}

/** A collapsible, drag-to-resize side panel. Width and collapsed state persist. */
export function SidePanel(props: {
  side: "left" | "right";
  title: string;
  icon: ReactNode;
  storageKey: string;
  defaultWidth?: number;
  children: ReactNode;
}) {
  const { side, title, icon, storageKey, defaultWidth = 300, children } = props;
  const [width, setWidth] = useState(
    () => Number(localStorage.getItem(`${storageKey}:w`)) || defaultWidth,
  );
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(`${storageKey}:c`) === "1",
  );
  const drag = useRef<{ x: number; w: number } | null>(null);

  useEffect(() => localStorage.setItem(`${storageKey}:w`, String(width)), [width, storageKey]);
  useEffect(() => localStorage.setItem(`${storageKey}:c`, collapsed ? "1" : "0"), [collapsed, storageKey]);

  const onMove = useCallback(
    (e: MouseEvent) => {
      if (!drag.current) return;
      const delta = e.clientX - drag.current.x;
      const raw = side === "left" ? drag.current.w + delta : drag.current.w - delta;
      setWidth(Math.min(MAX, Math.max(MIN, raw)));
    },
    [side],
  );

  const stop = useCallback(() => {
    drag.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", stop);
  }, [onMove]);

  function startDrag(e: React.MouseEvent) {
    e.preventDefault();
    drag.current = { x: e.clientX, w: width };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", stop);
  }

  if (collapsed) {
    return (
      <div className="shrink-0 w-11 bg-white rounded-2xl border border-slate-200/60 shadow-sm flex flex-col items-center py-3 gap-4">
        <button
          onClick={() => setCollapsed(false)}
          title={`Open ${title}`}
          className="text-slate-400 hover:text-indigo-600 hover:bg-slate-100 rounded-lg p-1 transition-colors"
        >
          <Chevron dir={side === "left" ? "right" : "left"} />
        </button>
        <span className="text-slate-400">{icon}</span>
        <span className="text-xs font-semibold tracking-wide text-slate-500 [writing-mode:vertical-rl] rotate-180 select-none">
          {title}
        </span>
      </div>
    );
  }

  const handle = (
    <div
      onMouseDown={startDrag}
      className={`group absolute top-0 ${side === "left" ? "right-0" : "left-0"} h-full w-2 cursor-col-resize flex items-center justify-center`}
    >
      <div className="h-10 w-1 rounded-full bg-slate-200 group-hover:bg-indigo-400 transition-colors" />
    </div>
  );

  return (
    <aside
      style={{ width }}
      className="relative shrink-0 bg-white rounded-2xl border border-slate-200/60 shadow-sm overflow-hidden flex flex-col"
    >
      <div className="flex items-center justify-between px-4 h-12 border-b border-slate-100 shrink-0">
        <div className="flex items-center gap-2 text-slate-700">
          <span className="text-slate-400">{icon}</span>
          <span className="text-sm font-semibold tracking-tight">{title}</span>
        </div>
        <button
          onClick={() => setCollapsed(true)}
          title={`Collapse ${title}`}
          className="text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-lg p-1 transition-colors"
        >
          <Chevron dir={side === "left" ? "left" : "right"} />
        </button>
      </div>
      <div className="flex-1 min-h-0 flex flex-col">{children}</div>
      {handle}
    </aside>
  );
}
