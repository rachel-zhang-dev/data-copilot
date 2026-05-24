"use client";

import { useState } from "react";

/**
 * The "type your question" textarea + submit button. Plain controlled
 * component; the parent owns the actual send.
 */
export function ChatInput({
  onSubmit,
  disabled,
}: {
  onSubmit: (question: string) => void;
  disabled: boolean;
}) {
  const [text, setText] = useState("");

  const send = () => {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
    setText("");
  };

  return (
    <form
      className="flex items-end gap-2 border-t border-(--color-border) bg-white p-3"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          // Cmd/Ctrl-Enter submits, plain Enter inserts a newline.
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            send();
          }
        }}
        placeholder="Ask a question about the data..."
        rows={2}
        disabled={disabled}
        className="w-full resize-none rounded-md border border-(--color-border) p-2 text-sm shadow-xs focus:border-(--color-accent) focus:outline-none disabled:bg-(--color-bg) disabled:text-(--color-muted)"
        aria-label="question"
      />
      <button
        type="submit"
        disabled={disabled || !text.trim()}
        className="rounded-md bg-(--color-accent) px-4 py-2 text-sm font-medium text-(--color-accent-fg) hover:opacity-90 disabled:opacity-50"
      >
        Send
      </button>
    </form>
  );
}
