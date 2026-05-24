# syntax=docker/dockerfile:1.7
#
# Frontend image — Node 22 alpine + Next.js standalone output.
#
# Three stages so the runtime image only carries the static assets +
# the standalone server bundle. Final layer lands < 150 MB.
#
#   deps     — pnpm install once with a writable store.
#   builder  — pnpm build with NEXT_TELEMETRY_DISABLED to skip the
#              first-build telemetry blocker.
#   runtime  — node + the standalone output. No pnpm, no toolchain.

FROM node:22-alpine AS deps

WORKDIR /app
RUN corepack enable

COPY apps/web/package.json apps/web/pnpm-lock.yaml* ./
# ``pnpm install --frozen-lockfile`` fails fast if the committed lock
# is out of date, which is what we want in CI / build.
RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile


FROM node:22-alpine AS builder

ENV NEXT_TELEMETRY_DISABLED=1

WORKDIR /app
RUN corepack enable

COPY --from=deps /app/node_modules ./node_modules
COPY apps/web/ ./

RUN pnpm build


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# Non-root for the same reason as the API image.
RUN addgroup --system --gid 10001 app && \
    adduser --system --uid 10001 --ingroup app app

# Next.js standalone output ships server.js + minimal node_modules at
# the project root, plus public/ + .next/static for the static
# assets. Copy in the order that maximises cache friendliness.
COPY --from=builder --chown=app:app /app/public ./public
COPY --from=builder --chown=app:app /app/.next/standalone ./
COPY --from=builder --chown=app:app /app/.next/static ./.next/static

USER app
EXPOSE 3000

HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD wget -qO- http://localhost:3000/api/health >/dev/null || exit 1

CMD ["node", "server.js"]
