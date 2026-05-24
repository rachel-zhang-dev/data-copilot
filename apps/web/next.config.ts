import type { NextConfig } from "next";

/**
 * Next.js configuration.
 *
 * `API_BASE_URL` is the only knob the front-end needs to talk to the
 * FastAPI backend. We pass it through `env` so Route Handlers (which
 * run on the Node runtime) can read it from `process.env`. The
 * default points at the local dev server started by
 * `./scripts/dev.sh api`.
 */
const nextConfig: NextConfig = {
  env: {
    API_BASE_URL: process.env.API_BASE_URL ?? "http://localhost:8000",
  },
  // SSE responses must not be buffered. Disabling Next's gzip on the
  // streaming Route Handler is done per-handler via response headers;
  // no global config needed.
  reactStrictMode: true,
};

export default nextConfig;
