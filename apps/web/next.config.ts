import type { NextConfig } from "next";

/**
 * Next.js configuration.
 *
 * `API_BASE_URL` is read at runtime by the server-only Route Handlers
 * (`app/api/**`). Do NOT declare it under `env: {}` here — that block
 * is Next.js's build-time inliner (it behaves like webpack's
 * DefinePlugin), which would freeze the value to whatever `process.env`
 * held during `next build`. Since the web container is built before
 * any runtime env vars are wired up, the inlined value would be the
 * fallback `http://localhost:8000`, defeating the docker-compose
 * service-name override (`http://api:8000`) at runtime.
 *
 * Plain `process.env.X` reads inside server code are evaluated at
 * runtime, so leaving `env` unset is the correct choice.
 */
const nextConfig: NextConfig = {
  // SSE responses must not be buffered. Disabling Next's gzip on the
  // streaming Route Handler is done per-handler via response headers;
  // no global config needed.
  reactStrictMode: true,
  // ``output: "standalone"`` produces a self-contained ``server.js``
  // + minimal ``node_modules`` under ``.next/standalone/``. The
  // production Dockerfile copies that out so the runtime image
  // doesn't need pnpm or the full dev dependency tree.
  output: "standalone",
};

export default nextConfig;
