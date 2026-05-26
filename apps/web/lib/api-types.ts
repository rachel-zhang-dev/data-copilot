/**
 * Placeholder for ``openapi-typescript``-generated types.
 *
 * Running ``pnpm gen:types`` against a live FastAPI dev server
 * (``./scripts/dev.sh api``) overwrites this file with the auto-
 * generated ``paths`` / ``components`` namespaces. Until that runs
 * the front-end uses the hand-curated shapes in ``./types.ts`` — they
 * are kept in lock-step with the backend's Pydantic models and are
 * what every component imports.
 *
 * This indirection means that as soon as the OpenAPI generator runs
 * for the first time, swapping ``./types.ts`` over to re-export from
 * here is a one-line change; until then no consumer breaks.
 */

export {};
