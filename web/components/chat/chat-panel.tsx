import { MoreHorizontal, PanelRightClose, Share2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { MessageList } from "./message-list";
import { Composer } from "./composer";

/**
 * Center column: header + scrolling transcript + composer.
 * Static composition — no handlers or state yet.
 */
export function ChatPanel() {
  return (
    <section className="flex h-full min-w-0 flex-col" aria-label="Conversation">
      {/* Header */}
      <header className="flex h-header shrink-0 items-center gap-3 border-b border-line px-5">
        <div className="min-w-0 leading-tight">
          <h1 className="truncate text-body font-semibold text-ink">
            Q3 support analysis
          </h1>
          <p className="text-2xs text-ink-muted">
            Session s-01 · started 09:41
          </p>
        </div>

        <div className="ml-auto flex items-center gap-2">
          <Badge variant="success" className="hidden sm:inline-flex">
            <span className="size-1.5 animate-pulse-dot rounded-full bg-success" />
            Sandbox ready
          </Badge>
          <Badge variant="default" className="hidden lg:inline-flex">
            RC-2 · 26 tools
          </Badge>

          <Separator orientation="vertical" className="mx-1 h-5" />

          <Button variant="ghost" size="icon-sm" aria-label="Share session">
            <Share2 className="size-4" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="More options">
            <MoreHorizontal className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Collapse artifact panel"
          >
            <PanelRightClose className="size-4" />
          </Button>
        </div>
      </header>

      {/* Transcript */}
      <MessageList />

      {/* Composer */}
      <Composer />
    </section>
  );
}
