"use client";

import { useState } from "react";
import { saveConversation, unsaveConversation } from "@/lib/api";

/**
 * Header pin / unpin control for the current conversation.
 *
 * Phase 1.4 / ADR 0019 — zero-friction "C" option: pinning never
 * blocks on a dialog. Title is auto-derived by the backend from the
 * first user question; the user can edit it later inline from the
 * SavedDrawer row.
 *
 * Three visual states:
 *   - no conversation yet → hidden (we can't pin nothing).
 *   - not pinned          → "Save" with hollow star.
 *   - pinned              → "Saved" with filled star + click to unpin.
 *
 * ``onChange`` lets the parent refresh its saved-list. Optimistic UI
 * is intentional — the API call usually returns in <100 ms; if it
 * fails we revert via the catch + alert.
 */
export function PinButton({
  threadId,
  pinned,
  onChange,
}: {
  threadId: string | null;
  pinned: boolean;
  onChange: () => void | Promise<void>;
}) {
  const [busy, setBusy] = useState(false);

  if (!threadId) {
    // No conversation in flight yet; show nothing — the button would
    // be a no-op and add visual clutter.
    return null;
  }

  const handleClick = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (pinned) {
        await unsaveConversation(threadId);
      } else {
        await saveConversation(threadId);
      }
      await onChange();
    } catch (err) {
      // Cheap MVP error surface — Phase 1.5 may add a toast.
      console.error("PinButton failed:", err);
      window.alert(
        pinned ? "Couldn't unpin the conversation." : "Couldn't save the conversation.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={busy}
      className={
        "flex items-center gap-1.5 rounded-md border border-(--color-border) px-2.5 py-1 " +
        "text-xs hover:bg-(--color-bg) disabled:cursor-not-allowed disabled:opacity-50 " +
        (pinned ? "bg-(--color-bg) text-(--color-fg)" : "bg-white text-(--color-muted)")
      }
      aria-label={pinned ? "Unsave conversation" : "Save conversation"}
      title={pinned ? "Unsave this conversation" : "Save this conversation"}
      data-testid="pin-button"
    >
      <span aria-hidden="true" className={pinned ? "text-yellow-500" : ""}>
        {pinned ? "★" : "☆"}
      </span>
      <span>{pinned ? "Saved" : "Save"}</span>
    </button>
  );
}
