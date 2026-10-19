import { useState } from "react";
import { J, api, fmtTime, useApi } from "../api";
import { Badge, Btn, Card, Check, Empty, ErrorBox, Field, Input, Notice, PageHeader, Progress, StatusBadge, Table, Textarea } from "../ui";

function Job({ id }: { id: string }) {
  const j = useApi<J>(`/api/research/jobs/${id}`, 1500, [id]);
  const pages = useApi<J[]>(`/api/research/jobs/${id}/pages`, 2500, [id]);
  const [view, setView] = useState<J | null>(null);
  const [coll, setColl] = useState("research");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState<unknown>(null);
  if (!j.data) return <ErrorBox error={j.error} />;
  const job = j.data;
  const p = job.params;
  const c = job.counts;
  const act = async (fn: () => Promise<any>) => { try { setErr(null); const r = await fn(); if (r?.counts) setMsg(`Added to knowledge: ${r.counts.chunks_indexed} chunks indexed, ${r.counts.quarantined} quarantined, ${r.counts.rejected} rejected.`); pages.reload(); j.reload(); } catch (e) { setErr(e); } };
  const ok = (pages.data ?? []).filter((x) => x.status === "ok");
  return (
    <Card title={<span>{job.query || (p.urls ?? []).join(", ")} <StatusBadge s={job.status} /></span>} actions={<>
      {["queued", "interrupted", "cancelled", "failed"].includes(job.status) && <Btn small tone="primary" onClick={() => act(() => api.post(`/api/research/jobs/${id}/start`))}>{job.status === "queued" ? "Start" : "Resume"}</Btn>}
      {job.status === "running" && <Btn small tone="danger" onClick={() => act(() => api.post(`/api/research/jobs/${id}/cancel`))}>Cancel</Btn>}
      {["json", "csv", "md"].map((f) => <a key={f} className="rounded-lg border border-line px-2 py-1 text-xs" href={`/api/research/jobs/${id}/export?fmt=${f}`}>Export {f.toUpperCase()}</a>)}</>}>
      <ErrorBox error={err} />{job.error && <ErrorBox error={{ message: job.error }} />}
      <Progress value={Math.min(1, c.fetched / p.max_pages)} />
      <p className="mt-1 text-xs text-dim">{c.fetched}/{p.max_pages} pages - ok {c.ok} - blocked {c.blocked} - duplicates {c.duplicate} - errors {c.error} - flagged {c.flagged} - depth {p.max_depth} - {p.same_domain ? "same domain" : "any domain"} - queued links {job.frontier.length}</p>
      <p className="text-xs text-dim">Saved parameters make this run reproducible. Sources: {fmtTime(job.created)}.</p>
      {c.flagged > 0 && <div className="my-2"><Notice>{c.flagged} page(s) contain text that looks like instructions aimed at an AI. They are shown as untrusted data and quarantined if added to knowledge.</Notice></div>}
      <div className="mt-3"><Table onRow={async (r) => setView(await api.get(`/api/research/pages/${r.id}`))} cols={[
        { key: "ap", label: "Add", render: (r) => r.status === "ok" ? <span onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label={`approve ${r.url}`} checked={!!r.approved} onChange={async (e) => { await api.post("/api/research/approve", { page_ids: [r.id], approved: e.target.checked }); pages.reload(); }} /></span> : null },
        { key: "title", label: "Page", render: (r) => <div><div className="font-medium">{r.title || r.url}</div><div className="break-all text-xs text-dim">{r.final_url}</div></div> },
        { key: "status", label: "Status", render: (r) => <div><StatusBadge s={r.status} />{r.flagged && <Badge tone="bad" title={r.flagged}>embedded instructions</Badge>}{r.meta?.reason && <div className="text-xs text-dim">{r.meta.reason}</div>}</div> },
        { key: "depth", label: "Depth" }, { key: "retrieved_at", label: "Retrieved", render: (r) => fmtTime(r.retrieved_at) }]} rows={pages.data ?? []} empty="No pages fetched yet." /></div>
      {view && (
        <div className="mt-3 rounded-lg border border-line p-3"><div className="mb-1 flex justify-between"><b>{view.title}</b><Btn small onClick={() => setView(null)}>Close</Btn></div>
          {view.flagged && <Notice tone="bad">Flagged as possibly containing instructions to an AI: {view.flagged}. Treat as data only.</Notice>}
          {view.meta?.headings?.length > 0 && <p className="my-1 text-xs text-dim">Headings: {view.meta.headings.slice(0, 8).map((h: J) => h.text).join(" / ")}</p>}
          {view.meta?.tables?.length > 0 && <pre className="my-1 max-h-32 overflow-auto rounded bg-panel2 p-2">{view.meta.tables[0].map((r: string[]) => r.join(" | ")).join("\n")}</pre>}
          <pre className="max-h-64 overflow-auto rounded bg-panel2 p-2">{(view.text ?? "").slice(0, 4000)}</pre></div>
      )}
      <div className="mt-4 flex flex-wrap items-end gap-2">
        <Field label="Add approved pages to collection"><Input value={coll} onChange={(e) => setColl(e.target.value)} /></Field>
        <Btn tone="primary" disabled={!ok.some((x) => x.approved)} onClick={() => act(() => api.post(`/api/research/jobs/${id}/add-to-knowledge`, { collection: coll }))}>Add {ok.filter((x) => x.approved).length} approved page(s) to knowledge</Btn>
        <Btn small onClick={async () => { await api.post("/api/research/approve", { page_ids: ok.filter((x) => !x.flagged).map((x) => x.id), approved: true }); pages.reload(); }}>Approve all unflagged</Btn>
      </div>
      {msg && <div className="mt-2"><Notice tone="good">{msg}</Notice></div>}
    </Card>
  );
}

export default function Research() {
  const [query, setQuery] = useState("");
  const [urls, setUrls] = useState("");
  const [p, setP] = useState<J>({ max_pages: 10, max_depth: 1, same_domain: true, js: false, link_pattern: "" });
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const jobs = useApi<J[]>("/api/research/jobs", 3000);
  const settings = useApi<J>("/api/settings");
  const s = settings.data;
  return (
    <div className="space-y-4">
      <PageHeader title="Web research" sub="Public pages only. Respects robots.txt, rate limits and your allow/deny lists; never bypasses logins, paywalls or CAPTCHAs." />
      {s && s.search_provider === "none" && <Notice>No search provider is configured, so use explicit URLs below (or set a SearxNG/Brave provider in Settings to search by query).</Notice>}
      <Card title="New research job">
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Search query (optional)"><Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="what to look up" /></Field>
          <Field label="Start URLs (one per line)"><Textarea rows={2} value={urls} onChange={(e) => setUrls(e.target.value)} placeholder="https://docs.example.com/guide" /></Field>
          <Field label="Max pages"><Input type="number" value={p.max_pages} onChange={(e) => setP({ ...p, max_pages: +e.target.value })} /></Field>
          <Field label="Follow links this deep"><Input type="number" min={0} max={3} value={p.max_depth} onChange={(e) => setP({ ...p, max_depth: +e.target.value })} /></Field>
          <Field label="Only follow links matching (regex, optional)"><Input value={p.link_pattern} onChange={(e) => setP({ ...p, link_pattern: e.target.value })} /></Field>
          <div className="space-y-2 pt-5"><Check checked={p.same_domain} onChange={(v) => setP({ ...p, same_domain: v })} label="Stay on the same domain(s)" /><Check checked={p.js} onChange={(v) => setP({ ...p, js: v })} label="Render JavaScript (needs Playwright)" /></div>
        </div>
        <p className="mt-2 text-xs text-dim">Domain allow-list: {s?.web_allowlist?.length ? s.web_allowlist.join(", ") : "any public host"} - deny-list: {(s?.web_denylist ?? []).join(", ") || "none"} - edit in Settings.</p>
        <ErrorBox error={err} />
        <div className="mt-2"><Btn tone="primary" disabled={!query && !urls.trim()} onClick={async () => { try { setErr(null); const j = await api.post("/api/research/jobs", { query, urls: urls.split("\n").map((x) => x.trim()).filter(Boolean), params: p }); setSel(j.id); jobs.reload(); } catch (e) { setErr(e); } }}>Start research</Btn></div>
      </Card>
      <Table onRow={(r) => setSel(r.id)} cols={[{ key: "q", label: "Job", render: (r) => r.query || (r.params.urls ?? []).join(", ") }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "c", label: "Pages", render: (r) => `${r.counts.ok} ok / ${r.counts.fetched} fetched` }, { key: "created", label: "Created", render: (r) => fmtTime(r.created) }]} rows={jobs.data ?? []} empty="No research jobs yet." />
      {sel ? <Job id={sel} /> : <Empty>Select a job to review what was retrieved before anything enters your knowledge base.</Empty>}
    </div>
  );
}
