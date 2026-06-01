"use client";

import { useState } from "react";
import type { SavedConversation } from "@/lib/api";
import { saveConversation } from "@/lib/api";

/**
 * Left sidebar listing pinned conversations + a "New chat" button.
 *
 * Phase 1.4 / ADR 0019. Same visual pattern as Claude / ChatGPT:
 * persistent rail when expanded, icon-only stub when collapsed.
 * Collapsed state is persisted to localStorage so it survives
 * reloads (the drawer isn't intrusive enough to deserve a server-
 * side preference).
 *
 * Inline title-edit:
 *   - click a row's title → goes to edit mode on that row;
 *   - Enter or blur saves;
 *   - Escape cancels.
 *
 * The row title is the entire clickable area when NOT editing — so
 * a click on whitespace loads the conversation. We block bubbling
 * inside the title's text node when editing so click-to-save and
 * click-to-load don't compete.
 */
export function SavedDrawer({
  items,
  currentThreadId,
  collapsed,
  onToggleCollapsed,
  onLoadConversation,
  onNewChat,
  onRefresh,
}: {
  items: SavedConversation[];
  currentThreadId: string | null;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onLoadConversation: (threadId: string) => void;
  onNewChat: () => void;
  onRefresh: () => void | Promise<void>;
}) {
  if (collapsed) {
    return (
      <aside
        className="flex h-svh w-12 flex-col items-center gap-2 border-r border-(--color-border) bg-white py-3"
        data-testid="saved-drawer-collapsed"
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          className="rounded-md p-1.5 text-(--color-muted) hover:bg-(--color-bg)"
          aria-label="Expand sidebar"
          title="Expand sidebar"
        >
          {/* hamburger icon */}
          <span aria-hidden="true">≡</span>
        </button>
        <button
          type="button"
          onClick={onNewChat}
          className="rounded-md p-1.5 text-(--color-muted) hover:bg-(--color-bg)"
          aria-label="New chat"
          title="New chat"
        >
          <span aria-hidden="true">+</span>
        </button>
      </aside>
    );
  }

  return (
    <aside
      className="flex h-svh w-64 flex-col border-r border-(--color-border) bg-white"
      data-testid="saved-drawer"
    >
      <div className="flex items-center justify-between gap-2 border-b border-(--color-border) px-3 py-2">
        <button
          type="button"
          onClick={onNewChat}
          className="flex-1 rounded-md border border-(--color-border) bg-white px-3 py-1.5 text-left text-sm hover:bg-(--color-bg)"
          aria-label="New chat"
        >
          + New chat
        </button>
        <button
          type="button"
          onClick={onToggleCollapsed}
          className="rounded-md p-1.5 text-(--color-muted) hover:bg-(--color-bg)"
          aria-label="Collapse sidebar"
          title="Collapse sidebar"
        >
          <span aria-hidden="true">«</span>
        </button>
      </div>

      <div className="px-3 pb-1 pt-3 text-xs uppercase tracking-wide text-(--color-muted)">
        Saved
      </div>

      <ul className="flex-1 overflow-y-auto px-2 pb-3" data-testid="saved-list">
        {items.length === 0 ? (
          <li className="px-2 py-2 text-xs text-(--color-muted)">
            No saved conversations yet.
            <br />
            Click ★ on a chat to pin it.
          </li>
        ) : (
          items.map((item) => (
            <SavedRow
              key={item.thread_id}
              item={item}
              isCurrent={item.thread_id === currentThreadId}
              onLoad={() => onLoadConversation(item.thread_id)}
              onRefresh={onRefresh}
            />
          ))
        )}
      </ul>
    </aside>
  );
}

function SavedRow({
  item,
  isCurrent,
  onLoad,
  onRefresh,
}: {
  item: SavedConversation;
  isCurrent: boolean;
  onLoad: () => void;
  onRefresh: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.title);

  const commit = async () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (!trimmed || trimmed === item.title) {
      setDraft(item.title);
      return;
    }
    try {
      await saveConversation(item.thread_id, { title: trimmed });
      await onRefresh();
    } catch (err) {
      console.error("title update failed:", err);
      setDraft(item.title);
    }
  };

  return (
    <li
      className={
        "group rounded-md px-2 py-1.5 text-sm " +
        (isCurrent ? "bg-(--color-bg) font-medium" : "hover:bg-(--color-bg)")
      }
      data-testid="saved-row"
      data-thread-id={item.thread_id}
    >
      {editing ? (
        <input
          autoFocus
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void commit()}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void commit();
            } else if (e.key === "Escape") {
              setEditing(false);
              setDraft(item.title);
            }
          }}
          className="w-full rounded-sm border border-(--color-border) bg-white px-1 py-0.5 text-sm"
          aria-label="Edit title"
        />
      ) : (
        <button
          type="button"
          onClick={onLoad}
          onDoubleClick={() => setEditing(true)}
          className="w-full truncate text-left"
          title={`${item.title} — double-click to edit`}
        >
          {item.title}
        </button>
      )}
      {item.last_question && !editing && (
        <div className="mt-0.5 truncate text-xs text-(--color-muted)">
          {item.turn_count} turn{item.turn_count === 1 ? "" : "s"}
          {item.last_question ? ` · ${item.last_question}` : ""}
        </div>
      )}
    </li>
  );
}
