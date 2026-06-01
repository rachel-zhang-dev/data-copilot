import { ChatPanel } from "@/components/ChatPanel";

/**
 * The whole product is one page. Server-rendered shell hosts a single
 * Client Component (``ChatPanel``) that owns the streaming chat state.
 *
 * RSC payload is tiny because the shell has zero state and the
 * ``ChatPanel`` bundle is the only "use client" entry in the tree.
 *
 * Phase 2.2 — ``?conversation=<id>&turn=<n>`` deep-link from a
 * dashboard card. We read those from the searchParams Promise (Next.js
 * 15 streaming params API) at the server boundary and pass the
 * already-coerced values into ``ChatPanel`` as props. This avoids
 * forcing the client to call ``useSearchParams``, which would require
 * a Suspense wrapper for SSR.
 */
export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ conversation?: string; turn?: string }>;
}) {
  const sp = await searchParams;
  const initialConversationId = sp.conversation ?? null;
  const turnRaw = sp.turn ? Number.parseInt(sp.turn, 10) : Number.NaN;
  const initialTurnIndex =
    Number.isFinite(turnRaw) && turnRaw > 0 ? turnRaw : null;
  return (
    <main className="bg-(--color-bg)">
      <ChatPanel
        initialConversationId={initialConversationId}
        initialTurnIndex={initialTurnIndex}
      />
    </main>
  );
}
