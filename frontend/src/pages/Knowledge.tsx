import { useState } from "react";
import { J, api, fmtTime, useApi } from "../api";
import { Badge, Btn, Card, Empty, ErrorBox, Field, Input, Notice, PageHeader, Select, StatusBadge, Table, Tabs, Textarea } from "../ui";

const LAYERS = ["semantic", "episodic", "procedural", "preference", "project"];
const LAYER_HELP: Record<string, string> = {
  semantic: "Facts and concepts", episodic: "Past tasks and corrections", procedural: "How-to knowledge and lessons", preference: "Your standing preferences (always applied)", project: "Project-scoped notes",
};

function Collections() {
  const { data, reload, error } = useApi<J[]>("/api/collections", 4000);
  const [sel, setSel] = useState<J | null>(null);
  const docs = useApi<J[]>(sel ? `/api/collections/${sel.id}/documents` : null, 0, [sel?.id]);
  const [doc, setDoc] = useState<J | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [name, setName] = useState("");
  const run = async (label: string, fn: () => Promise<unknown>) => { try { setErr(null); setBusy(label); await fn(); reload(); docs.reload(); } catch (e) { setErr(e); } finally { setBusy(""); } };
  return (
    <div className="space-y-3">
      <ErrorBox error={error ?? err} />
      <Card title="Collections" actions={<div className="flex gap-2"><Input className="!w-40" placeholder="new collection" value={name} onChange={(e) => setName(e.target.value)} aria-label="New collection name" /><Btn small disabled={!name} onClick={() => run("create", async () => { await api.post("/api/collections", { name }); setName(""); })}>Create</Btn></div>}>
        <Table onRow={setSel} cols={[{ key: "name", label: "Name" }, { key: "documents", label: "Documents" }, { key: "chunks", label: "Chunks" }, { key: "quarantined", label: "Quarantined" }, { key: "embedder", label: "Embedder", render: (r) => <code>{r.embedder || "-"}</code> },
          { key: "s", label: "", render: (r) => (r.needs_reindex ? <Badge tone="warn" title="Built with a different embedding model; semantic search is off until re-indexed">needs re-index</Badge> : null) },
          { key: "a", label: "", render: (r) => <div className="flex gap-1" onClick={(e) => e.stopPropagation()}>
            <Btn small disabled={!!busy} onClick={() => run("reindex", () => api.post(`/api/collections/${r.id}/reindex`))}>{busy === "reindex" ? "Re-indexing..." : "Re-index"}</Btn>
            <a className="rounded-lg border border-line px-2 py-1 text-xs" href={`/api/collections/${r.id}/export?fmt=json`}>Export JSON</a>
            <a className="rounded-lg border border-line px-2 py-1 text-xs" href={`/api/collections/${r.id}/export?fmt=csv`}>CSV</a>
            <Btn small tone="danger" onClick={() => { if (confirm(`Delete collection "${r.name}" and all its indexed content? Original files are not touched.`)) run("delete", async () => { await api.del(`/api/collections/${r.id}`); setSel(null); }); }}>Delete</Btn></div> }]}
          rows={data ?? []} empty="No collections yet. Upload a document in Chat or use Data ingestion." />
      </Card>
      {sel && (
        <Card title={`Documents in "${sel.name}"`}>
          <Table onRow={async (r) => setDoc(await api.get(`/api/documents/${r.id}`))} cols={[{ key: "title", label: "Title" }, { key: "source", label: "Source", render: (r) => <span className="break-all text-dim">{r.source}</span> }, { key: "source_type", label: "Type" }, { key: "chunks", label: "Chunks" },
            { key: "retrieved_at", label: "Added", render: (r) => fmtTime(r.created) }, { key: "status", label: "", render: (r) => <StatusBadge s={r.status} /> },
            { key: "d", label: "", render: (r) => <span onClick={(e) => e.stopPropagation()}><Btn small tone="danger" onClick={() => run("deldoc", () => api.del(`/api/documents/${r.id}`))}>Delete</Btn></span> }]} rows={docs.data ?? []} empty="Empty collection." />
        </Card>
      )}
      {doc && (
        <Card title={`Source inspection: ${doc.title}`} actions={<Btn small onClick={() => setDoc(null)}>Close</Btn>}>
          <p className="mb-2 break-all text-sm text-dim">{doc.source} - sha256 {doc.sha256?.slice(0, 12)} - added {fmtTime(doc.created)}{doc.raw_path ? ` - raw copy: ${doc.raw_path}` : ""}</p>
          {doc.facts?.length > 0 && <div className="mb-3"><h4 className="mb-1 text-sm font-medium">Extracted facts <span className="text-xs font-normal text-dim">(automatic and low-confidence, never trusted by default)</span></h4><Table cols={[{ key: "subject", label: "Subject" }, { key: "predicate", label: "Relation" }, { key: "object", label: "Value" }, { key: "trust", label: "Trust", render: (r) => <StatusBadge s={r.trust} /> }]} rows={doc.facts} /></div>}
          <div className="max-h-96 space-y-2 overflow-y-auto">{doc.chunks.map((c: J) => <div key={c.id} className="rounded-lg border border-line p-2 text-sm"><div className="mb-1 flex gap-2 text-xs text-dim"><span>#{c.idx}</span><span>{c.locator}</span><StatusBadge s={c.status} /></div>{c.text}</div>)}</div>
        </Card>
      )}
    </div>
  );
}

function Search() {
  const [q, setQ] = useState("");
  const [res, setRes] = useState<J | null>(null);
  const [err, setErr] = useState<unknown>(null);
  return (
    <div className="space-y-3">
      <div className="flex gap-2"><Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search your knowledge semantically + by keyword" aria-label="Search" onKeyDown={(e) => { if (e.key === "Enter") document.getElementById("kb-search")?.click(); }} />
        <button id="kb-search" className="rounded-lg bg-accent px-4 text-sm font-medium text-white" onClick={async () => { try { setErr(null); setRes(await api.post("/api/knowledge/search", { query: q, k: 10 })); } catch (e) { setErr(e); } }}>Search</button></div>
      <ErrorBox error={err} />
      {res?.degraded && <Notice>{res.degraded}</Notice>}
      {res && !res.hits.length && <Empty>No relevant results. (Unrelated text is deliberately not returned.)</Empty>}
      {res?.hits.map((h: J) => <Card key={h.chunk_id} title={<span>{h.title} <span className="text-xs font-normal text-dim">{h.locator} - {h.collection}</span></span>} actions={<Badge>score {h.score}</Badge>}><p className="mb-1 break-all text-xs text-dim">{h.source}</p><p className="text-sm">{h.text}</p></Card>)}
    </div>
  );
}

function Memory() {
  const [layer, setLayer] = useState("semantic");
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("active");
  const { data, reload, error } = useApi<J[]>(`/api/memories?layer=${layer}&status=${status}${q ? `&q=${encodeURIComponent(q)}` : ""}`, 0, [layer, q, status]);
  const counts = useApi<J>("/api/memories/counts", 0, [data]);
  const [detail, setDetail] = useState<J | null>(null);
  const [edit, setEdit] = useState<{ id: string; content: string } | null>(null);
  const [add, setAdd] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const run = async (fn: () => Promise<unknown>) => { try { setErr(null); await fn(); reload(); } catch (e) { setErr(e); } };
  return (
    <div className="space-y-3">
      <Tabs value={layer} onChange={setLayer} tabs={LAYERS.map((l) => ({ id: l, label: `${l} (${counts.data?.[l] ?? 0})` }))} />
      <p className="text-sm text-dim">{LAYER_HELP[layer]}. Working memory (the current conversation and task) lives in the Chat and Agent tasks pages.</p>
      <div className="flex flex-wrap gap-2">
        <Input className="!w-64" placeholder="Search memories" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search memories" />
        <Select className="!w-36" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status"><option value="active">Active</option><option value="outdated">Outdated</option><option value="all">All</option></Select>
        <a className="rounded-lg border border-line px-3 py-1.5 text-sm" href="/api/memories/export">Export all</a>
        <Btn onClick={() => run(() => api.post("/api/memories/reindex"))}>Re-index memories</Btn>
      </div>
      <ErrorBox error={error ?? err} />
      <Table onRow={async (r) => setDetail(await api.get(`/api/memories/${r.id}`))} cols={[
        { key: "content", label: "Memory", render: (r) => edit?.id === r.id ? <span onClick={(e) => e.stopPropagation()}><Textarea rows={2} value={edit!.content} onChange={(e) => setEdit({ id: r.id, content: e.target.value })} /><span className="mt-1 flex gap-1"><Btn small tone="primary" onClick={() => run(async () => { await api.patch(`/api/memories/${r.id}`, { content: edit!.content }); setEdit(null); })}>Save</Btn><Btn small onClick={() => setEdit(null)}>Cancel</Btn></span></span> : <span className={r.status === "outdated" ? "text-dim line-through" : ""}>{r.content}</span> },
        { key: "trust", label: "Trust", render: (r) => <StatusBadge s={r.trust} /> }, { key: "kind", label: "Kind" }, { key: "confidence", label: "Conf.", render: (r) => Number(r.confidence).toFixed(2) },
        { key: "updated", label: "Updated", render: (r) => fmtTime(r.updated) },
        { key: "a", label: "", render: (r) => <span className="flex flex-wrap gap-1" onClick={(e) => e.stopPropagation()}>
          <Btn small onClick={() => setEdit({ id: r.id, content: r.content })}>Edit</Btn>
          {r.trust !== "user_verified" && <Btn small onClick={() => run(() => api.post(`/api/memories/${r.id}/verify`))} title="Mark as verified by you">Verify</Btn>}
          {r.status === "active" && <Btn small onClick={() => run(() => api.post(`/api/memories/${r.id}/outdated`))}>Outdated</Btn>}
          <Btn small tone="danger" onClick={() => run(() => api.del(`/api/memories/${r.id}`))}>Delete</Btn></span> }]} rows={data ?? []} empty="No memories here." />
      {detail && (
        <Card title="Provenance" actions={<Btn small onClick={() => setDetail(null)}>Close</Btn>}>
          <dl className="grid grid-cols-[8rem_1fr] gap-y-1 text-sm"><dt className="text-dim">Layer / kind</dt><dd>{detail.layer} / {detail.kind}</dd><dt className="text-dim">Trust</dt><dd><StatusBadge s={detail.trust} /></dd><dt className="text-dim">Added</dt><dd>{fmtTime(detail.created)}</dd><dt className="text-dim">Source</dt><dd>{detail.source_type || "-"} {detail.source_ref}</dd>
            <dt className="text-dim">Used / utility</dt><dd>{detail.uses} times, utility {Number(detail.utility).toFixed(1)} (feedback nudges ranking)</dd>
            {detail.supersedes_memory && <><dt className="text-dim">Replaced</dt><dd className="line-through">{detail.supersedes_memory.content}</dd></>}
            {detail.source?.content && <><dt className="text-dim">Original message</dt><dd className="text-dim">{String(detail.source.content).slice(0, 300)}</dd></>}</dl>
        </Card>
      )}
      <Card title="Add a memory yourself"><div className="flex gap-2"><Textarea rows={2} value={add} onChange={(e) => setAdd(e.target.value)} placeholder="e.g. The staging database is rebuilt every Friday." aria-label="New memory" />
        <Btn tone="primary" disabled={!add.trim()} onClick={() => run(async () => { await api.post("/api/memories", { layer, content: add, kind: layer === "preference" ? "preference" : "fact" }); setAdd(""); })}>Save as {layer}</Btn></div>
        <p className="mt-1 text-xs text-dim">Memories you add are marked as verified by you. Memories the agent proposes stay "unverified" until you verify them.</p></Card>
    </div>
  );
}

export default function Knowledge() {
  const [tab, setTab] = useState("collections");
  const quarantine = useApi<J[]>("/api/quarantine", 0, [tab]);
  return (
    <div className="space-y-4">
      <PageHeader title="Knowledge & memory" sub="Everything NEXUS can retrieve, with sources, trust levels and timestamps." />
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "collections", label: "Collections" }, { id: "search", label: "Search" }, { id: "memory", label: "Memory" }, { id: "quarantine", label: `Quarantine${quarantine.data?.length ? ` (${quarantine.data.length})` : ""}` }]} />
      {tab === "collections" && <Collections />}
      {tab === "search" && <Search />}
      {tab === "memory" && <Memory />}
      {tab === "quarantine" && (quarantine.data?.length ? <div className="space-y-2">{quarantine.data.map((r) => (
        <div key={r.id} className="rounded-lg border border-line p-2 text-sm"><div className="text-xs text-dim">{r.source} - {r.reason}</div><pre className="my-1 max-h-32 overflow-auto">{r.text}</pre>
          <div className="flex gap-2"><Btn small onClick={async () => { await api.post(`/api/quarantine/${r.id}/approve`); quarantine.reload(); }}>Release</Btn><Btn small tone="danger" onClick={async () => { await api.del(`/api/quarantine/${r.id}`); quarantine.reload(); }}>Discard</Btn></div></div>))}</div> : <Empty>Nothing is quarantined.</Empty>)}
    </div>
  );
}
