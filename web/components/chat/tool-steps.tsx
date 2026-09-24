import { Check, ChevronDown, CircleDashed, LoaderCircle } from "lucide-react";
import type { StepStatus, ToolStep } from "@/lib/mock";
import { cn } from "@/lib/utils";

function StepIcon({ status }: { status: StepStatus }) {
  if (status === "done") {
    return (
      <span className="grid size-5 place-items-center rounded-full bg-success-soft">
        <Check className="size-3 text-success" strokeWidth={2.5} />
      </span>
    );
  }
  if (status === "running") {
    return (
      <span className="grid size-5 place-items-center rounded-full bg-accent-soft">
        <LoaderCircle className="size-3 animate-spin text-accent" />
      </span>
    );
  }
  return (
    <span className="grid size-5 place-items-center rounded-full bg-surface-sunken">
      <CircleDashed className="size-3 text-ink-muted" />
    </span>
  );
}

/**
 * Collapsed "agent used N tools" card (Manus-style transparency).
 * Static for now — expansion becomes real interaction in the next phase.
 */
export function ToolSteps({ steps }: { steps: ToolStep[] }) {
  const total = steps.reduce((sum, step) => {
    if (!step.duration) return sum;
    return sum + parseFloat(step.duration);
  }, 0);

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-surface shadow-card">
      <div className="flex cursor-default items-center gap-2 border-b border-line px-3.5 py-2.5">
        <span className="text-caption font-medium text-ink">
          Used {steps.length} tools
        </span>
        <span className="font-mono text-2xs text-ink-muted">
          {total.toFixed(1)}s
        </span>
        <ChevronDown className="ml-auto size-4 text-ink-muted" />
      </div>

      <ul className="divide-y divide-line/60">
        {steps.map((step) => (
          <li
            key={step.id}
            className="flex items-center gap-2.5 px-3.5 py-2"
          >
            <StepIcon status={step.status} />
            <span className="shrink-0 text-caption font-medium text-ink">
              {step.label}
            </span>
            <span className="hidden min-w-0 flex-1 truncate text-2xs text-ink-muted sm:block">
              {step.detail}
            </span>
            {step.duration ? (
              <span
                className={cn(
                  "ml-auto shrink-0 font-mono text-2xs text-ink-muted"
                )}
              >
                {step.duration}
              </span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
