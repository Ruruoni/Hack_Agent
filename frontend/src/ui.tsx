import { ReactNode, useState } from "react";
import { ApiError, J } from "./api";

type Tone = "neutral" | "good" | "warn" | "bad" | "accent";
const toneCls: Record<Tone, string> = {
  neutral: "bg-panel2 text-dim border-line",
  good: "bg-good/15 text-good border-good/40",
  warn: "bg-warn/15 text-warn border-warn/40",
  bad: "bg-bad/15 text-bad border-bad/40",
  accent: "bg-accent/15 text-accent border-accent/40",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap ${toneCls[tone]}`}>
      {children}
    </span>
  );
}

const STATUS_TONE: Record<string, Tone> = {
  completed: "good", done: "good", ready: "good", ok: "good", verified: "good", approved: "good", active: "good", accepted: "good", pass: "good", user_verified: "good", indexed: "good", validated: "good",
  running: "accent", planning: "accent", pending: "neutral", queued: "neutral", downloading: "accent", attempted: "accent", intended: "neutral", verifying: "accent",
  awaiting_approval: "warn", awaiting_input: "warn", awaiting_decision: "warn", paused: "warn", interrupted: "warn", quarantined: "warn", duplicate: "neutral", candidate: "warn",
  unverified: "warn", hypothesis: "warn", skipped: "neutral", blocked: "bad", outdated: "neutral", rolled_back: "neutral", planned: "neutral", proposed: "warn", tested: "good",
  failed: "bad", rejected: "bad", denied: "bad", cancelled: "neutral", approval_denied: "bad", fail: "bad", invalid: "bad", test_failed: "bad", failed_validation: "bad", completed_with_errors: "warn",
};
export const StatusBadge = ({ s }: { s?: string | null }) => (s ? <Badge tone={STATUS_TONE[s] ?? "neutral"}>{s.replace(/_/g, " ")}</Badge> : null);

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-line bg-panel ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h3 className="text-sm font-semibold">{title}</h3>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Btn({ tone = "neutral", small, disabled, onClick, children, title, type = "button" }: {
  tone?: "neutral" | "primary" | "danger" | "good"; small?: boolean; disabled?: boolean; onClick?: () => void; children: ReactNode; title?: string; type?: "button" | "submit";
}) {
  const t = { neutral: "bg-panel2 hover:bg-line text-fg border-line", primary: "bg-accent text-white border-accent hover:opacity-90", danger: "bg-bad text-white border-bad hover:opacity-90", good: "bg-good text-white border-good hover:opacity-90" }[tone];
  return (
    <button type={type} title={title} disabled={disabled} onClick={onClick}
      className={`rounded-lg border ${small ? "px-2 py-1 text-xs" : "px-3 py-1.5 text-sm"} font-medium transition disabled:opacity-40 disabled:cursor-not-allowed ${t}`}>
      {children}
    </button>
  );
}

const inp = "w-full rounded-lg border border-line bg-bg px-2.5 py-1.5 text-sm outline-none focus:border-accent";
export const Input = (p: React.InputHTMLAttributes<HTMLInputElement>) => <input {...p} className={`${inp} ${p.className ?? ""}`} />;
export const Textarea = (p: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...p} className={`${inp} ${p.className ?? ""}`} />;
export const Select = (p: React.SelectHTMLAttributes<HTMLSelectElement>) => <select {...p} className={`${inp} ${p.className ?? ""}`} />;

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-dim">{label}</span>
      {children}
      {hint && <span className="block text-xs text-dim">{hint}</span>}
    </label>
  );
}

export function Check({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode }) {
  return (
    <label className="flex items-center gap-2 text-sm">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="h-4 w-4 accent-[var(--color-accent)]" />
      {label}
    </label>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div role="alert" className="rounded-lg border border-bad/50 bg-bad/10 p-3 text-sm">
      <div className="font-medium text-bad">{e.message ?? String(error)}</div>
      {e.hint && <div className="mt-1 text-dim">{e.hint}</div>}
      {onRetry && <div className="mt-2"><Btn small onClick={onRetry}>Retry</Btn></div>}
    </div>
  );
}

export const Empty = ({ children }: { children: ReactNode }) => <div className="rounded-lg border border-dashed border-line p-6 text-center text-dim">{children}</div>;
export const Spinner = () => <span className="live text-dim">Loading...</span>;

export function Progress({ value, tone = "accent" }: { value: number; tone?: "accent" | "good" | "warn" | "bad" }) {
  const v = Math.max(0, Math.min(1, value));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-panel2" role="progressbar" aria-valuenow={Math.round(v * 100)}>
      <div className={`h-full bg-${tone} transition-all`} style={{ width: `${v * 100}%`, background: `var(--color-${tone})` }} />
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="rounded-lg border border-line bg-panel2/50 p-3">
      <div className="text-xs text-dim">{label}</div>
      <div className={`text-xl font-semibold ${tone === "bad" ? "text-bad" : tone === "good" ? "text-good" : tone === "warn" ? "text-warn" : ""}`}>{value}</div>
      {sub && <div className="text-xs text-dim">{sub}</div>}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: { id: string; label: ReactNode }[]; value: string; onChange: (id: string) => void }) {
  return (
    <div role="tablist" className="flex flex-wrap gap-1 border-b border-line">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}
          className={`-mb-px rounded-t-lg border border-b-0 px-3 py-1.5 text-sm ${value === t.id ? "border-line bg-panel font-semibold" : "border-transparent text-dim hover:text-fg"}`}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Table({ cols, rows, empty = "Nothing here yet.", onRow }: {
  cols: { key: string; label: string; render?: (r: J) => ReactNode; cls?: string }[]; rows: J[]; empty?: ReactNode; onRow?: (r: J) => void;
}) {
  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-left text-sm">
        <thead className="bg-panel2 text-xs text-dim">
          <tr>{cols.map((c) => <th key={c.key} className="px-3 py-2 font-medium">{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} onClick={onRow ? () => onRow(r) : undefined} className={`border-t border-line ${onRow ? "cursor-pointer hover:bg-panel2/60" : ""}`}>
              {cols.map((c) => <td key={c.key} className={`px-3 py-2 align-top ${c.cls ?? ""}`}>{c.render ? c.render(r) : String(r[c.key] ?? "")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Json({ value, label = "details" }: { value: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button className="text-xs text-accent underline" onClick={() => setOpen(!open)}>{open ? "hide" : "show"} {label}</button>
      {open && <pre className="mt-1 max-h-72 overflow-auto rounded-lg bg-panel2 p-2">{JSON.stringify(value, null, 2)}</pre>}
    </div>
  );
}

/** Minimal, safe markdown renderer (no HTML injection): code fences, inline code, bold, bullet lists, paragraphs. */
export function Markdown({ text, onCite }: { text: string; onCite?: (n: number) => void }) {
  const blocks = text.split(/```/);
  return (
    <div className="md leading-relaxed">
      {blocks.map((b, i) => {
        if (i % 2 === 1) return <pre key={i}>{b.replace(/^\w*\n/, "")}</pre>;
        return b.split(/\n{2,}/).map((para, j) => {
          const lines = para.split("\n");
          if (lines.every((l) => /^\s*[-*] /.test(l) || !l.trim()) && lines.some((l) => l.trim())) {
            return <ul key={`${i}-${j}`}>{lines.filter((l) => l.trim()).map((l, k) => <li key={k}>{inline(l.replace(/^\s*[-*] /, ""), onCite)}</li>)}</ul>;
          }
          return para.trim() ? <p key={`${i}-${j}`}>{lines.map((l, k) => <span key={k}>{inline(l, onCite)}{k < lines.length - 1 && <br />}</span>)}</p> : null;
        });
      })}
    </div>
  );
}

function inline(s: string, onCite?: (n: number) => void): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*|`[^`]+`|\[S\d+\])/g).map((p, i) => {
    if (/^\*\*[^*]+\*\*$/.test(p)) return <strong key={i}>{p.slice(2, -2)}</strong>;
    if (/^`[^`]+`$/.test(p)) return <code key={i} className="inline">{p.slice(1, -1)}</code>;
    const m = p.match(/^\[S(\d+)\]$/);
    if (m) return <button key={i} onClick={() => onCite?.(Number(m[1]))} className="mx-0.5 rounded bg-accent/20 px-1 text-xs font-semibold text-accent">{p}</button>;
    return p;
  });
}

export function PageHeader({ title, sub, actions }: { title: string; sub?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold">{title}</h1>
        {sub && <p className="text-sm text-dim">{sub}</p>}
      </div>
      <div className="flex gap-2">{actions}</div>
    </div>
  );
}

export function Notice({ tone = "warn", children }: { tone?: Tone; children: ReactNode }) {
  return <div className={`rounded-lg border px-3 py-2 text-sm ${toneCls[tone]}`}>{children}</div>;
}
