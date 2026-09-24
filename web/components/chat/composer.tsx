import { ArrowUp, ChevronDown, Cpu, Paperclip, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/**
 * Input area. Fully inert for now:
 * no onChange, no submit, no autosize — those arrive with the
 * interaction phase, after design approval.
 */
export function Composer() {
  return (
    <div className="shrink-0 px-5 pb-5 pt-2">
      <div className="mx-auto w-full max-w-chat">
        <div className="rounded-2xl border border-line bg-surface shadow-card transition-colors duration-fast focus-within:border-line-strong">
          <Textarea
            readOnly
            rows={2}
            placeholder="Ask Nimna to plan, run, or build…"
            aria-label="Message Nimna"
            className="min-h-[60px] resize-none px-4 pt-3.5"
          />

          <div className="flex items-center gap-1.5 px-3 pb-2.5">
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-ink-muted"
              aria-label="Attach files"
            >
              <Paperclip className="size-4" />
            </Button>

            <div className="flex h-8 cursor-default items-center gap-1.5 rounded-md px-2.5 text-2xs font-medium text-ink-secondary transition-colors duration-fast hover:bg-surface-sunken">
              <Cpu className="size-3.5" />
              Skills: auto
              <ChevronDown className="size-3 text-ink-muted" />
            </div>

            <div className="ml-auto flex items-center gap-1.5">
              <div className="hidden h-8 cursor-default items-center gap-1.5 rounded-md px-2.5 font-mono text-2xs text-ink-secondary transition-colors duration-fast hover:bg-surface-sunken sm:flex">
                gemini-2.5-pro
                <ChevronDown className="size-3 text-ink-muted" />
              </div>

              <Button
                size="icon-sm"
                className="rounded-full"
                aria-label="Stop generating"
              >
                <Square className="size-3.5 fill-current" />
              </Button>
              <Button
                size="icon-sm"
                className="rounded-full"
                aria-label="Send message"
              >
                <ArrowUp className="size-4" />
              </Button>
            </div>
          </div>
        </div>

        <p className="mt-2 text-center text-2xs text-ink-muted">
          Nimna backs claims with evidence — verify important outputs.
        </p>
      </div>
    </div>
  );
}
