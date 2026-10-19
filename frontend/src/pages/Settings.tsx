import { useState } from "react";
import { J, api, fmtTime, useApi } from "../api";
import { Badge, Btn, Card, Check, ErrorBox, Field, Input, Notice, PageHeader, Select, Table, Tabs, Textarea } from "../ui";

type Def = { key: string; label: string; hint?: string; type?: "bool" | "num" | "text" | "list" | "json" | "secret" | { select: string[] } };
const SECTIONS: { id: string; title: string; intro?: string; fields: Def[] }[] = [
  { id: "perm", title: "Permissions", intro: "These controls live outside the language model. The agent has no tool that can read or change them.", fields: [
    { key: "execution_mode", label: "Execution mode", type: { select: ["read_only", "approval_required", "restricted_automation"] }, hint: "read_only: inspect only. approval_required: consequential actions wait for you. restricted_automation: only the allow-listed low-risk tools run automatically." },
    { key: "auto_allow_tools", label: "Auto-allowed tools (restricted automation)", type: "list", hint: "One per line. Deletes, installs, training and network writes can never be automatic." },
    { key: "allowed_roots", label: "Extra folders the agent may read/write", type: "list", hint: "One absolute path per line. The workspace folder is always allowed." },
    { key: "authorized_targets", label: "Authorized network targets", type: "list", hint: "Hosts, wildcards or CIDRs (e.g. 192.168.50.0/24, lab.example) you are permitted to test. Network diagnostics against anything else are refused." },
    { key: "read_only_allows_network", label: "Read-only mode may read the web", type: "bool" }, { key: "approval_timeout_s", label: "Approval timeout (s)", type: "num" } ] },
  { id: "limits", title: "Agent limits", fields: [
    { key: "max_task_steps", label: "Max steps per task", type: "num" }, { key: "max_tool_calls", label: "Max tool calls per task", type: "num" }, { key: "max_task_seconds", label: "Max task duration (s)", type: "num" },
    { key: "max_step_retries", label: "Retries per failing step", type: "num" }, { key: "max_concurrent_tasks", label: "Concurrent tasks (restart to apply)", type: "num" }, { key: "tool_timeout_s", label: "Default tool timeout (s)", type: "num" },
    { key: "sandbox_memory_mb", label: "Python sandbox memory (MB)", type: "num" }, { key: "sandbox_cpu_s", label: "Python sandbox CPU time (s)", type: "num" } ] },
  { id: "model", title: "Model endpoint", fields: [
    { key: "lmstudio_base_url", label: "LM Studio base URL", hint: "Default http://localhost:1234/v1" }, { key: "lmstudio_api_key", label: "API key (only if LM Studio requires one)", type: "secret" },
    { key: "request_timeout", label: "Request timeout (s)", type: "num" }, { key: "chat_model", label: "Chat model id (blank = automatic)" }, { key: "llamacpp_dir", label: "llama.cpp folder (for GGUF export)" } ] },
  { id: "chat", title: "Conversation", fields: [
    { key: "personality", label: "Personality" }, { key: "response_length", label: "Response length", type: { select: ["short", "medium", "long"] } }, { key: "explain_mode", label: "Explanation level", type: { select: ["technical", "beginner"] } },
    { key: "history_messages", label: "Recent messages sent to the model", type: "num" }, { key: "summarize_after", label: "Summarize after N messages", type: "num" }, { key: "retrieval_top_k", label: "Sources retrieved per question", type: "num" } ] },
  { id: "ingest", title: "Ingestion & storage", fields: [
    { key: "max_file_mb", label: "Max file size (MB)", type: "num" }, { key: "chunk_chars", label: "Chunk size (chars)", type: "num" }, { key: "chunk_overlap", label: "Chunk overlap", type: "num" }, { key: "ingest_batch_size", label: "Chunks per checkpoint", type: "num" },
    { key: "ingest_workers", label: "Parallel workers", type: "num" }, { key: "max_reject_rate", label: "Quality gate: pause above this reject share", type: "num" }, { key: "min_free_disk_mb", label: "Keep this much disk free (MB)", type: "num" },
    { key: "quarantine_injections", label: "Quarantine text that looks like AI instructions", type: "bool" }, { key: "keep_raw", label: "Keep a copy of raw inputs", type: "bool" }, { key: "vector_backend", label: "Vector store (restart to apply)", type: { select: ["auto", "chroma", "sqlite"] } },
    { key: "max_auto_download_mb", label: "Dataset downloads above this need explicit approval (MB)", type: "num" } ] },
  { id: "web", title: "Web research & search", fields: [
    { key: "web_allowlist", label: "Domain allow-list (empty = any public host)", type: "list" }, { key: "web_denylist", label: "Domain deny-list", type: "list" }, { key: "web_respect_robots", label: "Respect robots.txt", type: "bool" },
    { key: "web_rate_limit_s", label: "Seconds between requests to one host", type: "num" }, { key: "web_max_pages", label: "Default max pages", type: "num" }, { key: "web_allow_private", label: "Allow private/loopback hosts (lab use only)", type: "bool", hint: "Leave OFF unless you are researching a local test site." },
    { key: "search_provider", label: "Search provider", type: { select: ["none", "searxng", "brave"] } }, { key: "search_url", label: "SearxNG URL" }, { key: "search_api_key", label: "Search API key", type: "secret" } ] },
  { id: "integ", title: "Integrations & external tools", intro: "JSON lists. Integrations: [{\"name\":\"tickets\",\"base_url\":\"https://api.example.com\",\"auth_env\":\"TICKETS_TOKEN\",\"allow_write\":false}]. External tools: [{\"name\":\"nmap_scan\",\"executable\":\"nmap\",\"args\":[\"-sT\",\"{target}\"],\"params\":{\"target\":\"[A-Za-z0-9.\\\\-/]+\"},\"target_param\":\"target\",\"effects\":[\"network_write\"],\"risk\":\"high\"}]. API secrets are read from environment variables, never stored here.", fields: [
    { key: "integrations", label: "API integrations", type: "json" }, { key: "external_tools", label: "Registered external tools", type: "json" } ] },
  { id: "privacy", title: "Privacy", fields: [{ key: "telemetry", label: "Telemetry (this build sends none)", type: "bool" }, { key: "allow_cors_origins", label: "Browser origins allowed to call the API", type: "list" }, { key: "allowed_hosts", label: "Allowed host names (DNS-rebinding guard)", type: "list", hint: "Names you may type in the browser address bar. Default: localhost, 127.0.0.1, [::1]." }] },
];

function Editor({ def, value, onChange }: { def: Def; value: any; onChange: (v: any) => void }) {
  const t = def.type ?? "text";
  if (t === "bool") return <Check checked={!!value} onChange={onChange} label={def.label} />;
  return (
    <Field label={def.label} hint={def.hint}>
      {t === "num" ? <Input type="number" step="any" value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? 0 : +e.target.value)} />
        : t === "list" ? <Textarea rows={3} value={(value ?? []).join("\n")} onChange={(e) => onChange(e.target.value.split("\n").map((x) => x.trim()).filter(Boolean))} />
        : t === "json" ? <JsonBox value={value} onChange={onChange} />
        : t === "secret" ? <Input type="password" value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder={value === "***set***" ? "(set - type to replace)" : ""} autoComplete="off" />
        : typeof t === "object" ? <Select value={value} onChange={(e) => onChange(e.target.value)}>{t.select.map((o) => <option key={o}>{o}</option>)}</Select>
        : <Input value={value ?? ""} onChange={(e) => onChange(e.target.value)} />}
    </Field>
  );
}

function JsonBox({ value, onChange }: { value: any; onChange: (v: any) => void }) {
  const [text, setText] = useState(JSON.stringify(value ?? [], null, 2));
  const [bad, setBad] = useState("");
  return <><Textarea rows={5} value={text} className="font-mono" onChange={(e) => { setText(e.target.value); try { onChange(JSON.parse(e.target.value)); setBad(""); } catch (x) { setBad(String(x)); } }} />{bad && <span className="text-xs text-bad">{bad}</span>}</>;
}

function System() {
  const audit = useApi<J[]>("/api/audit?limit=60", 0);
  const ver = useApi<J>("/api/audit/verify");
  const approvals = useApi<J[]>("/api/approvals?limit=40");
  const backups = useApi<J[]>("/api/backups");
  const trash = useApi<J[]>("/api/trash");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<unknown>(null);
  return (
    <div className="space-y-4">
      <ErrorBox error={err} />
      <Card title="Backup & recovery" actions={<Btn small tone="primary" onClick={async () => { try { const r = await api.post("/api/backup"); setMsg(`Backup written: ${r.file}. ${r.restore}`); backups.reload(); } catch (e) { setErr(e); } }}>Create backup now</Btn>}>
        {msg && <Notice tone="good">{msg}</Notice>}
        <Table cols={[{ key: "name", label: "Backup" }, { key: "bytes", label: "Bytes" }, { key: "modified", label: "Created", render: (r) => fmtTime(r.modified) }]} rows={backups.data ?? []} empty="No backups yet." />
        <h4 className="mb-1 mt-4 text-sm font-medium">Agent-deleted files (recoverable trash)</h4>
        <Table cols={[{ key: "original", label: "Original path" }, { key: "deleted_at", label: "Deleted", render: (r) => fmtTime(r.deleted_at) }, { key: "a", label: "", render: (r) => <Btn small onClick={async () => { try { await api.post(`/api/trash/${r.id}/restore`); trash.reload(); } catch (e) { setErr(e); } }}>Restore</Btn> }]} rows={trash.data ?? []} empty="Trash is empty." />
      </Card>
      <Card title="Approval history"><Table cols={[{ key: "requested", label: "When", render: (r) => fmtTime(r.requested) }, { key: "tool", label: "Action", render: (r) => <code>{r.tool}</code> }, { key: "risk", label: "Risk" }, { key: "status", label: "Decision" }, { key: "decided_by", label: "By" }, { key: "reason", label: "Reason" }]} rows={approvals.data ?? []} empty="No approvals requested yet." /></Card>
      <Card title="Audit log" actions={ver.data && <Badge tone={ver.data.ok ? "good" : "bad"}>{ver.data.ok ? `hash chain intact (${ver.data.checked} entries)` : `TAMPERING DETECTED at entry ${ver.data.broken_at}`}</Badge>}>
        <Table cols={[{ key: "ts", label: "When", render: (r) => fmtTime(r.ts) }, { key: "actor", label: "Actor" }, { key: "event", label: "Event" }, { key: "details", label: "Details", render: (r) => <code className="break-all text-xs text-dim">{String(r.details).slice(0, 160)}</code> }]} rows={audit.data ?? []} />
      </Card>
    </div>
  );
}

export default function Settings() {
  const { data, reload, error } = useApi<J>("/api/settings");
  const [tab, setTab] = useState("perm");
  const [form, setForm] = useState<J>({});
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const sec = SECTIONS.find((s) => s.id === tab);
  const dirty = Object.keys(form).length > 0;
  return (
    <div className="space-y-4">
      <PageHeader title="Settings" sub={data ? <>Data folder: <code>{data.data_dir}</code></> : ""} actions={dirty && <Btn tone="primary" onClick={async () => { try { setErr(null); const r = await api.put("/api/settings", form); setMsg(`Saved: ${r.applied.join(", ")}`); setForm({}); reload(); } catch (e) { setErr(e); } }}>Save {Object.keys(form).length} change(s)</Btn>} />
      <Tabs value={tab} onChange={setTab} tabs={[...SECTIONS.map((s) => ({ id: s.id, label: s.title })), { id: "system", label: "Backup, audit & history" }]} />
      <ErrorBox error={error ?? err} />
      {msg && <Notice tone="good">{msg}</Notice>}
      {tab === "perm" && <Notice>Changing the execution mode or allow-lists is a human decision made here. The language model cannot do it, and every change is written to the audit log.</Notice>}
      {sec && data && (
        <Card title={sec.title}>
          {sec.intro && <p className="mb-3 text-sm text-dim">{sec.intro}</p>}
          <div className="grid gap-4 md:grid-cols-2">{sec.fields.map((f) => <Editor key={f.key} def={f} value={form[f.key] ?? data[f.key]} onChange={(v) => setForm({ ...form, [f.key]: v })} />)}</div>
        </Card>
      )}
      {tab === "system" && <System />}
    </div>
  );
}
