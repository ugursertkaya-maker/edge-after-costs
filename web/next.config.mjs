/** @type {import('next').NextConfig} */
// The dashboard is served by nginx at https://your-domain.example/cryptobot, so
// every asset and every fetch the browser makes is same-origin under that
// prefix — which is what lets the session cookie work without CORS or a
// second domain. API_ORIGIN is the in-network address of the FastAPI
// container; it is never reachable from a browser.
//
// Read at BUILD time: Next serialises rewrites into routes-manifest.json, so
// setting API_ORIGIN only on the running container has no effect. The
// Dockerfile passes it as a build ARG for that reason.
const API_ORIGIN = process.env.API_ORIGIN || "http://cryptobot-api:8000";

const nextConfig = {
  output: "standalone",
  basePath: "/cryptobot",
  assetPrefix: "/cryptobot",
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        // Matched against the full incoming request path (which already
        // includes the basePath as the browser and nginx see it), so basePath
        // rewriting must be disabled on this entry or Next would prefix it a
        // second time and ask the API for /cryptobot/cryptobot/api/...
        source: "/cryptobot/api/:path*",
        destination: `${API_ORIGIN}/cryptobot/api/:path*`,
        basePath: false,
      },
    ];
  },
};

export default nextConfig;
