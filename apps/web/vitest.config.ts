import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Vitest configuration for component + lib unit tests.
 *
 * We run in jsdom because the components touch React state /
 * eventSource APIs that node's default env doesn't provide. The
 * setup file imports ``@testing-library/jest-dom`` matchers and
 * stubs out ``EventSource`` (which jsdom does not implement).
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./vitest.setup.ts"],
    globals: true,
    include: ["__tests__/**/*.test.ts", "__tests__/**/*.test.tsx"],
  },
  resolve: {
    alias: {
      "@": new URL(".", import.meta.url).pathname,
    },
  },
});
