/**
 * Flat-config ESLint setup. ``next/core-web-vitals`` covers the most
 * important rules; we keep the bar low so reviewers see a single
 * concise config rather than a labyrinth of inheritance.
 */
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat();

export default [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Allow apparently-unused React imports — RSC tooling sometimes
      // wants them anyway and Next.js suppresses noise from these.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
];
