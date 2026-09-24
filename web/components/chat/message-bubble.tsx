import { ArrowUpRight, FileText } from "lucide-react";
import type { Message } from "@/lib/mock";
import { artifact } from "@/lib/mock";
import { cn } from "@/lib/utils";
import { ToolSteps } from "./tool-steps";

/**
 * A single message row.
 *  • user      → beige bubble aligned to the trailing edge (Claude-style)
 *  • assistant → full-width, serif-touched agent mark, optional tool-steps
 *                card and artifact reference chip (Manus-style)
 */
export function MessageBubble({ message }: { message: Message }) {
  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-1">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-user-bubble px-4 py-2.5 text-body leading-relaxed text-ink shadow-hairline">
          {message.content}
        </div>
        <span className="pr-1 text-2xs text-ink-muted">{message.time}</span>
      </div>
    );
  }

  return (
    <div className="flex gap-3">
      {/* Agent mark */}
      <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg border border-line bg-surface font-display text-sm italic text-accent shadow-card">
        N
      </span>

      <div className="min-w-0 flex-1 space-y-3">
        {message.steps ? <ToolSteps steps={message.steps} /> : null}

        <p className="prose-nimna whitespace-pre-wrap">{message.content}</p>

        {message.artifactRef ? (
          <div className="flex w-fit max-w-full cursor-default items-center gap-3 rounded-xl border border-line bg-surface px-3.5 py-2.5 shadow-card transition-colors duration-fast hover:border-line-strong">
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-soft">
              <FileText className="size-4 text-accent" />
            </span>
            <span className="min-w-0 leading-tight">
              <span className="block truncate text-caption font-medium text-ink">
                {artifact.title}
              </span>
              <span className="text-2xs text-ink-muted">
                {artifact.kind} · {artifact.status.toLowerCase()}
              </span>
            </span>
            <ArrowUpRight className="size-4 shrink-0 text-ink-muted" />
          </div>
        ) : null}

        <span
          className={cn("block text-2xs text-ink-muted")}>{message.time}</span>
      </div>
    </div>
  );
}
