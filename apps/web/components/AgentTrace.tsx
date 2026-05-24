import type { PhaseEvent } from "@/lib/types";

/**
 * Compact progress bar that ticks one box per node activation. Hides
 * internal bookkeeping nodes (``reset_per_turn``, ``append_to_dialogue``,
 * ``compact_history``) unless the user has enabled "verbose mode"
 * via the ``showInternal`` prop (off by default).
 *
 * This is the cheapest possible way to convert raw streaming events
 * into "the agent is doing X" UX. A future polish week could replace
 * this with an animated stepper.
 */
export function AgentTrace({
  phases,
  isStreaming,
  showInternal = false,
}: {
  phases: PhaseEvent[];
  isStreaming: boolean;
  showInternal?: boolean;
}) {
  const visible = showInternal ? phases : phases.filter((p) => !p.internal);
  if (visible.length === 0 && !isStreaming) return null;

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs text-(--color-muted)">
      {visible.map((p, i) => (
        <span
          key={`${p.node}-${i}`}
          className="rounded-sm border border-(--color-border) bg-white px-1.5 py-0.5 font-mono"
        >
          {p.node}
        </span>
      ))}
      {isStreaming && (
        <span
          className="inline-flex h-2 w-2 animate-pulse rounded-full bg-(--color-accent)"
          aria-label="streaming"
        />
      )}
    </div>
  );
}
