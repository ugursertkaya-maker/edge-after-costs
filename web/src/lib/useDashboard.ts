"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type { DashboardPayload } from "./types";

const POLL_MS = 3000;

export interface DashboardState {
  data: DashboardPayload | null;
  /** True only before the first successful read — a refresh never blanks the
      screen, because a trading desk that flashes empty every 3 s is unusable. */
  loading: boolean;
  error: string | null;
  /** Consecutive failed polls. One failure is a blip; several is an outage,
      and the header says so rather than silently showing stale figures. */
  failures: number;
  /** When the last *successful* read landed (epoch ms). */
  lastOkMs: number | null;
  refresh: () => void;
}

/**
 * Polls the dashboard every 3 s. Deliberately a poll and not a socket: the
 * worker already batches its own writes and the payload is the whole screen,
 * so a fixed cadence is both cheaper on a 2-vCPU host and easier to reason
 * about than a stream that can silently stall.
 */
export function useDashboard(ledger: string): DashboardState {
  const [data, setData] = useState<DashboardPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [failures, setFailures] = useState(0);
  const [lastOkMs, setLastOkMs] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  // Keeps an in-flight read from writing state after the ledger changed.
  const ledgerRef = useRef(ledger);
  ledgerRef.current = ledger;

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      const asked = ledgerRef.current;
      try {
        const payload = await api.dashboard(asked);
        if (cancelled || ledgerRef.current !== asked) return;
        setData(payload);
        setError(null);
        setFailures(0);
        setLastOkMs(Date.now());
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          // The session died under us. The middleware owns the redirect on
          // the next navigation; here we only stop pretending to have data.
          setError("Oturum sona erdi. Yeniden giriş yapın.");
        } else if (err instanceof ApiError) {
          setError(err.message);
        } else {
          setError("Sunucuya ulaşılamıyor.");
        }
        setFailures((n) => n + 1);
      } finally {
        if (!cancelled) {
          setLoading(false);
          timer = setTimeout(tick, POLL_MS);
        }
      }
    }

    setLoading((prev) => (data === null ? true : prev));
    tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // `data` is intentionally not a dependency: including it would restart the
    // poll loop on every successful read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ledger, nonce]);

  return { data, loading, error, failures, lastOkMs, refresh };
}

/**
 * A separate, slower liveness probe on `GET /api/health`.
 *
 * It answers a question the dashboard poll cannot: when `/dashboard` starts
 * failing, is the API process down, or is it up and the worker behind it
 * stalled? Those need different responses from the operator, so the rail shows
 * them as two different readings rather than one "error".
 */
export function useApiHealth(): { ok: boolean | null; status: string | null } {
  const [state, setState] = useState<{ ok: boolean | null; status: string | null }>({
    ok: null,
    status: null,
  });

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function tick() {
      try {
        const res = await api.health();
        if (!cancelled) setState({ ok: true, status: res?.status ?? "ok" });
      } catch {
        if (!cancelled) setState({ ok: false, status: null });
      } finally {
        if (!cancelled) timer = setTimeout(tick, 15000);
      }
    }

    tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, []);

  return state;
}
