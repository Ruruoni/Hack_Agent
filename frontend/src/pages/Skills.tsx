import { useState } from "react";
import { J, api, fmtTime, useApi } from "../api";
import { Badge, Btn, Card, Empty, ErrorBox, Field, Input, Json, Notice, PageHeader, StatusBadge, Table, Tabs, Textarea } from "../ui";

function Tools() {
  const tools = useApi<J[]>("/api/tools");
  const gaps = useApi<J>("/api/tools/gaps");
  const cmds = useApi<J>("/api/tools/commands");
  const [sel, setSel] = useState<J | null>(null);
  return (
    <div className="space-y-4">
      <Table onRow={setSel} cols={[{ key: "name", label: "Tool", render: (r) => <code>{r.name}</code> }, { key: "description", label: "What it does", render: (r) => <span className="text-dim">{r.description}</span> },
        { key: "effects", label: "Effects", render: (r) => r.effects.join(", ") }, { key: "risk", label: "Risk", render: (r) => <Badge tone={r.risk === "low" ? "good" : r.risk === "medium" ? "warn" : "bad"}>{r.risk}</Badge> },
        { key: "m", label: "", render: (r) => (r.missing_dependencies.length ? <Badge tone="bad" title={r.missing_dependencies.join(", ")}>missing dependency</Badge> : null) }]} rows={tools.data ?? []} />
      {sel && <Card title={sel.name} actions={<Btn small onClick={() => setSel(null)}>Close</Btn>}><p className="mb-2 text-sm">{sel.docs}</p><p className="mb-2 text-xs text-dim">Timeout {sel.timeout}s - output limit {sel.max_output_chars} chars - source {sel.source} - dependencies: {sel.dependencies.join(", ") || "none"}</p>
        <div className="grid gap-3 md:grid-cols-2"><div><h4 className="mb-1 text-sm font-medium">Input schema</h4><pre className="max-h-64 overflow-auto rounded bg-panel2 p-2">{JSON.stringify(sel.input_schema, null, 2)}</pre></div><div><h4 className="mb-1 text-sm font-medium">Output schema</h4><pre className="max-h-64 overflow-auto rounded bg-panel2 p-2">{JSON.stringify(sel.output_schema, null, 2)}</pre></div></div></Card>}
      <Card title="Registered commands (run_command)">
        <p className="mb-2 text-sm text-dim">The agent never gets a shell. It can only run these registered commands with validated parameters. Commands that touch the network require the target to be in your Authorized targets (Settings). Register more external utilities under Settings &gt; External tools.</p>
        <Table cols={[{ key: "k", label: "Command", render: (r) => <code>{r.id}</code> }, { key: "description", label: "Description" }, { key: "effects", label: "Effects", render: (r) => r.effects.join(", ") }, { key: "risk", label: "Risk" }, { key: "params", label: "Params", render: (r) => r.params.join(", ") }]} rows={Object.entries(cmds.data ?? {}).map(([k, v]: [string, J]) => ({ id: k, ...v }))} />
      </Card>
      <Card title="Capability gaps and extension proposals">
        {gaps.data?.missing_dependencies?.length ? <ul className="list-disc pl-5 text-sm">{gaps.data.missing_dependencies.map((m: J) => <li key={m.tool}><code>{m.tool}</code> needs: {m.missing.join(", ")}</li>)}</ul> : <p className="text-sm text-dim">All registered tools have their dependencies.</p>}
        <p className="mt-2 text-sm text-dim">When a task needs something missing, the agent drafts a proposal (see the Extensions tab). Nothing is installed or activated without you.</p>
      </Card>
    </div>
  );
}

function Workflows() {
  const sk = useApi<J[]>("/api/skills?kind=workflow", 4000);
  const [sel, setSel] = useState<J | null>(null);
  const ver = useApi<J[]>(sel ? `/api/skills/versions/${sel.name}` : null, 0, [sel?.name]);
  const [err, setErr] = useState<unknown>(null);
  const act = async (fn: () => Promise<any>) => { try { setErr(null); const r = await fn(); if (r?.id) setSel(r); sk.reload(); ver.reload(); } catch (e) { setErr(e); } };
  return (
    <div className="space-y-3">
      <Notice tone="accent">Workflows are reusable tool sequences the agent proposed after completing tasks (or you wrote). A candidate is replayed with read-only tools and the regression suite, and only becomes usable after YOU approve it. Every step still goes through permission checks.</Notice>
      <ErrorBox error={err} />
      <Table onRow={setSel} cols={[{ key: "name", label: "Workflow", render: (r) => <span><code>{r.name}</code> <span className="text-dim">v{r.version}</span></span> }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "description", label: "Description", render: (r) => <span className="text-dim">{r.description}</span> },
        { key: "s", label: "Success / fail", render: (r) => `${r.successes} / ${r.failures}` }, { key: "origin", label: "Origin", render: (r) => <span className="text-xs text-dim">{r.origin}</span> }, { key: "created", label: "Created", render: (r) => fmtTime(r.created) }]} rows={sk.data ?? []} empty="No workflows yet. Complete a multi-step task and a candidate will appear here." />
      {sel && (
        <Card title={<span><code>{sel.name}</code> v{sel.version} <StatusBadge s={sel.status} /></span>} actions={<>
          {["candidate", "failed_validation", "validated"].includes(sel.status) && <Btn small onClick={() => act(() => api.post(`/api/skills/${sel.id}/evaluate`))}>Evaluate</Btn>}
          {sel.status === "validated" && <Btn small tone="good" onClick={() => act(() => api.post(`/api/skills/${sel.id}/approve`))}>Approve</Btn>}
          {sel.status === "approved" && <Btn small tone="danger" onClick={() => act(async () => { await api.post(`/api/skills/${sel.id}/disable`); return {}; })}>Disable</Btn>}
          <Btn small onClick={() => setSel(null)}>Close</Btn></>}>
          <h4 className="mb-1 text-sm font-medium">Steps</h4>
          <ol className="list-decimal space-y-1 pl-5 text-sm">{sel.spec.steps.map((s: J) => <li key={s.id}><code>{s.tool}</code> <span className="text-dim">{JSON.stringify(s.args)}</span></li>)}</ol>
          <p className="mt-2 text-sm text-dim">Parameters: {Object.keys(sel.spec.params ?? {}).join(", ") || "none"}</p>
          {sel.tests?.static_errors?.length > 0 && <div className="mt-2"><ErrorBox error={{ message: sel.tests.static_errors.join("; ") }} /></div>}
          {sel.tests?.replays && <div className="mt-2 text-sm"><b>Test results:</b> static {sel.tests.static_errors?.length ? "failed" : "ok"}; replay {sel.tests.replays.every((r: J) => r.ok) ? "ok" : "failed"} ({sel.tests.replays.length} example(s)); regression suite {sel.tests.regression ? `${sel.tests.regression.passed} passed / ${sel.tests.regression.failed} failed` : "n/a"}. <span className="text-dim">{sel.tests.note}</span></div>}
          <h4 className="mb-1 mt-3 text-sm font-medium">Version history</h4>
          <Table cols={[{ key: "version", label: "Version" }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "origin", label: "Origin" }, { key: "updated", label: "Updated", render: (r) => fmtTime(r.updated) }]} rows={ver.data ?? []} />
        </Card>
      )}
    </div>
  );
}

function Extensions() {
  const ex = useApi<J[]>("/api/skills?kind=extension", 4000);
  const [cap, setCap] = useState("");
  const [why, setWhy] = useState("");
  const [deps, setDeps] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [out, setOut] = useState("");
  const act = async (fn: () => Promise<any>) => { try { setErr(null); const r = await fn(); if (r?.output) setOut(r.output); ex.reload(); } catch (e) { setErr(e); } };
  return (
    <div className="space-y-3">
      <Notice tone="accent">Extensions are Python plugins. Proposals are drafted into a quarantine folder and are inactive. Required order: isolated self-test, you install dependencies (button below or manually), then you activate. The agent can propose but never install, test-bypass or activate.</Notice>
      <ErrorBox error={err} />
      <Card title="Propose an extension manually"><div className="grid gap-3 md:grid-cols-3"><Field label="Capability needed"><Input value={cap} onChange={(e) => setCap(e.target.value)} placeholder="e.g. parse Wireshark JSON exports" /></Field><Field label="Why"><Input value={why} onChange={(e) => setWhy(e.target.value)} /></Field><Field label="Python packages (comma separated)"><Input value={deps} onChange={(e) => setDeps(e.target.value)} placeholder="scapy" /></Field></div>
        <div className="mt-2"><Btn tone="primary" disabled={!cap} onClick={() => act(async () => { await api.post("/api/extensions/propose", { capability: cap, rationale: why, dependencies: deps.split(",").map((x) => x.trim()).filter(Boolean) }); setCap(""); })}>Create proposal</Btn></div></Card>
      {out && <pre className="max-h-48 overflow-auto rounded-lg bg-panel2 p-2">{out}</pre>}
      <Table cols={[{ key: "name", label: "Extension", render: (r) => <code>{r.name}</code> }, { key: "description", label: "Purpose" }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "deps", label: "Dependencies", render: (r) => (r.spec.dependencies ?? []).join(", ") || "-" },
        { key: "t", label: "Test", render: (r) => (r.tests?.cases ? <span>{r.tests.cases.filter((c: J) => c.passed).length}/{r.tests.cases.length} passed{r.tests.note && <span className="block text-xs text-warn">{r.tests.note}</span>}{r.tests.missing_dependencies?.length > 0 && <span className="block text-xs text-warn">missing: {r.tests.missing_dependencies.join(", ")}</span>}</span> : "-") },
        { key: "a", label: "", render: (r) => <div className="flex flex-wrap gap-1">
          {["proposed", "test_failed", "tested"].includes(r.status) && <Btn small onClick={() => act(() => api.post(`/api/extensions/${r.id}/test`))}>Run self-test</Btn>}
          {r.spec.dependencies?.length > 0 && r.status !== "active" && <Btn small onClick={() => { if (confirm(`Install: ${r.spec.dependencies.join(", ")} into this Python environment?`)) act(() => api.post(`/api/extensions/${r.id}/install-deps`)); }}>Install deps</Btn>}
          {r.status === "tested" && <Btn small tone="good" onClick={() => act(() => api.post(`/api/extensions/${r.id}/activate`))}>Activate</Btn>}
          {r.status === "active" && <Btn small tone="danger" onClick={() => act(() => api.post(`/api/extensions/${r.id}/deactivate`))}>Deactivate</Btn>}</div> }]} rows={ex.data ?? []} empty="No extension proposals." />
    </div>
  );
}

export default function Skills() {
  const [tab, setTab] = useState("tools");
  return (
    <div className="space-y-4">
      <PageHeader title="Skills & extensions" sub="Registered tools, learned workflows, and proposed new capabilities." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "tools", label: "Tools" }, { id: "workflows", label: "Workflows (learned skills)" }, { id: "extensions", label: "Extensions" }]} />
      {tab === "tools" && <Tools />}{tab === "workflows" && <Workflows />}{tab === "extensions" && <Extensions />}
    </div>
  );
}
