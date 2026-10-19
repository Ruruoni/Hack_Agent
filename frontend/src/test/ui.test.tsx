import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { Markdown, StatusBadge, Table } from "../ui";
import { ApiError, api, sseStream, fmtBytes } from "../api";
import { Approvals } from "../App";

afterEach(() => vi.restoreAllMocks());

describe("Markdown", () => {
  it("renders code, bold and lists", () => {
    render(<Markdown text={"Hello **world** and `code`\n\n- one\n- two\n\n```\nprint(1)\n```"} />);
    expect(screen.getByText("world").tagName).toBe("STRONG");
    expect(screen.getByText("code").tagName).toBe("CODE");
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("print(1)")).toBeInTheDocument();
  });
  it("never injects HTML from model output", () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)"><script>alert(2)</script>'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>");
  });
  it("turns [S1] into a clickable citation", () => {
    const cite = vi.fn();
    render(<Markdown text="The port is 7421 [S1]." onCite={cite} />);
    fireEvent.click(screen.getByText("[S1]"));
    expect(cite).toHaveBeenCalledWith(1);
  });
});

describe("components", () => {
  it("StatusBadge prettifies statuses", () => {
    render(<StatusBadge s="awaiting_approval" />);
    expect(screen.getByText("awaiting approval")).toBeInTheDocument();
  });
  it("Table shows an empty state", () => {
    render(<Table cols={[{ key: "a", label: "A" }]} rows={[]} empty="Nothing" />);
    expect(screen.getByText("Nothing")).toBeInTheDocument();
  });
  it("formats bytes", () => {
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(null)).toBe("-");
  });
});

describe("api", () => {
  it("turns backend errors into ApiError with hint", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, text: async () => JSON.stringify({ error: { code: "server_unreachable", message: "no LM Studio", hint: "Start Server" } }) }));
    await expect(api.get("/api/x")).rejects.toMatchObject({ code: "server_unreachable", hint: "Start Server", status: 503 });
  });
  it("explains an unreachable backend", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fail")));
    const e = (await api.get("/api/x").catch((x) => x)) as ApiError;
    expect(e.code).toBe("backend_unreachable");
    expect(e.hint).toContain("start.ps1");
  });
  it("parses an SSE stream split across chunks", async () => {
    const enc = new TextEncoder();
    const chunks = ['data: {"type":"delta","te', 'xt":"Hi"}\n\ndata: {"type":"done"}\n\n'];
    let i = 0;
    const body = { getReader: () => ({ read: async () => (i < chunks.length ? { done: false, value: enc.encode(chunks[i++]) } : { done: true, value: undefined }) }) };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, body }));
    const out: any[] = [];
    for await (const ev of sseStream("/api/chat", {})) out.push(ev);
    expect(out).toEqual([{ type: "delta", text: "Hi" }, { type: "done" }]);
  });
});

describe("Approvals", () => {
  const item = { id: "appr_1", tool: "fs_delete", risk: "high", reason: "delete needs approval", args: { args: { path: "/x" } } };
  it("posts the human decision and refreshes", async () => {
    const post = vi.spyOn(api, "post").mockResolvedValue({});
    const done = vi.fn();
    render(<MemoryRouter><Approvals list={[item]} onDone={done} /></MemoryRouter>);
    expect(screen.getByText(/fs_delete/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Approve once"));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(post).toHaveBeenCalledWith("/api/approvals/appr_1/decide", { approve: true });
    fireEvent.click(screen.getByText("Deny"));
    await waitFor(() => expect(post).toHaveBeenLastCalledWith("/api/approvals/appr_1/decide", { approve: false }));
  });
  it("shows API errors", async () => {
    vi.spyOn(api, "post").mockRejectedValue(new ApiError(403, "forbidden", "Approvals can only be decided by a human user."));
    render(<MemoryRouter><Approvals list={[item]} onDone={() => {}} /></MemoryRouter>);
    fireEvent.click(screen.getByText("Approve once"));
    expect(await screen.findByRole("alert")).toHaveTextContent("only be decided by a human");
  });
});
