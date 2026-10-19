import { useState } from "react";
import { J, api, fmtBytes, fmtDur, useApi } from "../api";
import { Badge, Btn, Card, Check, Empty, ErrorBox, Field, Input, Markdown, Notice, PageHeader, Progress, Select, Stat, StatusBadge, Table, Tabs, Textarea } from "../ui";

export function FolderPicker({ onPick, onClose }: { onPick: (p: string) => void; onClose: () => void }) {
  const [path, setPath] = useState("");
  const { data, error } = useApi<J>(`/api/fs/browse?path=${encodeURIComponent(path)}`, 0, [path]);
  return (
    <Card title="Choose a file or folder" actions={<Btn small onClick={onClose}>Close</Btn>}>
      <ErrorBox error={error} />
      {data && (
        <div className="space-y-2">
          <div className="flex gap-2">
            <Btn small disabled={!data.parent && !data.path} onClick={() => setPath(data.parent ?? "")}>Up</Btn>
            <Btn small onClick={() => setPath(data.home)}>Home</Btn>
            <code className="flex-1 truncate rounded bg-panel2 px-2 py-1">{data.path || "(drives)"}</code>
            {data.path && <Btn small tone="primary" onClick={() => onPick(data.path)}>Use this folder</Btn>}
          </div>
          <div className="max-h-64 overflow-y-auto rounded-lg border border-line">
            {data.entries.map((e: J) => (
              <div key={e.path} className="flex items-center justify-between border-b border-line px-3 py-1 text-sm last:border-0 hover:bg-panel2">
                <button className="truncate text-left" onClick={() => (e.dir ? setPath(e.path) : onPick(e.path))}>{e.dir ? "[dir] " : ""}{e.name}</button>
                <span className="text-xs text-dim">{e.dir ? "" : fmtBytes(e.size)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

function NewJob({ onCreated }: { onCreated: (id: string) => void }) {
  const [sources, setSources] = useState("");
  const [name, setName] = useState("");
  const [collection, setCollection] = useState("default");
  const [adv, setAdv] = useState<J>({ batch_size: 64, chunk_chars: 1200, workers: 2, max_reject_rate: 0.6, include_patterns: "", exclude_patterns: "", tabular_retrieval: true, recursive: true });
  const [picker, setPicker] = useState(false);
  const [est, setEst] = useState<J | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const list = () => sources.split("\n").map((s) => s.trim()).filter(Boolean);
  const cfg = () => ({ ...adv, include_patterns: String(adv.include_patterns).split("\n").filter(Boolean), exclude_patterns: String(adv.exclude_patterns).split("\n").filter(Boolean) });
  return (
    <div className="space-y-3">
      <Card title="1. What to ingest">
        <Field label="Files, folders or public URLs (one per line)" hint="Supported: PDF, DOCX, TXT, MD, HTML, CSV, JSON/JSONL, XLSX, XML, Parquet, source code, configs, logs, and ZIP/TAR archives of those.">
          <Textarea rows={4} value={sources} onChange={(e) => setSources(e.target.value)} placeholder={"C:\\Users\\me\\Documents\\manuals\nhttps://example.com/docs/page.html"} aria-label="Sources" />
        </Field>
        <div className="mt-2 flex gap-2"><Btn onClick={() => setPicker(true)}>Browse...</Btn>
          <Btn onClick={async () => { try { setErr(null); setEst(await api.post("/api/ingest/estimate", { sources: list().filter((s) => !s.startsWith("http")) })); } catch (e) { setErr(e); } }} disabled={!list().length}>Estimate size and time</Btn></div>
        {picker && <div className="mt-3"><FolderPicker onClose={() => setPicker(false)} onPick={(p) => { setSources((s) => (s ? s + "\n" : "") + p); setPicker(false); }} /></div>}
        {est && <div className="mt-3 grid grid-cols-2 gap-2 md:grid-cols-4"><Stat label="Files" value={est.files} /><Stat label="Size" value={fmtBytes(est.bytes)} /><Stat label="Rough time" value={fmtDur(est.est_seconds)} sub="with the local embedder" /><Stat label="Disk free" value={fmtBytes(est.disk_free_mb * 2 ** 20)} /></div>}
        {est?.warning && <div className="mt-2"><Notice>{est.warning}</Notice></div>}
      </Card>
      <Card title="2. Where and how">
        <div className="grid gap-3 md:grid-cols-3">
          <Field label="Job name"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="optional" /></Field>
          <Field label="Collection" hint="Created if it does not exist"><Input value={collection} onChange={(e) => setCollection(e.target.value)} /></Field>
          <Field label="Batch size (chunks per checkpoint)" hint={est ? `suggested: ${est.suggested_batch_size}` : ""}><Input type="number" value={adv.batch_size} onChange={(e) => setAdv({ ...adv, batch_size: +e.target.value })} /></Field>
        </div>
        <details className="mt-3"><summary className="cursor-pointer text-sm text-accent">Filtering and quality rules</summary>
          <div className="mt-2 grid gap-3 md:grid-cols-3">
            <Field label="Chunk size (chars)"><Input type="number" value={adv.chunk_chars} onChange={(e) => setAdv({ ...adv, chunk_chars: +e.target.value })} /></Field>
            <Field label="Parallel workers"><Input type="number" value={adv.workers} onChange={(e) => setAdv({ ...adv, workers: +e.target.value })} /></Field>
            <Field label="Pause when this share is rejected" hint="0.6 = 60%"><Input type="number" step="0.05" value={adv.max_reject_rate} onChange={(e) => setAdv({ ...adv, max_reject_rate: +e.target.value })} /></Field>
            <Field label="Only keep chunks matching (regex, one per line)"><Textarea rows={2} value={adv.include_patterns} onChange={(e) => setAdv({ ...adv, include_patterns: e.target.value })} /></Field>
            <Field label="Reject chunks matching (regex, one per line)"><Textarea rows={2} value={adv.exclude_patterns} onChange={(e) => setAdv({ ...adv, exclude_patterns: e.target.value })} /></Field>
            <div className="space-y-2 pt-5"><Check checked={adv.recursive} onChange={(v) => setAdv({ ...adv, recursive: v })} label="Include sub-folders" /><Check checked={adv.tabular_retrieval} onChange={(v) => setAdv({ ...adv, tabular_retrieval: v })} label="Index table rows for search (off = profile only)" /></div>
          </div>
        </details>
      </Card>
      <ErrorBox error={err} />
      <Btn tone="primary" disabled={!list().length} onClick={async () => { try { setErr(null); const j = await api.post("/api/ingest/jobs", { name, collection, sources: list(), config: cfg() }); onCreated(j.id); } catch (e) { setErr(e); } }}>Start ingestion</Btn>
      <p className="text-xs text-dim">Original files are never modified. Questionable content is quarantined for review, not silently dropped. Ingestion adds searchable knowledge - it does not retrain the model.</p>
    </div>
  );
}

function JobDetail({ id }: { id: string }) {
  const { data: j, reload, error } = useApi<J>(`/api/ingest/jobs/${id}`, 1500, [id]);
  const [tab, setTab] = useState("items");
  const [filter, setFilter] = useState("");
  const items = useApi<J[]>(`/api/ingest/jobs/${id}/items?limit=300${filter ? `&status=${filter}` : ""}`, tab === "items" ? 3000 : 0, [id, filter, tab]);
  const rejects = useApi<J[]>(tab === "rejects" ? `/api/ingest/jobs/${id}/rejects?limit=300` : null, 0, [id, tab]);
  const report = useApi<J>(tab === "report" ? `/api/ingest/jobs/${id}/report` : null, 0, [id, tab, j?.status]);
  const profiles = useApi<J[]>(tab === "profiles" ? `/api/ingest/jobs/${id}/profiles` : null, 0, [id, tab]);
  const quarantine = useApi<J[]>(tab === "quarantine" ? `/api/quarantine?job=${id}` : null, 0, [id, tab]);
  const [err, setErr] = useState<unknown>(null);
  if (error && !j) return <ErrorBox error={error} />;
  if (!j) return null;
  const c = j.counts;
  const act = async (a: string, body: J = {}) => { try { setErr(null); await api.post(`/api/ingest/jobs/${id}/${a}`, body); reload(); } catch (e) { setErr(e); } };
  const running = j.status === "running";
  return (
    <Card title={<span>{j.name} <StatusBadge s={j.status} /></span>} actions={<>
      {running && <Btn small onClick={() => act("pause")}>Pause</Btn>}
      {["paused", "interrupted", "failed", "queued", "cancelled", "completed_with_errors"].includes(j.status) && <Btn small tone="primary" onClick={() => act(j.status === "queued" ? "start" : "resume")}>{j.status === "queued" ? "Start" : "Resume"}</Btn>}
      {j.status === "paused" && j.pause_reason.startsWith("Quality gate") && <Btn small tone="danger" title="Continue even though many records are rejected" onClick={() => act("resume", { force_quality: true })}>Resume anyway</Btn>}
      {c.items_failed > 0 && <Btn small onClick={() => act("retry")}>Retry {c.items_failed} failed</Btn>}
      {running && <Btn small tone="danger" onClick={() => act("cancel")}>Cancel</Btn>}
    </>}>
      <ErrorBox error={err} />
      {j.pause_reason && <Notice>{j.pause_reason}</Notice>}
      {j.error && <ErrorBox error={{ message: j.error }} />}
      <div className="my-3"><Progress value={c.items_discovered ? c.processed / c.items_discovered : 0} /><p className="mt-1 text-xs text-dim">{c.processed} of {c.items_discovered} items processed{c.records_seen ? ` - ${c.records_seen.toLocaleString()} records read` : ""}</p></div>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-7">
        <Stat label="Discovered" value={c.items_discovered} /><Stat label="Processed" value={c.processed} /><Stat label="Chunks indexed" value={c.chunks_indexed} tone="good" />
        <Stat label="Duplicates" value={c.duplicated} sub={`${c.chunks_near_duplicate} near`} /><Stat label="Rejected" value={c.rejected} tone={c.rejected ? "warn" : undefined} />
        <Stat label="Quarantined" value={c.quarantined} tone={c.quarantined ? "warn" : undefined} /><Stat label="Failed" value={c.failed} tone={c.failed ? "bad" : undefined} />
      </div>
      <div className="mt-4"><Tabs value={tab} onChange={setTab} tabs={[{ id: "items", label: "Files" }, { id: "rejects", label: "Rejected records" }, { id: "quarantine", label: "Quarantine" }, { id: "profiles", label: "Schema & quality" }, { id: "report", label: "Report" }]} /></div>
      <div className="mt-3">
        {tab === "items" && <>
          <div className="mb-2 w-48"><Select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by status"><option value="">All statuses</option>{["pending", "running", "done", "duplicate", "rejected", "quarantined", "failed"].map((s) => <option key={s}>{s}</option>)}</Select></div>
          <Table cols={[{ key: "source", label: "Source", render: (r) => <span className="break-all">{r.source}</span> }, { key: "kind", label: "Type" }, { key: "size", label: "Size", render: (r) => fmtBytes(r.size) }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> },
            { key: "counts", label: "Chunks", render: (r) => r.counts?.chunks_indexed ?? "" }, { key: "reason", label: "Note", render: (r) => <span className="text-dim">{r.error || r.reason}</span> }]} rows={items.data ?? []} />
        </>}
        {tab === "rejects" && <Table cols={[{ key: "item_id", label: "Item", render: (r) => <code>{r.item_id.slice(-6)}</code> }, { key: "record_index", label: "Record #" }, { key: "reason", label: "Reason" }, { key: "sample", label: "Sample", render: (r) => <code className="break-all text-dim">{r.sample}</code> }]} rows={rejects.data ?? []} empty="No rejected records." />}
        {tab === "quarantine" && <Quarantine rows={quarantine.data ?? []} reload={quarantine.reload} />}
        {tab === "profiles" && (profiles.data?.length ? profiles.data.map((p) => (
          <div key={p.source} className="mb-4"><h4 className="mb-1 font-medium">{p.source}</h4>
            <p className="text-sm text-dim">{p.profile.rows} rows, {p.profile.malformed_rows} malformed{p.profile.schema_drift && <Badge tone="warn"> schema changes inside file</Badge>}</p>
            <Table cols={[{ key: "col", label: "Column" }, { key: "type", label: "Type" }, { key: "null_pct", label: "% missing" }, { key: "distinct", label: "Distinct (sampled)" }, { key: "range", label: "Range / sample" }]}
              rows={Object.entries(p.profile.columns as J).map(([k, v]: [string, J]) => ({ id: k, col: k, type: v.type, null_pct: v.null_pct, distinct: v.distinct_sampled, range: v.type === "numeric" ? `${v.min} .. ${v.max} (mean ${v.mean})` : v.sample }))} /></div>
        )) : <Empty>No tabular data in this job.</Empty>)}
        {tab === "report" && (report.data ? <div><Markdown text={report.data.markdown} /><div className="mt-3"><Btn small onClick={() => navigator.clipboard?.writeText(report.data!.markdown)}>Copy report</Btn></div></div> : <p className="text-dim">Building report (includes a retrieval self-check)...</p>)}
      </div>
    </Card>
  );
}

function Quarantine({ rows, reload }: { rows: J[]; reload: () => void }) {
  return rows.length ? (
    <div className="space-y-2">
      <Notice>These chunks look like instructions aimed at an AI (possible prompt injection). They are NOT searchable. Release one only if you are sure it is harmless.</Notice>
      {rows.map((r) => (
        <div key={r.id} className="rounded-lg border border-line p-2 text-sm"><div className="text-xs text-dim">{r.source} {r.locator} - {r.reason}</div><pre className="my-1 max-h-32 overflow-auto">{r.text}</pre>
          <div className="flex gap-2"><Btn small onClick={async () => { await api.post(`/api/quarantine/${r.id}/approve`); reload(); }}>Release into knowledge base</Btn><Btn small tone="danger" onClick={async () => { await api.del(`/api/quarantine/${r.id}`); reload(); }}>Discard</Btn></div></div>
      ))}
    </div>
  ) : <Empty>Nothing quarantined.</Empty>;
}

function Datasets({ onImported }: { onImported: (id: string) => void }) {
  const [ref, setRef] = useState("");
  const [info, setInfo] = useState<J | null>(null);
  const [sel, setSel] = useState<string[]>([]);
  const [plan, setPlan] = useState<J | null>(null);
  const [approveLarge, setApproveLarge] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const dls = useApi<J[]>("/api/datasets/downloads", 2000);
  const [prev, setPrev] = useState<J | null>(null);
  const [coll, setColl] = useState("datasets");
  const guard = async (fn: () => Promise<void>) => { try { setErr(null); setBusy(true); await fn(); } catch (e) { setErr(e); } finally { setBusy(false); } };
  return (
    <div className="space-y-3">
      <Card title="Add a dataset">
        <Field label="Hugging Face dataset id (owner/name), direct URL, or local path" hint="Nothing is downloaded until you press Download. Access tokens are read from the HF_TOKEN environment variable only - never paste them here.">
          <div className="flex gap-2"><Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="stanfordnlp/imdb  |  https://host/file.csv  |  C:\data\mydata" aria-label="Dataset reference" />
            <Btn tone="primary" disabled={!ref || busy} onClick={() => guard(async () => { const i = await api.post("/api/datasets/inspect", { ref }); setInfo(i); setSel(i.files.filter((f: J) => f.supported).map((f: J) => f.name)); setPlan(null); })}>Inspect</Btn></div>
        </Field>
        <ErrorBox error={err} />
        {info && (
          <div className="mt-3 space-y-3">
            <div className="grid gap-2 md:grid-cols-4"><Stat label="Source" value={info.kind} /><Stat label="Licence" value={info.license} tone={/not declared|unknown/.test(info.license) ? "warn" : undefined} /><Stat label="Access" value={info.requires_auth ? "login required" : "public"} tone={info.requires_auth ? "warn" : "good"} sub={info.requires_auth ? (info.token_configured ? "HF_TOKEN set" : "HF_TOKEN not set") : ""} /><Stat label="Total size" value={fmtBytes(info.total_bytes)} /></div>
            {info.warnings?.map((w: string) => <Notice key={w}>{w}</Notice>)}
            <Table cols={[{ key: "sel", label: "", render: (r) => <input type="checkbox" aria-label={`select ${r.name}`} checked={sel.includes(r.name)} onChange={(e) => setSel(e.target.checked ? [...sel, r.name] : sel.filter((x) => x !== r.name))} /> }, { key: "name", label: "File" }, { key: "split", label: "Split" }, { key: "size", label: "Size", render: (r) => fmtBytes(r.size) }, { key: "supported", label: "Parseable", render: (r) => (r.supported ? "yes" : "no") }]} rows={info.files.map((f: J) => ({ ...f, id: f.name }))} />
            <div className="flex gap-2"><Btn disabled={!sel.length || busy} onClick={() => guard(async () => setPlan(await api.post("/api/datasets/plan", { ref, files: sel })))}>Check size, disk space and access</Btn></div>
          </div>
        )}
        {plan && (
          <div className="mt-3 space-y-2">
            <div className="grid gap-2 md:grid-cols-3"><Stat label="Selected" value={fmtBytes(plan.selected_bytes)} /><Stat label="Disk free" value={fmtBytes(plan.disk_free_bytes)} tone={plan.disk_ok ? "good" : "bad"} /><Stat label="Needs your explicit OK" value={plan.needs_explicit_approval ? "yes (large)" : "no"} tone={plan.needs_explicit_approval ? "warn" : "good"} /></div>
            {plan.needs_explicit_approval && <Check checked={approveLarge} onChange={setApproveLarge} label={`I approve downloading ${fmtBytes(plan.selected_bytes)}`} />}
            <Btn tone="primary" disabled={!plan.disk_ok || busy || (plan.needs_explicit_approval && !approveLarge)} onClick={() => guard(async () => { await api.post("/api/datasets/download", { ref, files: sel, approve_large: approveLarge }); dls.reload(); })}>Download</Btn>
          </div>
        )}
      </Card>
      <Card title="Downloads">
        <Table cols={[{ key: "ref", label: "Dataset", render: (r) => <span className="break-all">{r.ref}</span> }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> },
          { key: "p", label: "Progress", render: (r) => <div className="w-32"><Progress value={r.total_bytes ? r.done_bytes / r.total_bytes : 0} /><span className="text-xs text-dim">{fmtBytes(r.done_bytes)} / {fmtBytes(r.total_bytes)}</span></div> },
          { key: "err", label: "Note", render: (r) => <span className="text-bad">{r.error}</span> },
          { key: "a", label: "", render: (r) => <div className="flex gap-1">{r.status === "downloading" && <Btn small onClick={async () => { await api.post(`/api/datasets/downloads/${r.id}/cancel`); }}>Cancel</Btn>}
            {r.status === "ready" && <><Btn small onClick={() => guard(async () => setPrev({ id: r.id, ...(await api.get(`/api/datasets/downloads/${r.id}/preview`)) }))}>Preview</Btn></>}</div> }]} rows={dls.data ?? []} empty="No datasets downloaded yet." />
        {prev && (
          <div className="mt-3 space-y-2"><h4 className="font-medium">Preview: {prev.file}</h4>
            <p className="text-sm text-dim">Columns: {prev.columns?.join(", ")}{prev.num_rows ? ` - ${prev.num_rows} rows` : ""}</p>
            <pre className="max-h-56 overflow-auto rounded-lg bg-panel2 p-2">{JSON.stringify(prev.rows, null, 2)}</pre>
            <div className="flex items-end gap-2"><Field label="Import into collection"><Input value={coll} onChange={(e) => setColl(e.target.value)} /></Field>
              <Btn tone="primary" onClick={() => guard(async () => { const j = await api.post(`/api/datasets/downloads/${prev.id}/import`, { collection: coll }); onImported(j.id); })}>Import into pipeline</Btn></div></div>
        )}
      </Card>
      <p className="text-xs text-dim">Command line: <code>python -m nexus.cli dataset inspect owner/name</code> - see DATASET_GUIDE.md.</p>
    </div>
  );
}

export default function Ingestion() {
  const [tab, setTab] = useState("jobs");
  const [sel, setSel] = useState<string | null>(null);
  const jobs = useApi<J[]>("/api/ingest/jobs", 2500);
  const go = (id: string) => { setSel(id); setTab("jobs"); jobs.reload(); };
  return (
    <div className="space-y-4">
      <PageHeader title="Data ingestion" sub="Large-scale, resumable import of documents and datasets into searchable knowledge." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "jobs", label: `Jobs${jobs.data ? ` (${jobs.data.length})` : ""}` }, { id: "new", label: "New job" }, { id: "datasets", label: "Dataset manager" }]} />
      {tab === "new" && <NewJob onCreated={go} />}
      {tab === "datasets" && <Datasets onImported={go} />}
      {tab === "jobs" && (
        <div className="space-y-4">
          <ErrorBox error={jobs.error} />
          <Table onRow={(r) => setSel(r.id)} cols={[{ key: "name", label: "Job" }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> },
            { key: "p", label: "Progress", render: (r) => <div className="w-28"><Progress value={r.counts.items_discovered ? r.counts.processed / r.counts.items_discovered : 0} /></div> },
            { key: "i", label: "Indexed", render: (r) => r.counts.chunks_indexed }, { key: "r", label: "Rejected", render: (r) => r.counts.rejected }, { key: "f", label: "Failed", render: (r) => r.counts.failed }]}
            rows={jobs.data ?? []} empty="No ingestion jobs yet. Use 'New job' or 'Dataset manager'." />
          {sel && <JobDetail id={sel} />}
        </div>
      )}
    </div>
  );
}
