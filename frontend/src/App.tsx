import { useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api, J, useApi, useEvents } from "./api";
import { Badge, Btn, Card, ErrorBox } from "./ui";
import Dashboard from "./pages/Dashboard";
import Chat from "./pages/Chat";
import Ingestion from "./pages/Ingestion";
import Knowledge from "./pages/Knowledge";
import Models from "./pages/Models";
import Tasks from "./pages/Tasks";
import Research from "./pages/Research";
import Skills from "./pages/Skills";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";

const NAV = [
  ["/", "Dashboard"], ["/chat", "Chat"], ["/ingestion", "Data ingestion"], ["/knowledge", "Knowledge & memory"], ["/models", "Models & learning"],
  ["/tasks", "Agent tasks"], ["/research", "Web research"], ["/skills", "Skills & extensions"], ["/settings", "Settings"], ["/setup", "Setup wizard"],
];

export function Approvals({ list, onDone }: { list: J[]; onDone: () => void }) {
  const [err, setErr] = useState<unknown>(null);
  const decide = async (id: string, approve: boolean) => {
    try {
      await api.post(`/api/approvals/${id}/decide`, { approve });
      onDone();
    } catch (e) {
      setErr(e);
    }
  };
  if (!list.length) return null;
  return (
    <div className="space-y-2">
      <ErrorBox error={err} />
      {list.map((a) => (
        <Card key={a.id} title={<span>Approval needed: <code>{a.tool}</code> <Badge tone={a.risk === "critical" || a.risk === "high" ? "bad" : "warn"}>{a.risk} risk</Badge></span>}>
          <p className="mb-2 text-sm text-dim">{a.reason}</p>
          <pre className="mb-3 max-h-40 overflow-auto rounded-lg bg-panel2 p-2">{JSON.stringify(a.args?.args ?? a.args, null, 2)}</pre>
          <div className="flex gap-2">
            <Btn tone="good" onClick={() => decide(a.id, true)}>Approve once</Btn>
            <Btn tone="danger" onClick={() => decide(a.id, false)}>Deny</Btn>
          </div>
        </Card>
      ))}
    </div>
  );
}

export default function App() {
  const health = useApi<J>("/api/health", 5000);
  const llm = useApi<J>("/api/llm/status", 8000);
  const pending = useApi<J[]>("/api/approvals?status=pending", 2500);
  const [drawer, setDrawer] = useState(false);
  useEvents((e) => { if (e.kind === "approval.requested") { pending.reload(); setDrawer(true); } });

  const stopped = !!health.data?.stopped;
  const llmOk = llm.data?.health?.ok;
  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-3 border-b border-line bg-panel px-4 py-2">
        <div className="text-base font-bold tracking-wide">NEXUS<span className="text-accent"> AI</span></div>
        <Badge tone={llmOk ? "good" : "bad"} title={llm.data?.health?.message ?? llm.data?.base_url}>{llmOk ? "LM Studio connected" : "LM Studio offline"}</Badge>
        <Badge tone="accent" title="Active chat model">{llm.data?.capabilities?.model ?? llm.data?.configured_model ?? "no model"}</Badge>
        {llm.data?.capabilities && <Badge tone={llm.data.capabilities.native_tools ? "good" : "warn"} title={(llm.data.capabilities.limitations ?? []).join("\n")}>{llm.data.capabilities.native_tools ? "native tools" : "JSON-action fallback"}</Badge>}
        <Badge tone={health.data?.execution_mode === "read_only" ? "good" : health.data?.execution_mode === "approval_required" ? "accent" : "warn"} title="Execution mode (Settings)">{(health.data?.execution_mode ?? "?").replace(/_/g, " ")}</Badge>
        <div className="ml-auto flex items-center gap-2">
          <button onClick={() => setDrawer(!drawer)} className="relative rounded-lg border border-line px-3 py-1 text-sm">
            Approvals {!!pending.data?.length && <span className="ml-1 rounded-full bg-warn px-1.5 text-xs font-bold text-black">{pending.data.length}</span>}
          </button>
          {stopped ? <Btn tone="good" onClick={async () => { await api.post("/api/resume"); health.reload(); }}>Resume agent</Btn>
            : <Btn tone="danger" title="Cancels every running task and ingestion job immediately" onClick={async () => { await api.post("/api/stop"); health.reload(); }}>STOP ALL</Btn>}
        </div>
      </header>
      {health.error && <div className="p-3"><ErrorBox error={health.error} onRetry={health.reload} /></div>}
      {stopped && <div className="bg-bad/20 px-4 py-1.5 text-center text-sm text-bad">Global stop is active: tasks and jobs are cancelled and new ones will not run. Press "Resume agent" to continue.</div>}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <nav className="flex shrink-0 gap-1 overflow-x-auto border-b border-line bg-panel p-2 md:block md:w-52 md:space-y-0.5 md:overflow-y-auto md:border-b-0 md:border-r" aria-label="Main">
          {NAV.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `block whitespace-nowrap rounded-lg px-3 py-1.5 text-sm ${isActive ? "bg-accent/15 font-semibold text-accent" : "text-dim hover:bg-panel2 hover:text-fg"}`}>{label}</NavLink>
          ))}
          <p className="hidden px-3 pt-4 text-xs leading-snug text-dim md:block">Indexing documents, remembering things and fine-tuning model weights are three different things. See "Models & learning".</p>
        </nav>
        <main className="min-w-0 flex-1 overflow-y-auto p-3 md:p-5">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/chat" element={<Chat />} />
            <Route path="/ingestion" element={<Ingestion />} />
            <Route path="/knowledge" element={<Knowledge />} />
            <Route path="/models" element={<Models />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/research" element={<Research />} />
            <Route path="/skills" element={<Skills />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/setup" element={<Setup />} />
          </Routes>
        </main>
        {drawer && (
          <aside className="max-h-[50vh] shrink-0 space-y-3 overflow-y-auto border-t border-line bg-bg p-3 md:max-h-none md:w-96 md:border-l md:border-t-0" aria-label="Approvals">
            <div className="flex items-center justify-between"><h2 className="font-semibold">Pending approvals</h2><Btn small onClick={() => setDrawer(false)}>Close</Btn></div>
            {pending.data?.length ? <Approvals list={pending.data} onDone={pending.reload} /> : <p className="text-sm text-dim">Nothing is waiting for you. The agent can never approve its own actions.</p>}
          </aside>
        )}
      </div>
    </div>
  );
}
