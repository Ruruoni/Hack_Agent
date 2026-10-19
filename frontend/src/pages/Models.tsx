import { useState } from "react";
import { J, api, fmtBytes, fmtTime, useApi } from "../api";
import { Badge, Btn, Card, Check, Empty, ErrorBox, Field, Input, Notice, PageHeader, Progress, Select, Stat, StatusBadge, Table, Tabs, Textarea } from "../ui";

function Connection() {
  const st = useApi<J>("/api/llm/status", 6000);
  const models = useApi<J>("/api/llm/models");
  const emb = useApi<J>("/api/embeddings/status");
  const settings = useApi<J>("/api/settings");
  const [msg, setMsg] = useState<string>("");
  const [err, setErr] = useState<unknown>(null);
  const [form, setForm] = useState<J>({});
  const run = async (label: string, fn: () => Promise<any>) => { try { setErr(null); setMsg(`${label}...`); const r = await fn(); setMsg(typeof r === "string" ? r : `${label}: done`); st.reload(); models.reload(); emb.reload(); settings.reload(); } catch (e) { setErr(e); setMsg(""); } };
  const s = settings.data;
  const val = (k: string) => form[k] ?? s?.[k] ?? "";
  const caps = st.data?.capabilities;
  return (
    <div className="space-y-4">
      <ErrorBox error={err} />
      {msg && <Notice tone="accent">{msg}</Notice>}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="LM Studio connection" actions={<Btn small onClick={() => run("Testing the model", async () => { const r = await api.post("/api/llm/test", {}); return `Reply from ${r.model} in ${r.seconds}s: "${r.reply.trim()}"`; })}>Test generation</Btn>}>
          <div className="space-y-1 text-sm">
            <div>Server: <code>{st.data?.base_url}</code> {st.data?.health?.ok ? <Badge tone="good">connected</Badge> : <Badge tone="bad">unreachable</Badge>}</div>
            {!st.data?.health?.ok && st.data?.health && <ErrorBox error={{ message: st.data.health.message, hint: st.data.health.hint }} />}
            <div>Active model: <b>{caps?.model ?? st.data?.configured_model ?? "-"}</b></div>
            {caps && <div className="flex flex-wrap gap-2 pt-1"><Badge tone={caps.native_tools ? "good" : "warn"}>{caps.native_tools ? "native tool calling" : "JSON-action fallback"}</Badge><Badge tone={caps.vision ? "good" : "neutral"}>{caps.vision ? "vision" : "text only"}</Badge>{caps.max_context && <Badge>max ctx {caps.max_context}</Badge>}{caps.loaded_context && <Badge>loaded ctx {caps.loaded_context}</Badge>}</div>}
            {caps?.limitations?.length > 0 && <ul className="list-disc pl-5 pt-2 text-dim">{caps.limitations.map((l: string) => <li key={l}>{l}</li>)}</ul>}
          </div>
          <div className="mt-3 flex gap-2"><Btn small onClick={() => run("Probing tool calling", async () => { const r = await api.post("/api/llm/probe-tools"); return r.native_tool_calling ? "This model supports native tool calling." : "No native tool calling detected - the validated JSON-action fallback will be used."; })}>Probe tool calling</Btn></div>
        </Card>
        <Card title="Generation settings" actions={<Btn small tone="primary" onClick={() => run("Saving", () => api.post("/api/llm/select", { context_window: +val("context_window"), temperature: +val("temperature"), top_p: +val("top_p"), max_tokens: +val("max_tokens"), tool_mode: val("tool_mode") }))}>Save</Btn>}>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Prompt budget (context window, tokens)" hint="Set this to the context length you loaded the model with in LM Studio."><Input type="number" value={val("context_window")} onChange={(e) => setForm({ ...form, context_window: e.target.value })} /></Field>
            <Field label="Max answer tokens"><Input type="number" value={val("max_tokens")} onChange={(e) => setForm({ ...form, max_tokens: e.target.value })} /></Field>
            <Field label="Temperature"><Input type="number" step="0.1" value={val("temperature")} onChange={(e) => setForm({ ...form, temperature: e.target.value })} /></Field>
            <Field label="Top-p"><Input type="number" step="0.05" value={val("top_p")} onChange={(e) => setForm({ ...form, top_p: e.target.value })} /></Field>
            <Field label="Tool calling mode"><Select value={val("tool_mode")} onChange={(e) => setForm({ ...form, tool_mode: e.target.value })}><option value="auto">auto (detect)</option><option value="native">native</option><option value="prompt">JSON-action fallback</option></Select></Field>
          </div>
        </Card>
      </div>
      <Card title="Models in LM Studio" actions={<Btn small onClick={models.reload}>Refresh</Btn>}>
        <ErrorBox error={models.error} />
        <Table cols={[{ key: "id", label: "Model" }, { key: "type", label: "Type" }, { key: "loaded", label: "State", render: (r) => (r.loaded == null ? <span className="text-dim">unknown</span> : <StatusBadge s={r.loaded ? "active" : "pending"} />) },
          { key: "ctx", label: "Context", render: (r) => r.loaded_context ?? r.max_context ?? "-" }, { key: "quantization", label: "Quant" }, { key: "capabilities", label: "Capabilities", render: (r) => (r.capabilities ?? []).join(", ") },
          { key: "a", label: "", render: (r) => <div className="flex gap-1">
            {r.type !== "embedding" && <Btn small tone={models.data?.selected === r.id ? "good" : "neutral"} onClick={() => run("Selecting", () => api.post("/api/llm/select", { chat_model: r.id }))}>{models.data?.selected === r.id ? "Selected" : "Use for chat"}</Btn>}
            {r.type === "embedding" && <Btn small onClick={() => run("Selecting embedder", () => api.post("/api/llm/select", { embedding_model: r.id, embedding_provider: "lmstudio" }))}>Use for embeddings</Btn>}
            <Btn small onClick={() => run("Loading", () => api.post("/api/llm/load", { model: r.id, context_length: +val("context_window") || undefined }))}>Load</Btn>
            <Btn small onClick={() => run("Unloading", () => api.post("/api/llm/unload", { model: r.id }))}>Unload</Btn></div> }]} rows={models.data?.models ?? []} empty="LM Studio reported no models." />
        <p className="mt-2 text-xs text-dim">Load/unload uses LM Studio's REST API when your version supports it; otherwise use the LM Studio app or `lms load`. See LM_STUDIO_GUIDE.md.</p>
      </Card>
      <Card title="Embedding model (for semantic search)">
        <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">Current: <code>{emb.data?.id}</code> {emb.data?.ok ? <Badge tone="good">working (dim {emb.data.dim})</Badge> : emb.data?.error ? <Badge tone="bad">{emb.data.error.message}</Badge> : null}{emb.data && !emb.data.semantic && <Badge tone="warn">lexical fallback</Badge>}</div>
        {emb.data?.note && <Notice>{emb.data.note}</Notice>}
        {emb.data?.collections_needing_reindex?.length > 0 && <div className="mt-2"><Notice>Re-index needed after changing the embedding model: {emb.data!.collections_needing_reindex.join(", ")} (Knowledge &gt; Collections &gt; Re-index).</Notice></div>}
        <div className="mt-3 grid gap-3 md:grid-cols-3">
          <Field label="Provider"><Select value={val("embedding_provider")} onChange={(e) => setForm({ ...form, embedding_provider: e.target.value })}><option value="hash">Built-in (lexical, offline)</option><option value="lmstudio">LM Studio embedding model</option><option value="sentence_transformers">sentence-transformers (local, optional)</option></Select></Field>
          <Field label="Model id"><Input value={val("embedding_model")} onChange={(e) => setForm({ ...form, embedding_model: e.target.value })} placeholder="text-embedding-nomic-embed-text-v1.5" /></Field>
          <div className="pt-5"><Btn tone="primary" onClick={() => run("Saving embedder", () => api.post("/api/llm/select", { embedding_provider: val("embedding_provider"), embedding_model: val("embedding_model") }))}>Save embedder</Btn></div>
        </div>
      </Card>
    </div>
  );
}

function Learning() {
  const ov = useApi<J>("/api/learning/overview", 5000);
  const cases = useApi<J[]>("/api/eval/cases");
  const [run, setRun] = useState<J | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const d = ov.data;
  if (!d) return <ErrorBox error={ov.error} />;
  const mem = Object.entries(d.persistent_learning.memories as Record<string, number>);
  return (
    <div className="space-y-4">
      <Notice tone="accent">Three different mechanisms. Only the third one changes the language model itself.</Notice>
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="1. Data ingestion" className="border-accent/40"><p className="mb-2 text-sm text-dim">{d.data_ingestion.what}</p>
          <div className="grid grid-cols-2 gap-2"><Stat label="Collections" value={d.data_ingestion.collections} /><Stat label="Documents" value={d.data_ingestion.documents} /><Stat label="Chunks indexed" value={d.data_ingestion.chunks_indexed} /><Stat label="Quarantined" value={d.data_ingestion.quarantined} tone={d.data_ingestion.quarantined ? "warn" : undefined} /></div>
          <p className="mt-2 text-xs text-dim">Jobs running: {d.data_ingestion.jobs_running}. Logs and reports: Data ingestion.</p></Card>
        <Card title="2. Persistent learning" className="border-good/40"><p className="mb-2 text-sm text-dim">{d.persistent_learning.what}</p>
          <div className="grid grid-cols-2 gap-2"><Stat label="Memories" value={mem.reduce((a, [, n]) => a + n, 0)} sub={mem.map(([k, n]) => `${k} ${n}`).join(" - ")} /><Stat label="Feedback items" value={d.persistent_learning.feedback_items} /><Stat label="Workflows approved" value={d.persistent_learning.workflows_approved} sub={`${d.persistent_learning.workflow_candidates} candidate(s)`} /><Stat label="Regression cases" value={d.persistent_learning.regression_cases} /></div></Card>
        <Card title="3. Model weights (fine-tuning)" className="border-warn/40"><p className="mb-2 text-sm text-dim">{d.model_weights.what}</p>
          <Stat label="Weights modified?" value={d.model_weights.weights_modified ? "yes (adapter active)" : "no"} tone={d.model_weights.weights_modified ? "warn" : "good"} sub={d.model_weights.statement} />
          <p className="mt-2 text-xs text-dim">Training runs: {d.model_weights.training_runs}. Manage under "Fine-tuning".</p></Card>
      </div>
      <Card title="Regression suite" actions={<Btn small tone="primary" onClick={async () => { try { setErr(null); setRun(await api.post("/api/eval/run")); } catch (e) { setErr(e); } }}>Run all now</Btn>}>
        <p className="mb-2 text-sm text-dim">Every verified correction and approved workflow adds a test, so later improvements cannot silently break earlier behaviour. Checks that need the model are skipped (never counted as passed) when LM Studio is offline.</p>
        <ErrorBox error={err} />
        {run && <div className="mb-3 flex gap-2"><Badge tone="good">{run.passed} passed</Badge><Badge tone={run.failed ? "bad" : "neutral"}>{run.failed} failed</Badge><Badge>{run.skipped} skipped</Badge>{run.regressions?.length > 0 && <Badge tone="bad">regressions: {run.regressions.join(", ")}</Badge>}</div>}
        <Table cols={[{ key: "name", label: "Case" }, { key: "kind", label: "Kind" }, { key: "origin", label: "Created from", render: (r) => <span className="text-dim">{r.origin || "-"}</span> }, { key: "res", label: "Last result", render: (r) => { const x = run?.results.find((y: J) => y.case_id === r.id); return x ? <span><StatusBadge s={x.status} /> <span className="text-xs text-dim">{x.detail}</span></span> : null; } },
          { key: "d", label: "", render: (r) => <Btn small onClick={async () => { await api.del(`/api/eval/cases/${r.id}`); cases.reload(); }}>Remove</Btn> }]} rows={cases.data ?? []} empty="No regression cases yet. They are created when you correct the agent or approve workflows." />
      </Card>
    </div>
  );
}

function FineTuning() {
  const hw = useApi<J>("/api/training/hardware");
  const runs = useApi<J[]>("/api/training/runs", 3000);
  const dsets = useApi<J[]>("/api/training/datasets");
  const reg = useApi<J[]>("/api/training/registry", 0);
  const [chk, setChk] = useState<J | null>(null);
  const [form, setForm] = useState<J>({ model: "", source: "", license: "", confirm: false, name: "", method: "lora", dataset: "", epochs: 1, lr: 0.0002, rank: 16, seqlen: 512 });
  const [sel, setSel] = useState<string | null>(null);
  const run = useApi<J>(sel ? `/api/training/runs/${sel}` : null, 3000, [sel]);
  const exp = useApi<J>(sel && run.data?.status === "awaiting_decision" || run.data?.status === "accepted" ? `/api/training/runs/${sel}/export-plan` : null, 0, [sel, run.data?.status]);
  const [err, setErr] = useState<unknown>(null);
  const [lm, setLm] = useState("");
  const f = (k: string, v: any) => setForm({ ...form, [k]: v });
  const go = async (fn: () => Promise<any>) => { try { setErr(null); await fn(); runs.reload(); run.reload(); dsets.reload(); reg.reload(); } catch (e) { setErr(e); } };
  const r = run.data;
  return (
    <div className="space-y-4">
      <Notice tone="warn">Fine-tuning changes model weights. It is optional, never starts automatically, needs your explicit approval, never overwrites the original model, and its result must be exported to GGUF before LM Studio can load it. Retrieval and memory keep working without it.</Notice>
      <ErrorBox error={err} />
      <Card title="Can this computer train?">
        {hw.data && <div className="space-y-2"><div className="grid grid-cols-2 gap-2 md:grid-cols-4"><Stat label="GPU" value={hw.data.gpus[0]?.name ?? "none"} sub={hw.data.gpus[0] ? `${hw.data.gpus[0].vram_free_mb} MB free VRAM` : ""} /><Stat label="RAM free" value={fmtBytes(hw.data.ram_available_mb * 2 ** 20)} /><Stat label="Disk free" value={fmtBytes(hw.data.disk_free_mb * 2 ** 20)} /><Stat label="PyTorch" value={hw.data.torch.installed ? `${hw.data.torch.version}${hw.data.torch.cuda ? " + CUDA" : " (CPU)"}` : "not installed"} /></div>
          <div className="flex flex-wrap gap-2">{Object.entries(hw.data.libraries as Record<string, boolean>).map(([k, v]) => <Badge key={k} tone={v ? "good" : "neutral"}>{k}: {v ? "installed" : "missing"}</Badge>)}</div>
          {hw.data.blockers.map((b: string) => <Notice key={b}>{b}</Notice>)}</div>}
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="1. Training data">
          <div className="space-y-3"><Field label="Training file (JSONL: messages / prompt+completion / instruction+output)"><Input value={form.source} onChange={(e) => f("source", e.target.value)} placeholder="C:\data\my_examples.jsonl" /></Field>
            <div className="grid grid-cols-2 gap-3"><Field label="Licence of this data" hint="e.g. mit, apache-2.0, cc-by-4.0, user-owned"><Input value={form.license} onChange={(e) => f("license", e.target.value)} /></Field><Field label="Name"><Input value={form.name} onChange={(e) => f("name", e.target.value)} /></Field></div>
            <Check checked={form.confirm} onChange={(v) => f("confirm", v)} label="I confirm I have the right to use this data to fine-tune a model" />
            <Btn tone="primary" disabled={!form.source || !form.license || !form.confirm} onClick={() => go(async () => { await api.post("/api/training/datasets", { source: form.source, license: form.license, permitted_use_confirmed: true, name: form.name }); })}>Validate, clean and split</Btn></div>
          <p className="mt-2 text-xs text-dim">Duplicates, low-quality rows and prompt-injection-like text are removed; splits are deterministic and leak-checked.</p>
          <div className="mt-3"><Table cols={[{ key: "name", label: "Dataset" }, { key: "license", label: "Licence" }, { key: "splits", label: "train / val / test", render: (r) => `${r.splits.train} / ${r.splits.val} / ${r.splits.test}` }, { key: "version", label: "Version" }]} rows={dsets.data ?? []} empty="No prepared datasets." /></div>
        </Card>
        <Card title="2. Base model & plan">
          <div className="space-y-3"><Field label="Hugging Face-format model folder (not a .gguf)" hint="LM Studio's GGUF files cannot be fine-tuned here; use the original model's folder."><div className="flex gap-2"><Input value={form.model} onChange={(e) => f("model", e.target.value)} placeholder="C:\models\Qwen2.5-0.5B-Instruct" /><Btn onClick={() => go(async () => setChk(await api.post("/api/training/check-model", { ref: form.model })))}>Check</Btn></div></Field>
            {chk && (chk.trainable ? <Notice tone="good">Compatible: {chk.model_type}, about {(chk.approx_params / 1e9).toFixed(2)} B parameters. LoRA targets: {chk.target_modules?.join(", ")}.{chk.gguf_convertible ? " Can be converted back to GGUF." : ""}</Notice> : <Notice>{chk.message || chk.issues?.join("; ")}</Notice>)}
            <div className="grid grid-cols-2 gap-3"><Field label="Dataset"><Select value={form.dataset} onChange={(e) => f("dataset", e.target.value)}><option value="">choose...</option>{dsets.data?.map((d) => <option key={d.id} value={d.id}>{d.name} ({d.version})</option>)}</Select></Field>
              <Field label="Method"><Select value={form.method} onChange={(e) => f("method", e.target.value)}><option value="lora">LoRA</option><option value="qlora">QLoRA (NVIDIA GPU)</option></Select></Field>
              <Field label="Epochs"><Input type="number" value={form.epochs} onChange={(e) => f("epochs", +e.target.value)} /></Field><Field label="Learning rate"><Input type="number" step="0.0001" value={form.lr} onChange={(e) => f("lr", +e.target.value)} /></Field></div>
            <Btn tone="primary" disabled={!form.model || !form.dataset} onClick={() => go(async () => { const p = await api.post("/api/training/plan", { base_model: form.model, dataset_id: form.dataset, method: form.method, params: { epochs: form.epochs, learning_rate: form.lr, rank: form.rank, max_seq_len: form.seqlen } }); setSel(p.id); })}>Create plan and estimate resources</Btn></div>
        </Card>
      </div>
      <Card title="3. Runs"><Table onRow={(r) => setSel(r.id)} cols={[{ key: "id", label: "Run", render: (r) => <code>{r.id.slice(-8)}</code> }, { key: "base_model", label: "Base model", render: (r) => r.base_model.split(/[\\/]/).pop() }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "created", label: "Created", render: (r) => fmtTime(r.created) }]} rows={runs.data ?? []} empty="No training runs." /></Card>
      {r && (
        <Card title={<span>Run {r.id.slice(-8)} <StatusBadge s={r.status} /></span>}>
          <div className="grid gap-3 md:grid-cols-3"><Stat label="Estimated memory" value={r.estimate.known ? `${r.estimate.memory_gib} GiB` : "unknown"} sub={r.estimate.known ? `device: ${r.estimate.device}` : ""} /><Stat label="Feasible here" value={r.estimate.feasible ? "yes" : "no"} tone={r.estimate.feasible ? "good" : "bad"} /><Stat label="Dataset" value={r.dataset.name} sub={`${r.dataset.splits.train} train / ${r.dataset.splits.test} test - ${r.dataset.license}`} /></div>
          {r.estimate.reasons?.map((x: string) => <p key={x} className="mt-1 text-sm text-warn">{x}</p>)}
          <p className="mt-1 text-xs text-dim">{r.estimate.note}</p>
          <div className="mt-3 flex flex-wrap gap-2">
            {r.status === "planned" && <Btn tone="primary" onClick={() => go(() => api.post(`/api/training/runs/${r.id}/request-approval`))}>Request approval to train</Btn>}
            {r.status === "awaiting_approval" && <><Notice>Waiting for your approval (header &gt; Approvals).</Notice><Btn tone="danger" onClick={() => go(() => api.post(`/api/training/runs/${r.id}/start`))}>Start training (after approving)</Btn></>}
            {r.status === "running" && <Btn onClick={() => go(() => api.post(`/api/training/runs/${r.id}/stop`))}>Stop gracefully</Btn>}
          </div>
          {r.progress && <div className="mt-3"><Progress value={r.progress.max_steps ? r.progress.step / r.progress.max_steps : 0} /><p className="text-xs text-dim">step {r.progress.step}/{r.progress.max_steps}</p></div>}
          {r.log_tail && <details className="mt-3"><summary className="cursor-pointer text-sm text-accent">Training log</summary><pre className="max-h-56 overflow-auto rounded-lg bg-panel2 p-2">{r.log_tail}</pre></details>}
          {r.metrics?.comparison && (
            <div className="mt-4 space-y-2"><h4 className="font-medium">Evaluation: adapted vs baseline <Badge tone={r.metrics.overall === "improved" ? "good" : r.metrics.overall === "regressed" ? "bad" : "warn"}>{r.metrics.overall}</Badge></h4>
              <Table cols={[{ key: "split", label: "Held-out split" }, { key: "b", label: "Baseline loss" }, { key: "t", label: "Tuned loss" }, { key: "d", label: "Change" }, { key: "v", label: "Verdict", render: (x) => <StatusBadge s={x.verdict === "improved" ? "good" : x.verdict === "regressed" ? "fail" : "pending"} /> }]}
                rows={Object.entries(r.metrics.comparison as J).map(([k, v]: [string, J]) => ({ id: k, split: k, b: v.baseline_loss.toFixed(4), t: v.tuned_loss.toFixed(4), d: `${v.relative_change_pct}%`, v: v.verdict }))} />
              <p className="text-xs text-dim">{r.metrics.caveat} Original model files unchanged: <b>{r.metrics.base_model_unchanged ? "verified" : "CHANGED - do not use"}</b>.</p>
              {r.status === "awaiting_decision" && <div className="flex gap-2"><Btn tone="good" onClick={() => go(() => api.post(`/api/training/runs/${r.id}/decide`, { accept: true }))}>Accept result</Btn><Btn tone="danger" onClick={() => go(() => api.post(`/api/training/runs/${r.id}/decide`, { accept: false }))}>Reject</Btn></div>}</div>
          )}
          {r.metrics?.error && <ErrorBox error={{ message: r.metrics.error, hint: r.metrics.log_tail }} />}
          {exp.data && (
            <details className="mt-3" open><summary className="cursor-pointer text-sm font-medium text-accent">Export to LM Studio (required: a Transformers checkpoint is not loadable in LM Studio)</summary>
              {exp.data.missing.map((m: string) => <p key={m} className="text-sm text-warn">{m}</p>)}
              <ol className="mt-2 list-decimal space-y-2 pl-5 text-sm">{exp.data.steps.map((s: J) => <li key={s.step}>{s.what}{s.cmd && <pre className="mt-1 rounded bg-panel2 p-2">{s.cmd}</pre>}</li>)}</ol></details>
          )}
        </Card>
      )}
      <Card title="4. Model registry, activation and rollback">
        <Table cols={[{ key: "name", label: "Adapter" }, { key: "status", label: "Status", render: (x) => <StatusBadge s={x.status} /> }, { key: "created", label: "Created", render: (x) => fmtTime(x.created) },
          { key: "a", label: "", render: (x) => x.status === "active" ? <Btn small tone="danger" onClick={() => go(() => api.post(`/api/training/registry/${x.id}/rollback`))}>Roll back to previous model</Btn>
            : ["accepted", "rolled_back"].includes(x.status) ? <div className="flex gap-1"><Input className="!w-56" placeholder="model id as shown in LM Studio" value={lm} onChange={(e) => setLm(e.target.value)} /><Btn small disabled={!lm} onClick={() => go(() => api.post(`/api/training/registry/${x.id}/activate`, { lmstudio_model_id: lm }))}>Activate</Btn></div> : null }]} rows={reg.data ?? []} empty="No accepted adapters. The original model is in use." />
        <p className="mt-2 text-xs text-dim">Activating switches NEXUS's chat model setting to the exported model you loaded in LM Studio; rollback restores the previous setting. The original model files are never modified.</p>
      </Card>
    </div>
  );
}

export default function Models() {
  const [tab, setTab] = useState("connection");
  return (
    <div className="space-y-4">
      <PageHeader title="Models & learning" sub="LM Studio connection, what the agent has learned, and optional weight fine-tuning." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "connection", label: "LM Studio & models" }, { id: "learning", label: "Learning systems & regression tests" }, { id: "finetune", label: "Fine-tuning (optional)" }]} />
      {tab === "connection" && <Connection />}
      {tab === "learning" && <Learning />}
      {tab === "finetune" && <FineTuning />}
    </div>
  );
}
