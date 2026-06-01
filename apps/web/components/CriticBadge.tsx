import type { Critic } from "@/lib/types";

/**
 * Phase 2.3 / ADR 0021 — low-confidence badge.
 *
 * Renders nothing on ``ok`` verdicts (the common case). On
 * ``suspicious`` or ``wrong`` it shows a small inline card with the
 * critic's reason and any specific concerns, so a careful user knows
 * to double-check the answer before relying on it.
 *
 * Why "show, don't hide": when the critic flags a turn as wrong but
 * the retry budget is exhausted, we deliberately let the answer
 * through rather than blocking it. The user sees the result PLUS the
 * critic's objection and decides whether to trust it. Hiding the
 * answer would be more dangerous: it removes the user's ability to
 * spot-check against their own knowledge.
 */
export function CriticBadge({ critic }: { critic: Critic | null }) {
  if (!critic || critic.verdict === "ok") return null;

  const isWrong = critic.verdict === "wrong";
  const title = isWrong
    ? "Reviewer flagged this answer as wrong"
    : "Low confidence — reviewer suggested double-checking";

  return (
    <section
      className={
        "rounded-md border bg-white p-3 text-sm shadow-xs " +
        (isWrong
          ? "border-(--color-error) text-(--color-error)"
          : "border-(--color-warn)")
      }
      role="status"
      aria-live="polite"
      data-testid="critic-badge"
      data-verdict={critic.verdict}
    >
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="text-base">
          {isWrong ? "⚠️" : "⚠"}
        </span>
        <div className="flex-1">
          <div className="font-medium">{title}</div>
          {critic.reason && (
            <div
              className={
                "mt-0.5 text-xs " +
                (isWrong ? "text-(--color-error)" : "text-(--color-muted)")
              }
            >
              {critic.reason}
            </div>
          )}
          {critic.concerns.length > 0 && (
            <ul
              className={
                "mt-1 list-disc pl-5 text-xs " +
                (isWrong ? "text-(--color-error)" : "text-(--color-muted)")
              }
            >
              {critic.concerns.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}
