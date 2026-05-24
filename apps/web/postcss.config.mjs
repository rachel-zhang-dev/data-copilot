/**
 * Tailwind CSS v4 ships its config inside the CSS file (see
 * ``app/globals.css``); the only PostCSS plugin needed is
 * ``@tailwindcss/postcss``. No autoprefixer either — Tailwind v4
 * bundles its own vendor-prefix step.
 */
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
