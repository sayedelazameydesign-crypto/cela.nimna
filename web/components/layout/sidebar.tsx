import {
  HardDrive,
  Home,
  Library,
  MonitorPlay,
  Plus,
  Search,
  Settings,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { sessions, user, type SessionStatus } from "@/lib/mock";
import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const NAV_ITEMS: { label: string; icon: LucideIcon; badge?: string }[] = [
  { label: "Home", icon: Home },
  { label: "Knowledge", icon: Library },
  { label: "Computer", icon: MonitorPlay, badge: "beta" },
  { label: "Drive", icon: HardDrive },
];

const STATUS_DOT: Record<SessionStatus, string> = {
  running: "bg-accent animate-pulse-dot",
  done: "bg-success",
  "needs-approval": "bg-warning",
  queued: "bg-ink-muted",
};

const STATUS_LABEL: Record<SessionStatus, string> = {
  running: "running",
  done: "done",
  "needs-approval": "needs approval",
  queued: "queued",
};

export function Sidebar() {
  const creditsPct = Math.round(
    (user.creditsUsed / user.creditsTotal) * 100
  );

  return (
    <div className="flex h-full w-full flex-col">
      {/* Brand */}
      <div className="flex items-center gap-2.5 px-5 pb-4 pt-5">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent font-display text-lg italic text-accent-ink shadow-card">
          N
        </span>
        <div className="leading-tight">
          <p className="font-display text-title font-medium tracking-tight">
            Nimna
          </p>
          <p className="text-2xs text-ink-muted">agent workspace</p>
        </div>
        <Button
          variant="ghost"
          size="icon-sm"
          className="ml-auto text-ink-muted"
          aria-label="Workspace settings"
        >
          <Settings className="size-4" />
        </Button>
      </div>

      {/* New task + search */}
      <div className="space-y-2 px-3">
        <Button className="w-full justify-start">
          <Plus className="size-4" />
          New task
        </Button>
        <div className="flex h-9 items-center gap-2 rounded-md border border-line bg-surface px-3 text-ink-muted">
          <Search className="size-3.5" />
          <span className="text-caption">Search tasks…</span>
        </div>
      </div>

      {/* Primary nav */}
      <nav className="mt-4 px-3" aria-label="Primary">
        <ul className="space-y-0.5">
          {NAV_ITEMS.map((item) => (
            <li key={item.label}>
              <div className="flex h-9 cursor-default items-center gap-2.5 rounded-md px-3 text-caption text-ink-secondary transition-colors duration-fast hover:bg-surface hover:text-ink">
                <item.icon className="size-4" />
                {item.label}
                {item.badge ? (
                  <span className="ml-auto rounded-full bg-accent-soft px-1.5 py-px text-2xs font-medium text-accent">
                    {item.badge}
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      </nav>

      {/* Recent tasks */}
      <div className="mt-5 flex min-h-0 flex-1 flex-col px-3">
        <p className="px-3 pb-2 text-2xs font-medium uppercase tracking-wide text-ink-muted">
          Recent tasks
        </p>
        <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto pb-2">
          {sessions.map((session, index) => (
            <li key={session.id}>
              <div
                className={cn(
                  "cursor-default rounded-lg px-3 py-2 transition-colors duration-fast",
                  index === 0
                    ? "border border-line/70 bg-surface shadow-card"
                    : "hover:bg-surface/70"
                )}
              >
                <p className="truncate text-caption font-medium text-ink">
                  {session.title}
                </p>
                <p className="mt-0.5 flex items-center gap-1.5 text-2xs text-ink-muted">
                  <span
                    className={cn(
                      "size-1.5 rounded-full",
                      STATUS_DOT[session.status]
                    )}
                  />
                  {STATUS_LABEL[session.status]} · {session.updated}
                </p>
              </div>
            </li>
          ))}
        </ul>
      </div>

      {/* Footer: credits + user */}
      <div className="space-y-2 border-t border-line p-3">
        <div className="rounded-xl border border-line bg-surface p-3 shadow-card">
          <div className="flex items-center gap-2">
            <Sparkles className="size-3.5 text-accent" />
            <span className="text-caption font-medium text-ink">
              Beta credits
            </span>
            <span className="ml-auto text-2xs text-ink-muted">
              {user.creditsUsed.toLocaleString()} /{" "}
              {user.creditsTotal.toLocaleString()}
            </span>
          </div>
          <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-sunken">
            <div
              className="h-full rounded-full bg-accent"
              style={{ width: `${creditsPct}%` }}
            />
          </div>
        </div>

        <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5 transition-colors duration-fast hover:bg-surface/70">
          <Avatar initials={user.initials} className="bg-accent-soft text-accent" />
          <div className="leading-tight">
            <p className="text-caption font-medium text-ink">{user.name}</p>
            <p className="text-2xs text-ink-muted">{user.plan} plan</p>
          </div>
        </div>
      </div>
    </div>
  );
}
