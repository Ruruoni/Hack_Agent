import { Link } from "react-router-dom";
import { J, fmtBytes, fmtTime, useApi } from "../api";
import { Badge, Card, Empty, ErrorBox, PageHeader, Progress, Stat, StatusBadge, Table } from "../ui";
import { Approvals } from "../App";

export default function Dashboard() {
  const { data: d, error, reload } = useApi<J>("/api/dashboard", 4000);
  const wiz = useApi<J[]>("/api/setup/steps");
  const pending = useApi<J[]>("/api/approvals?status=pending", 3000);
  if (error && !d) return <ErrorBox error={error} onRetry={reload} />;
  if (!d) return <p className="text-dim">Loading...</p>;
  const hw = d.resources;
  const wizardDone = wiz.data ? wiz.data.filter((s) => s.last?.ok).length : 0;
  const caps = d.llm.capabilities;
  return (
    <div className="space-y-4">
      <PageHeader title="Dashboard" sub="Everything NEXUS is doing, at a glance." />
      {wiz.data && wizardDone < wiz.data.length && (
        <Card title="First-run setup"><p className="mb-2 text-sm">{wizardDone} of {wiz.data.length} setup checks passed.</p><Progress value={wizardDone / wiz.data.length} /><div className="mt-2"><Link className="text-accent underline" to="/setup">Continue the setup wizard</Link></div></Card>
      )}
      {!!pending.data?.length && <Approvals list={pending.data} onDone={pending.reload} />}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Agent" value={d.agent.status} tone={d.agent.status === "ready" ? "good" : "bad"} sub={`mode: ${d.agent.execution_mode.replace(/_/g, " ")}`} />
        <Stat label="LM Studio" value={d.llm.health.ok ? "connected" : "offline"} tone={d.llm.health.ok ? "good" : "bad"} sub={d.llm.health.ok ? d.llm.health.base_url : d.llm.health.hint} />
        <Stat label="Active model" value={d.llm.active_model ?? "-"} sub={caps?.loaded_context ? `${caps.loaded_context} ctx` : caps?.max_context ? `max ${caps.max_context} ctx` : ""} />
        <Stat label="Embeddings" value={d.embedding.semantic ? "semantic" : "lexical"} tone={d.embedding.semantic ? "good" : "warn"} sub={d.embedding.id} />
      </div>
      {caps?.limitations?.length > 0 && <Card title="Model capabilities & limitations"><ul className="list-disc space-y-1 pl-5 text-sm text-dim">{caps.limitations.map((l: string) => <li key={l}>{l}</li>)}</ul></Card>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Resources">
          <div className="grid grid-cols-2 gap-3">
            <Stat label="CPU" value={`${hw.cpu_count} cores`} sub={`${hw.cpu_percent}% busy`} />
            <Stat label="RAM free" value={fmtBytes(hw.ram_available_mb * 2 ** 20)} sub={`of ${fmtBytes(hw.ram_total_mb * 2 ** 20)}`} />
            <Stat label="Disk free" value={fmtBytes(hw.disk_free_mb * 2 ** 20)} sub={`of ${fmtBytes(hw.disk_total_mb * 2 ** 20)}`} />
            <Stat label="GPU" value={hw.gpus.length ? hw.gpus[0].name : "none detected"} sub={hw.gpus.length ? `${hw.gpus[0].vram_free_mb} MB VRAM free` : "fine-tuning limited"} />
          </div>
          <p className="mt-2 text-xs text-dim">{hw.os} - Python {hw.python} - background jobs: {hw.background_jobs}</p>
        </Card>
        <Card title="Active tasks" actions={<Link className="text-xs text-accent underline" to="/tasks">all tasks</Link>}>
          {d.tasks.active.length ? <Table cols={[{ key: "objective", label: "Objective" }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "progress", label: "", render: (r) => <div className="w-24"><Progress value={r.progress} /></div> }]} rows={d.tasks.active} /> : <Empty>No task is running.</Empty>}
          {d.ingestion.running.length > 0 && <p className="mt-2 text-sm">Ingestion running: {d.ingestion.running.map((j: J) => j.name).join(", ")}</p>}
        </Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Knowledge collections" actions={<Link className="text-xs text-accent underline" to="/knowledge">manage</Link>}>
          <Table cols={[{ key: "name", label: "Collection" }, { key: "documents", label: "Docs" }, { key: "chunks", label: "Chunks" }, { key: "quarantined", label: "Quarantined" }, { key: "needs_reindex", label: "", render: (r) => r.needs_reindex ? <Badge tone="warn">re-index</Badge> : null }]} rows={d.collections} empty="No collections yet - ingest some documents." />
        </Card>
        <Card title="Three learning systems (kept separate)" actions={<Link className="text-xs text-accent underline" to="/models">details</Link>}>
          <ul className="space-y-2 text-sm">
            <li><b>1. Data ingestion:</b> {d.learning.data_ingestion.chunks_indexed} chunks indexed in {d.learning.data_ingestion.collections} collections.</li>
            <li><b>2. Persistent learning:</b> {Object.values(d.learning.persistent_learning.memories as Record<string, number>).reduce((a, b) => a + b, 0)} memories, {d.learning.persistent_learning.workflows_approved} workflows, {d.learning.persistent_learning.regression_cases} regression cases.</li>
            <li><b>3. Model weights:</b> {d.learning.model_weights.weights_modified ? <Badge tone="warn">adapter active</Badge> : <Badge tone="good">unmodified</Badge>} <span className="text-dim">{d.learning.model_weights.statement}</span></li>
          </ul>
        </Card>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Recent learning activity">
          <Table cols={[{ key: "layer", label: "Layer" }, { key: "title", label: "Memory" }, { key: "trust", label: "Trust", render: (r) => <StatusBadge s={r.trust} /> }, { key: "created", label: "When", render: (r) => fmtTime(r.created) }]} rows={d.recent_learning} empty="Nothing learned yet." />
        </Card>
        <Card title="Recent errors & warnings">
          {d.recent_errors.length === 0 && d.ingestion.attention.length === 0 ? <Empty>All quiet.</Empty> : (
            <ul className="space-y-1.5 text-sm">
              {d.ingestion.attention.map((j: J) => <li key={j.id}><StatusBadge s={j.status} /> ingestion <b>{j.name}</b> {j.pause_reason || j.error}</li>)}
              {d.recent_errors.map((e: J, i: number) => <li key={i}><span className="text-dim">{fmtTime(e.ts)}</span> {e.message}</li>)}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
