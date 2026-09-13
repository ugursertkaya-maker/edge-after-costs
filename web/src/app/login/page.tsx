"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [checkingSession, setCheckingSession] = useState(true);

  // middleware.ts deliberately does not bounce a cookie-holding visitor off
  // this page, because a dead cookie would then loop forever. The authority on
  // whether a session is real is the server, so ask it — and only leave once
  // it says yes.
  useEffect(() => {
    // A first-time visitor has no cookies at all; probing would guarantee a
    // 401. Probe exactly when there is something to verify.
    if (typeof document === "undefined" || !document.cookie.includes("cb_csrf=")) {
      setCheckingSession(false);
      return;
    }
    let cancelled = false;
    api
      .me()
      .then((res) => {
        if (!cancelled && res.authenticated) router.replace("/");
      })
      .catch(() => {
        // not authenticated, or the API is unreachable — stay put
      })
      .finally(() => {
        if (!cancelled) setCheckingSession(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await api.login(email, password);
      router.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Incorrect email or password.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Too many attempts. Try again in a little while.");
      } else if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Sign-in failed. Check your connection and try again.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login">
      <div className="panel login-card">
        <div className="login-title">NETEDGE</div>
        <div className="login-sub">
          Paper-only research terminal · observation and simulation
        </div>

        {error && (
          <div className="banner banner-err" style={{ marginTop: 16 }} role="alert">
            <span className="banner-tag">Error</span>
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="login-form">
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button
            type="submit"
            className="btn btn-primary btn-block"
            disabled={submitting || checkingSession}
          >
            {submitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {/* These three are properties of the system, not of the session, so
            they stay visible before anyone has authenticated. */}
        <div className="login-foot">
          <div className="rstat">
            <span className="rstat-k">Mode</span>
            <span className="rstat-v is-pos">PAPER</span>
          </div>
          <div className="rstat">
            <span className="rstat-k">Live orders</span>
            <span className="rstat-v is-off">OFF</span>
          </div>
          <div className="rstat">
            <span className="rstat-k">Starting equity</span>
            <span className="rstat-v">$500</span>
          </div>
        </div>
      </div>
    </main>
  );
}
