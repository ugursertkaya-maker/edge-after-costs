import type { AuthMeResponse, DashboardPayload, HealthResponse } from "./types";

// Every request is same-origin under the app's own basePath and is proxied to
// the FastAPI container by the rewrite in next.config.mjs. That is what makes
// the session cookie work without CORS. Note that with `basePath` set, Next
// does NOT rewrite plain fetch() URLs, so the prefix is written out here.
const API_BASE = "/edge-after-costs/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=").slice(1).join("=")) : null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);

  if (method !== "GET" && method !== "HEAD") {
    const csrf = readCookie("cb_csrf");
    if (csrf) headers.set("X-CSRF-Token", csrf);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    method,
    headers,
    credentials: "include",
    cache: "no-store",
  });

  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.message === "string") message = body.message;
      else if (typeof body?.detail === "string") message = body.detail;
    } catch {
      // not JSON — keep the default message
    }
    throw new ApiError(res.status, message);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => request<HealthResponse>("/health"),

  login: (email: string, password: string) =>
    request<{ ok: boolean }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  me: () => request<AuthMeResponse>("/auth/me"),

  /** The whole screen in one read — see lib/types.ts for the shape. */
  dashboard: (ledger = "base") =>
    request<DashboardPayload>(`/dashboard?ledger=${encodeURIComponent(ledger)}`),
};
