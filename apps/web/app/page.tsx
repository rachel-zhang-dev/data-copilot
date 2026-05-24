import { ChatPanel } from "@/components/ChatPanel";

/**
 * The whole product is one page. Server-rendered shell hosts a single
 * Client Component (``ChatPanel``) that owns the streaming chat state.
 *
 * RSC payload is tiny because the shell has zero state and the
 * ``ChatPanel`` bundle is the only "use client" entry in the tree.
 */
export default function HomePage() {
  return (
    <main className="bg-(--color-bg)">
      <ChatPanel />
    </main>
  );
}
