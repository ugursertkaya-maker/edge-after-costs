import { NextRequest, NextResponse } from "next/server";

/**
 * Gates every app route on the presence of the `cb_session` cookie.
 *
 * It deliberately does NOT redirect *away* from /login when a cookie exists:
 * a stale or server-side-expired session would then bounce the operator into
 * the dashboard, get a 401, and bounce back — a loop. The login page verifies
 * the session itself via `GET /auth/me` and only then navigates away.
 *
 * With `basePath: "/cryptobot"` Next strips the prefix from
 * `request.nextUrl.pathname` before middleware sees it, so the paths below are
 * basePath-relative. The prefix is stripped defensively anyway in case that
 * behaviour changes between Next versions.
 */
export function middleware(request: NextRequest) {
  let pathname = request.nextUrl.pathname;
  if (pathname.startsWith("/cryptobot")) {
    pathname = pathname.slice("/cryptobot".length) || "/";
  }

  if (pathname === "/login" || pathname.startsWith("/login/")) {
    return NextResponse.next();
  }

  const hasSession = Boolean(request.cookies.get("cb_session")?.value);

  if (!hasSession) {
    const loginUrl = request.nextUrl.clone();
    // basePath is re-applied by Next when the URL is built; writing
    // "/cryptobot/login" here would redirect to /cryptobot/cryptobot/login.
    loginUrl.pathname = "/login";
    loginUrl.search = "";
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // An explicit list rather than a negative-lookahead regex: with basePath
  // set, the regex form silently fails to match the root route, which renders
  // the dashboard without a session and lets the API answer 401 instead.
  matcher: ["/"],
};
