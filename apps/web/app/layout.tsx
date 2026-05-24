import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Data Copilot",
  description:
    "Enterprise-grade Text-to-SQL agent. Ask a question, get the SQL, the rows, " +
    "a chart, and an insight summary — with explicit confirmation on expensive queries.",
};

/**
 * Root layout. Intentionally minimal — the whole app is one page so
 * routing is RSC + a single Client Component. This keeps the bundle
 * small and the screenshot story coherent.
 */
export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
