import { useEffect, useRef, useState } from "react";
import { ApiError, J, api, sseStream, useApi } from "../api";
import { Approvals } from "../App";
import { Badge, Btn, Card, Check, ErrorBox, Markdown, Notice, Progress, Select, StatusBadge, Textarea } from "../ui";

type Msg = { id?: string; role: string; content: string; meta?: J; streaming?: boolean };

function Sources({ sources, cited, focus }: { sources: J[]; cited: number[]; focus: number | null }) {
  if (!sources?.length) return null;
  return (
    <details className="mt-2 rounded-lg border border-line bg-panel2/40 p-2 text-xs" open={focus != null}>
      <summary className="cursor-pointer text-dim">{sources.length} source{sources.length > 1 ? "s" : ""} retrieved ({cited.length} cited)</summary>
      <ol className="mt-2 space-y-2">
        {sources.map((s) => (
          <li key={s.n} className={`rounded border p-2 ${focus === s.n ? "border-accent" : "border-line"}`}>
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={cited.includes(s.n) ? "accent" : "neutral"}>S{s.n}</Badge>
              <b>{s.title}</b>
              {s.locator && <span className="text-dim">{s.locator}</span>}
              {s.type === "memory" && <StatusBadge s={s.trust} />}
              {s.collection && <span className="text-dim">in {s.collection}</span>}
            </div>
            <div className="mt-0.5 break-all text-dim">{s.source}{s.retrieved_at ? ` - retrieved ${new Date(s.retrieved_at * 1000).toLocaleDateString()}` : ""}</div>
            <div className="mt-1">{s.snippet}</div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function Feedback({ cid, msg }: { cid: string; msg: Msg }) {
  const [rating, setRating] = useState(0);
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [ask, setAsk] = useState<J | null>(null);
  const [scope, setScope] = useState("");
  const [result, setResult] = useState<string>("");
  const [err, setErr] = useState<unknown>(null);
  const send = async (body: J) => {
    try {
      setErr(null);
      const r = await api.post("/api/feedback", { conversation_id: cid, message_id: msg.id, ...body });
      if (r.needs_scope) { setAsk(r); setScope(r.suggested); return; }
      setAsk(null);
      setResult(r.saved ? `Saved as ${r.saved.scope.replace("_", "-")} (${r.saved.layer} memory, ${r.saved.trust.replace("_", " ")}).${r.regression_case ? " A regression test was created." : ""}` : "Thanks - feedback recorded.");
      setOpen(false);
    } catch (e) { setErr(e); }
  };
  return (
    <div className="mt-2 text-xs">
      <div className="flex items-center gap-1">
        <button aria-label="Helpful" className={`rounded border px-1.5 py-0.5 ${rating === 1 ? "border-good text-good" : "border-line text-dim"}`} onClick={() => { setRating(1); send({ rating: 1 }); }}>Helpful</button>
        <button aria-label="Not helpful" className={`rounded border px-1.5 py-0.5 ${rating === -1 ? "border-bad text-bad" : "border-line text-dim"}`} onClick={() => { setRating(-1); send({ rating: -1 }); setOpen(true); }}>Not helpful</button>
        <button className="rounded border border-line px-1.5 py-0.5 text-dim" onClick={() => setOpen(!open)}>Correct this</button>
        {result && <span className="ml-2 text-good">{result}</span>}
      </div>
      {open && (
        <div className="mt-2 space-y-2 rounded-lg border border-line p-2">
          <Textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder='What should it have said or done? e.g. "Always show sizes in GiB"' />
          {ask && (
            <div className="space-y-1">
              <Notice>{ask.question}</Notice>
              <Select value={scope} onChange={(e) => setScope(e.target.value)} aria-label="Correction scope">
                <option value="one_time">One-time correction (only this conversation)</option>
                <option value="preference">General preference from now on</option>
                <option value="fact">Verified fact (replaces the old claim)</option>
                <option value="hypothesis">Uncertain hypothesis (stored as unverified)</option>
              </Select>
            </div>
          )}
          <Btn small tone="primary" disabled={!text.trim()} onClick={() => send({ correction: text, ...(ask ? { scope } : {}) })}>{ask ? "Save with this scope" : "Save correction"}</Btn>
          <ErrorBox error={err} />
        </div>
      )}
    </div>
  );
}

export default function Chat() {
  const convs = useApi<J[]>("/api/conversations", 0);
  const cols = useApi<J[]>("/api/collections");
  const pending = useApi<J[]>("/api/approvals?status=pending", 2000);
  const [cid, setCid] = useState<string | null>(null);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [mode, setMode] = useState<"chat" | "agent">("chat");
  const [useRetrieval, setUseRetrieval] = useState(true);
  const [selCols, setSelCols] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [task, setTask] = useState<J | null>(null);
  const [taskEvents, setTaskEvents] = useState<J[]>([]);
  const [focus, setFocus] = useState<number | null>(null);
  const [style, setStyle] = useState({ explain_mode: "", response_length: "" });
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, taskEvents]);

  const open = async (id: string) => {
    const c = await api.get(`/api/conversations/${id}`);
    setCid(id);
    setMsgs(c.messages.map((m: J) => ({ id: m.id, role: m.role, content: m.content, meta: m.meta })));
    setTask(null); setTaskEvents([]); setNotice("");
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput(""); setErr(null); setNotice(""); setBusy(true); setTask(null); setTaskEvents([]);
    setMsgs((m) => [...m, { role: "user", content: text }, { role: "assistant", content: "", streaming: true, meta: {} }]);
    const ctrl = (abort.current = new AbortController());
    const overrides: J = {};
    if (style.explain_mode) overrides.explain_mode = style.explain_mode;
    if (style.response_length) overrides.response_length = style.response_length;
    try {
      for await (const ev of sseStream("/api/chat", { conversation_id: cid, message: text, mode, use_retrieval: useRetrieval, collections: selCols, overrides }, ctrl.signal)) {
        if (ev.type === "meta") { if (!cid) setCid(ev.conversation_id); }
        else if (ev.type === "retrieval") setMsgs((m) => { const c = [...m]; c[c.length - 1] = { ...c[c.length - 1], meta: { ...c[c.length - 1].meta, sources: ev.sources, degraded: ev.degraded } }; return c; });
        else if (ev.type === "notice") setNotice(ev.message);
        else if (ev.type === "delta") setMsgs((m) => { const c = [...m]; const l = c[c.length - 1]; c[c.length - 1] = { ...l, content: l.content + ev.text }; return c; });
        else if (ev.type === "task") setTask(ev.task);
        else if (ev.type === "task_event") setTaskEvents((t) => [...t, ev.event]);
        else if (ev.type === "done") setMsgs((m) => { const c = [...m]; c[c.length - 1] = { id: ev.message.id, role: "assistant", content: ev.message.content, meta: ev.message.meta }; return c; });
        else if (ev.type === "title") convs.reload();
        else if (ev.type === "error") { setErr(new ApiError(0, ev.error.code, ev.error.message, ev.error.hint)); setMsgs((m) => m.filter((x, i) => !(i === m.length - 1 && x.streaming && !x.content))); }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") setErr(e);
    } finally {
      setBusy(false); abort.current = null; convs.reload();
    }
  };

  const stop = async () => {
    abort.current?.abort();
    if (cid) await api.post(`/api/chat/${cid}/stop`).catch(() => {});
    if (task?.id) await api.post(`/api/tasks/${task.id}/cancel`).catch(() => {});
    setBusy(false);
  };

  const upload = async (files: File[]) => {
    if (!files.length) return;
    try {
      setErr(null);
      let id = cid;
      if (!id) { id = (await api.post("/api/conversations", {})).id; setCid(id); }
      const r = await api.upload(files, "uploads", id!);
      const c = r.job.counts;
      setNotice(`Uploaded ${files.length} file(s): ${c.chunks_indexed} chunks indexed${c.rejected ? `, ${c.rejected} rejected` : ""}${c.quarantined ? `, ${c.quarantined} quarantined` : ""}. You can now ask about "${r.documents[0]?.title ?? "the document"}".`);
      cols.reload(); convs.reload();
    } catch (e) { setErr(e); }
  };

  const myPending = (pending.data ?? []).filter((a) => !task || a.task_id === task.id);

  return (
    <div className="flex h-[calc(100vh-11rem)] gap-4 md:h-[calc(100vh-7.5rem)]">
      <aside className="hidden w-60 shrink-0 flex-col gap-2 md:flex">
        <Btn tone="primary" onClick={() => { setCid(null); setMsgs([]); setTask(null); setTaskEvents([]); }}>New conversation</Btn>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto">
          {convs.data?.map((c) => (
            <div key={c.id} className={`group flex items-center justify-between rounded-lg px-2 py-1.5 text-sm ${c.id === cid ? "bg-accent/15" : "hover:bg-panel2"}`}>
              <button className="min-w-0 flex-1 truncate text-left" onClick={() => open(c.id)} title={c.summary || c.title}>{c.title}</button>
              <button aria-label="Delete conversation" className="hidden text-dim hover:text-bad group-hover:block" onClick={async () => { await api.del(`/api/conversations/${c.id}`); if (c.id === cid) { setCid(null); setMsgs([]); } convs.reload(); }}>x</button>
            </div>
          ))}
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="mb-2 flex flex-wrap items-center gap-3 text-sm">
          <div className="inline-flex overflow-hidden rounded-lg border border-line" role="group" aria-label="Mode">
            <button className={`px-3 py-1 ${mode === "chat" ? "bg-accent text-white" : "bg-panel"}`} onClick={() => setMode("chat")}>Chat</button>
            <button className={`px-3 py-1 ${mode === "agent" ? "bg-accent text-white" : "bg-panel"}`} onClick={() => setMode("agent")} title="Runs a bounded task with tools; consequential steps need your approval">Agent</button>
          </div>
          <Check checked={useRetrieval} onChange={setUseRetrieval} label="Use my knowledge" />
          <Select className="!w-40" aria-label="Collections" value={selCols[0] ?? ""} onChange={(e) => setSelCols(e.target.value ? [e.target.value] : [])}>
            <option value="">All collections</option>
            {cols.data?.map((c) => <option key={c.id} value={c.id}>{c.name} ({c.chunks})</option>)}
          </Select>
          <Select className="!w-36" aria-label="Explanation level" value={style.explain_mode} onChange={(e) => setStyle({ ...style, explain_mode: e.target.value })}>
            <option value="">Default level</option><option value="technical">Technical</option><option value="beginner">Beginner-friendly</option>
          </Select>
          <Select className="!w-32" aria-label="Response length" value={style.response_length} onChange={(e) => setStyle({ ...style, response_length: e.target.value })}>
            <option value="">Default length</option><option value="short">Short</option><option value="medium">Medium</option><option value="long">Long</option>
          </Select>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto rounded-xl border border-line bg-panel p-4" aria-live="polite">
          {msgs.length === 0 && <div className="py-16 text-center text-dim"><p className="text-lg">Ask anything, or upload a document first.</p><p className="text-sm">Answers that use your documents show numbered sources you can inspect.</p></div>}
          {msgs.map((m, i) => (
            <div key={i} className={`flex ${m.role === "user" ? "justify-end" : ""}`}>
              <div className={`max-w-[85%] rounded-2xl px-4 py-2.5 ${m.role === "user" ? "bg-accent/20" : "bg-panel2"}`}>
                {m.content ? <Markdown text={m.content} onCite={(n) => setFocus(n)} /> : m.streaming ? <span className="live text-dim">thinking...</span> : null}
                {m.role === "assistant" && m.meta?.degraded && <p className="mt-1 text-xs text-warn">{m.meta.degraded}</p>}
                {m.role === "assistant" && <Sources sources={m.meta?.sources ?? []} cited={m.meta?.cited ?? []} focus={focus} />}
                {m.role === "assistant" && m.meta?.task_id && <p className="mt-1 text-xs text-dim">Task <code>{m.meta.task_id}</code> - {m.meta.task_status}</p>}
                {m.role === "assistant" && m.id && !m.streaming && cid && <Feedback cid={cid} msg={m} />}
              </div>
            </div>
          ))}
          {task && (
            <Card title={<span>Task progress <StatusBadge s={task.status} /></span>}>
              <Progress value={task.progress ?? 0} />
              <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto text-xs">
                {taskEvents.filter((e) => ["plan", "tool_intent", "tool_result", "approval", "injection", "clarification", "completed", "failed", "cancelled"].includes(e.kind)).map((e, i) => (
                  <li key={i} className={e.level === "warn" || e.level === "error" ? "text-warn" : "text-dim"}><b>{e.kind.replace("_", " ")}:</b> {e.message}</li>
                ))}
              </ul>
              {myPending.length > 0 && <div className="mt-3"><Approvals list={myPending} onDone={pending.reload} /></div>}
              {task.status === "awaiting_input" && <p className="mt-2 text-sm text-warn">The agent asked a question - answer it in Agent tasks.</p>}
            </Card>
          )}
          <div ref={bottom} />
        </div>
        {notice && <div className="mt-2"><Notice tone="accent">{notice}</Notice></div>}
        <div className="mt-2"><ErrorBox error={err} /></div>
        <div className="mt-2 flex items-end gap-2">
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => { const picked = Array.from(e.target.files ?? []); e.target.value = ""; upload(picked); }} />
          <Btn onClick={() => fileRef.current?.click()} title="Upload PDF, DOCX, TXT, MD, HTML, CSV, JSON, XLSX, code...">Upload</Btn>
          <Textarea rows={2} value={input} placeholder={mode === "agent" ? "Describe a task, e.g. 'Analyse the log file in my workspace and summarise the errors'" : "Message NEXUS... (Enter to send, Shift+Enter for a new line)"}
            onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} aria-label="Message" />
          {busy ? <Btn tone="danger" onClick={stop}>Stop</Btn> : <Btn tone="primary" onClick={send} disabled={!input.trim()}>Send</Btn>}
        </div>
      </div>
    </div>
  );
}
