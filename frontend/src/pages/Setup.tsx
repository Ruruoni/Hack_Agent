import { useState } from "react";
import { J, api, useApi } from "../api";
import { Badge, Btn, Card, ErrorBox, Input, Notice, PageHeader, Progress, Select } from "../ui";

export default function Setup() {
  const steps = useApi<J[]>("/api/setup/steps");
  const models = useApi<J>("/api/llm/models");
  const [results, setResults] = useState<Record<string, J>>({});
  const [running, setRunning] = useState("");
  const [url, setUrl] = useState("https://example.com");
  const [model, setModel] = useState("");
  const [embProv, setEmbProv] = useState("hash");
  const [embModel, setEmbModel] = useState("");
  const [err, setErr] = useState<unknown>(null);

  const run = async (id: string, body: J = {}) => {
    setRunning(id);
    try { setErr(null); const r = await api.post(`/api/setup/run/${id}`, body); setResults((x) => ({ ...x, [id]: r })); steps.reload(); if (id === "select_model") models.reload(); }
    catch (e) { setErr(e); }
    finally { setRunning(""); }
  };
  const runAll = async () => {
    for (const s of steps.data ?? []) {
      if (["memory_persist", "web_import"].includes(s.id)) continue;
      await run(s.id, s.id === "select_model" && model ? { model } : {});
      if (!(await api.get<J[]>("/api/setup/steps")).find((x) => x.id === s.id)?.last?.ok && ["python", "lmstudio", "models", "database"].includes(s.id)) break;
    }
  };
  const done = (steps.data ?? []).filter((s) => s.last?.ok).length;
  const total = steps.data?.length ?? 15;
  const extra: Record<string, JSX.Element> = {
    select_model: <Select className="!w-56" value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model"><option value="">automatic (first loaded)</option>{models.data?.models?.filter((m: J) => m.type !== "embedding").map((m: J) => <option key={m.id}>{m.id}</option>)}</Select>,
    embedding: <div className="flex gap-2"><Select className="!w-44" value={embProv} onChange={(e) => setEmbProv(e.target.value)} aria-label="Embedding provider"><option value="hash">built-in (offline)</option><option value="lmstudio">LM Studio model</option></Select><Input className="!w-56" placeholder="embedding model id" value={embModel} onChange={(e) => setEmbModel(e.target.value)} /></div>,
    web_import: <Input className="!w-72" value={url} onChange={(e) => setUrl(e.target.value)} aria-label="URL to import" />,
  };
  const body = (id: string): J => id === "select_model" ? (model ? { model } : {}) : id === "embedding" ? { provider: embProv, model: embModel } : id === "web_import" ? { url } : {};
  return (
    <div className="space-y-4">
      <PageHeader title="Setup wizard" sub="Fifteen checks that prove each part of NEXUS works on this computer." actions={<Btn tone="primary" onClick={runAll} disabled={!!running}>Run all basic checks</Btn>} />
      <Progress value={done / total} />
      <p className="text-sm text-dim">{done} of {total} checks passed. You can run them one at a time and fix problems as they appear. Written guide: INSTALL_WINDOWS.md.</p>
      <ErrorBox error={err} />
      {steps.data?.map((s, i) => {
        const r = results[s.id];
        const last = s.last;
        const ok = r ? r.ok : last?.ok;
        return (
          <Card key={s.id} title={<span>{i + 1}. {s.title} {ok === true && <Badge tone="good">passed</Badge>}{ok === false && <Badge tone="bad">failed</Badge>}</span>}
            actions={<>
              {s.id === "memory_persist" && <Btn small onClick={() => run(s.id, { verify: true })} disabled={!!running}>Verify after restart</Btn>}
              <Btn small tone="primary" onClick={() => run(s.id, body(s.id))} disabled={!!running}>{running === s.id ? "Running..." : s.id === "memory_persist" ? "Write marker" : "Run"}</Btn></>}>
            <p className="text-sm text-dim">{s.description}</p>
            {extra[s.id] && <div className="mt-2">{extra[s.id]}</div>}
            {(r?.detail || last?.detail) && <p className={`mt-2 text-sm ${ok ? "" : "text-bad"}`}>{r?.detail ?? last?.detail}</p>}
            {r?.hint && !r.ok && <div className="mt-2"><Notice>{r.hint}</Notice></div>}
            {r?.chat && <p className="mt-1 text-xs text-dim">Chat models: {r.chat.join(", ")}{r.embedding.length ? ` - Embedding: ${r.embedding.join(", ")}` : ""}</p>}
          </Card>
        );
      })}
    </div>
  );
}
