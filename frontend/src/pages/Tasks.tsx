import { useState } from "react";
import { J, api, fmtTime, useApi } from "../api";
import { Approvals } from "../App";
import { Badge, Btn, Card, Empty, ErrorBox, Markdown, Notice, PageHeader, Progress, StatusBadge, Table, Textarea } from "../ui";

function TaskDetail({ id, onChange }: { id: string; onChange: () => void }) {
  const t = useApi<J>(`/api/tasks/${id}`, 1200, [id]);
  const ev = useApi<J[]>(`/api/tasks/${id}/events`, 1200, [id]);
  const runs = useApi<J[]>(`/api/tasks/${id}/runs`, 1500, [id]);
  const pend = useApi<J[]>("/api/approvals?status=pending", 1500);
  const [answer, setAnswer] = useState("");
  const [err, setErr] = useState<unknown>(null);
  const [fb, setFb] = useState("");
  const task = t.data;
  if (!task) return <ErrorBox error={t.error} />;
  const act = async (fn: () => Promise<unknown>) => { try { setErr(null); await fn(); t.reload(); onChange(); } catch (e) { setErr(e); } };
  const plan = task.plan?.steps ?? [];
  const active = ["pending", "planning", "running", "awaiting_approval"].includes(task.status);
  const mine = (pend.data ?? []).filter((a) => a.task_id === id);
  const counts = (s: string[]) => (runs.data ?? []).filter((r) => s.includes(r.state)).length;
  return (
    <Card title={<span>{task.objective.slice(0, 90)} <StatusBadge s={task.status} /></span>} actions={<>
      {active && <Btn small tone="danger" onClick={() => act(() => api.post(`/api/tasks/${id}/cancel`))}>Cancel</Btn>}
      {task.status === "failed" && <Btn small tone="primary" onClick={() => act(() => api.post(`/api/tasks/${id}/resume`))}>Resume from checkpoint</Btn>}
    </>}>
      <ErrorBox error={err} />
      <Progress value={task.progress} />
      <p className="mt-1 text-xs text-dim">Step {task.state?.step ?? 0}, {task.state?.tool_calls ?? 0} tool call(s){task.state?.tainted ? " - " : ""}{task.state?.tainted && <Badge tone="warn">untrusted content seen: actions need approval</Badge>} - created {fmtTime(task.created)}</p>
      {mine.length > 0 && <div className="my-3"><Approvals list={mine} onDone={() => { pend.reload(); t.reload(); }} /></div>}
      {task.status === "awaiting_input" && (
        <div className="my-3 space-y-2"><Notice><Markdown text={task.result} /></Notice><Textarea rows={2} value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer" aria-label="Answer" /><Btn tone="primary" disabled={!answer.trim()} onClick={() => act(async () => { await api.post(`/api/tasks/${id}/answer`, { answer }); setAnswer(""); })}>Send answer and continue</Btn></div>
      )}
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div><h4 className="mb-1 text-sm font-medium">Plan</h4>
          {plan.length ? <ol className="list-decimal space-y-1 pl-5 text-sm">{plan.map((s: J, i: number) => <li key={i}>{s.description}{s.tool && <> <code>{s.tool}</code></>}</li>)}</ol> : <p className="text-sm text-dim">Not planned yet.</p>}
          {task.plan?.success_criteria && <p className="mt-1 text-xs text-dim">Success criteria: {task.plan.success_criteria}</p>}
          <h4 className="mb-1 mt-4 text-sm font-medium">Tool calls <span className="font-normal text-dim">(requested {runs.data?.length ?? 0} - completed {counts(["completed", "verified"])} - verified {counts(["verified"])} - blocked {counts(["denied", "approval_denied"])})</span></h4>
          <Table cols={[{ key: "tool", label: "Tool", render: (r) => <code>{r.tool}</code> }, { key: "state", label: "State", render: (r) => <StatusBadge s={r.state} /> }, { key: "v", label: "", render: (r) => (r.verified ? <Badge tone="good" title={r.verification}>verified</Badge> : null) }, { key: "e", label: "Detail", render: (r) => <span className="text-xs text-dim">{r.error || (r.verification && r.verified ? r.verification : "")}</span> }]} rows={runs.data ?? []} empty="No tool calls." />
          <p className="mt-1 text-xs text-dim">"intended" = the model asked; "attempted" = we started it; "completed" = it returned ok; "verified" = we independently checked the effect.</p>
        </div>
        <div><h4 className="mb-1 text-sm font-medium">Log</h4>
          <ul className="max-h-80 space-y-1 overflow-y-auto rounded-lg bg-panel2/50 p-2 text-xs">{(ev.data ?? []).map((e) => <li key={e.id} className={e.level === "warn" || e.level === "error" ? "text-warn" : "text-dim"}><span className="opacity-60">{new Date(e.ts * 1000).toLocaleTimeString()}</span> <b>{e.kind}</b> {e.message}</li>)}</ul></div>
      </div>
      {(task.result || task.error) && task.status !== "awaiting_input" && (
        <div className="mt-4"><h4 className="mb-1 text-sm font-medium">Result</h4>{task.error && <ErrorBox error={{ message: task.error }} />}<div className="rounded-lg border border-line p-3"><Markdown text={task.result || ""} /></div></div>
      )}
      {task.status === "completed" && (
        <div className="mt-3 flex items-center gap-2 text-sm"><span className="text-dim">Did it actually work?</span>
          <Btn small onClick={() => act(async () => { await api.post(`/api/tasks/${id}/outcome`, { success: true }); setFb("Thanks - recorded as a verified success."); })}>Yes</Btn>
          <Btn small onClick={() => act(async () => { await api.post(`/api/tasks/${id}/outcome`, { success: false }); setFb("Recorded as a failure; the agent will weigh this next time."); })}>No</Btn><span className="text-good">{fb}</span></div>
      )}
    </Card>
  );
}

export default function Tasks() {
  const [obj, setObj] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const list = useApi<J[]>("/api/tasks", 2500);
  return (
    <div className="space-y-4">
      <PageHeader title="Agent tasks" sub="Bounded, permission-checked autonomous work with full logs." />
      <Card title="New task">
        <Textarea rows={3} value={obj} onChange={(e) => setObj(e.target.value)} placeholder={"e.g. Analyse the file 'server.log' in my workspace and list the most frequent errors\ne.g. Search my knowledge base for our backup policy and write a one-page report"} aria-label="Task objective" />
        <div className="mt-2 flex items-center gap-3"><Btn tone="primary" disabled={!obj.trim()} onClick={async () => { try { setErr(null); const t = await api.post("/api/tasks", { objective: obj }); setObj(""); setSel(t.id); list.reload(); } catch (e) { setErr(e); } }}>Run task</Btn>
          <span className="text-xs text-dim">Files the agent can touch: the workspace and any folders you allowed in Settings. Consequential steps pause for your approval.</span></div>
        <ErrorBox error={err} />
      </Card>
      <Table onRow={(r) => setSel(r.id)} cols={[{ key: "objective", label: "Objective", render: (r) => r.objective.slice(0, 100) }, { key: "status", label: "Status", render: (r) => <StatusBadge s={r.status} /> }, { key: "p", label: "", render: (r) => <div className="w-24"><Progress value={r.progress} /></div> }, { key: "created", label: "Created", render: (r) => fmtTime(r.created) }]} rows={list.data ?? []} empty="No tasks yet." />
      {sel ? <TaskDetail id={sel} onChange={list.reload} /> : <Empty>Select a task to see its plan, tool calls, approvals and log.</Empty>}
    </div>
  );
}
