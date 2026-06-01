import path from "path";
import { fileURLToPath } from "url";
import type { NextConfig } from "next";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
 *
 * ``outputFileTracingRoot`` is pinned to the repo root so Next.js's
 * file tracer doesn't pick a parent ancestor (the warning it emits
 * when ``$HOME/pnpm-lock.yaml`` exists alongside the workspace
 * lockfile would otherwise nag on every build). The root is computed
 * once from this file's path; no env / runtime input.
 *
 * ``webpack(config)`` declares ``canvas`` as an aliased false so
 * the ``vega-canvas`` Node-only sub-module that bundle-traces in
 * from ``vega-embed`` doesn't fail to resolve. We render charts as
 * SVG (``renderer: "svg"`` in ChartRenderer) so the canvas backend
 * is never actually used — silencing the warning here is purely
 * cosmetic, not a behaviour change.
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
  outputFileTracingRoot: path.resolve(__dirname, "..", ".."),
  // ``webpack``'s ``config`` parameter type comes from ``NextConfig``;
  // we don't depend on ``webpack`` directly so we don't pull it in as
  // a dev-dep just for the type — Next.js infers it.
  webpack: (config) => {
    config.resolve = config.resolve ?? {};
    config.resolve.fallback = {
      ...(config.resolve.fallback ?? {}),
      canvas: false,
    };
    return config;
  },
};

export default nextConfig;
