import { useCallback, useEffect, useRef, useState } from "react";

export type J = Record<string, any>;

export class ApiError extends Error {
  code: string;
  hint: string;
  status: number;
  constructor(status: number, code: string, message: string, hint = "") {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
  }
}

async function req<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "backend_unreachable", "Cannot reach the NEXUS backend.", "Start it with scripts\\start.ps1 (or `python -m nexus.cli serve`) and reload this page.");
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? "http_error", e.message ?? (typeof data === "string" ? data : `HTTP ${res.status}`), e.hint ?? "");
  }
  return data as T;
}

export const api = {
  get: <T = any>(p: string) => req<T>("GET", p),
  post: <T = any>(p: string, b: unknown = {}) => req<T>("POST", p, b),
  put: <T = any>(p: string, b: unknown) => req<T>("PUT", p, b),
  patch: <T = any>(p: string, b: unknown) => req<T>("PATCH", p, b),
  del: <T = any>(p: string) => req<T>("DELETE", p),
  async upload(files: File[], collection: string, conversationId?: string): Promise<J> {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    fd.append("collection", collection);
    if (conversationId) fd.append("conversation_id", conversationId);
    const res = await fetch("/api/uploads", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new ApiError(res.status, data?.error?.code ?? "upload", data?.error?.message ?? "upload failed", data?.error?.hint ?? "");
    return data;
  },
};

/** Parse a server-sent-event stream from a POST request (EventSource only supports GET). */
export async function* sseStream(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<J> {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal });
  if (!res.ok || !res.body) {
    const d = await res.json().catch(() => ({}));
    throw new ApiError(res.status, d?.error?.code ?? "http_error", d?.error?.message ?? `HTTP ${res.status}`, d?.error?.hint ?? "");
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i: number;
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const block = buf.slice(0, i);
      buf = buf.slice(i + 2);
      const line = block.split("\n").find((l) => l.startsWith("data: "));
      if (line) {
        try {
          yield JSON.parse(line.slice(6));
        } catch {
          /* ignore malformed frame */
        }
      }
    }
  }
}

export function useApi<T = any>(path: string | null, intervalMs = 0, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api.get<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(e as ApiError);
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    alive.current = true;
    setLoading(true);
    load();
    const t = intervalMs > 0 ? setInterval(load, intervalMs) : undefined;
    return () => {
      alive.current = false;
      if (t) clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, intervalMs, ...deps]);
  return { data, error, loading, reload: load, setData };
}

/** Subscribe to the backend's live event stream (/api/events). */
export function useEvents(onEvent: (e: J) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  useEffect(() => {
    const es = new EventSource("/api/events");
    es.onmessage = (m) => {
      try {
        cb.current(JSON.parse(m.data));
      } catch {
        /* ignore */
      }
    };
    return () => es.close();
  }, []);
}

export const fmtBytes = (n?: number | null) => {
  if (n == null) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
};
export const fmtTime = (t?: number | null) => (t ? new Date(t * 1000).toLocaleString() : "-");
export const fmtDur = (s?: number | null) => (s == null ? "-" : s < 90 ? `${s.toFixed(0)} s` : s < 5400 ? `${(s / 60).toFixed(0)} min` : `${(s / 3600).toFixed(1)} h`);
