import {
  Code2,
  Download,
  ExternalLink,
  Eye,
  RefreshCw,
  Terminal,
  X,
} from "lucide-react";
import { artifact } from "@/lib/mock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const TABS = [
  { label: "Preview", icon: Eye, active: true },
  { label: "Code", icon: Code2, active: false },
  { label: "Console", icon: Terminal, active: false },
] as const;

/**
 * Right-hand artifact panel (Manus-style "the agent's desk").
 * Tabs are rendered but inert — switching logic lands in the
 * interaction phase, after approval.
 */
export function ArtifactPanel() {
  return (
    <section className="flex h-full w-full min-w-0 flex-col" aria-label="Artifact">
      {/* Panel header */}
      <header className="flex h-header shrink-0 items-center gap-2.5 border-b border-line px-4">
        <div className="min-w-0 leading-tight">
          <h2 className="truncate text-caption font-semibold text-ink">
            {artifact.title}
          </h2>
          <p className="text-2xs text-ink-muted">{artifact.kind}</p>
        </div>
        <Badge variant="success" className="ml-1 shrink-0">
          <span className="size-1.5 animate-pulse-dot rounded-full bg-success" />
          {artifact.status}
        </Badge>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <Button variant="ghost" size="icon-sm" aria-label="Refresh artifact">
            <RefreshCw className="size-4" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Close artifact panel">
            <X className="size-4" />
          </Button>
        </div>
      </header>

      {/* Tabs + actions */}
      <div className="flex shrink-0 items-center gap-1 border-b border-line px-4 pt-2">
        {TABS.map((tab) => (
          <div
            key={tab.label}
            className={cn(
              "-mb-px flex cursor-default items-center gap-1.5 border-b-2 px-3 pb-2 text-caption transition-colors duration-fast",
              tab.active
                ? "border-accent font-medium text-ink"
                : "border-transparent text-ink-muted hover:text-ink-secondary"
            )}
          >
            <tab.icon className="size-3.5" />
            {tab.label}
          </div>
        ))}
        <div className="ml-auto flex items-center gap-1 pb-1.5">
          <Button variant="ghost" size="icon-sm" aria-label="Open in new tab">
            <ExternalLink className="size-4" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Download artifact">
            <Download className="size-4" />
          </Button>
        </div>
      </div>

      {/* Document preview */}
      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        <article className="mx-auto max-w-[40rem] rounded-xl border border-line bg-surface p-8 shadow-card">
          <p className="text-2xs font-medium uppercase tracking-wide text-accent">
            Executive brief
          </p>
          <h1 className="mt-2 font-display text-display-lg font-medium tracking-tight text-ink">
            Q3 Support Trends
          </h1>
          <p className="mt-1.5 text-2xs text-ink-muted">
            {artifact.preparedBy} · {artifact.date} · 3 sources
          </p>

          <div className="my-6 h-px w-full bg-line" />

          <p className="text-body leading-relaxed text-ink">
            Support volume closed Q3 at 12,480 tickets, down 8% quarter over
            quarter. The mix, however, shifted: escalations are concentrating
            in billing and onboarding, and a third issue — webhook reliability
            — surfaced after the August incident.
          </p>

          <h2 className="mt-6 text-body font-semibold text-ink">
            Top three issues
          </h2>
          <ol className="mt-3 space-y-3">
            {[
              {
                title: "Billing disputes",
                share: "31% of escalations",
                note: "Proration after mid-cycle plan changes is the dominant driver.",
              },
              {
                title: "Workspace switcher onboarding",
                share: "19% of new-user tickets",
                note: "Users miss the switcher affordance introduced in v2.4.",
              },
              {
                title: "Webhook retry latency",
                share: "+220% after Aug 12",
                note: "Retries queue behind the single replay worker.",
              },
            ].map((issue, index) => (
              <li key={issue.title} className="flex gap-3">
                <span className="grid size-6 shrink-0 place-items-center rounded-full bg-accent-soft text-2xs font-semibold text-accent">
                  {index + 1}
                </span>
                <div className="min-w-0">
                  <p className="text-caption font-medium text-ink">
                    {issue.title}{" "}
                    <span className="ml-1 font-mono text-2xs text-accent">
                      {issue.share}
                    </span>
                  </p>
                  <p className="mt-0.5 text-caption leading-relaxed text-ink-secondary">
                    {issue.note}
                  </p>
                </div>
              </li>
            ))}
          </ol>

          <h2 className="mt-6 text-body font-semibold text-ink">
            Quarterly snapshot
          </h2>
          <table className="mt-3 w-full border-collapse text-caption">
            <thead>
              <tr className="border-b border-line text-left text-2xs uppercase tracking-wide text-ink-muted">
                <th className="py-2 pr-4 font-medium">Month</th>
                <th className="py-2 pr-4 font-medium">Tickets</th>
                <th className="py-2 pr-4 font-medium">Esc. rate</th>
                <th className="py-2 font-medium">CSAT</th>
              </tr>
            </thead>
            <tbody className="text-ink">
              {[
                ["July", "4,310", "6.1%", "4.5"],
                ["August", "4,482", "7.4%", "4.3"],
                ["September", "3,688", "6.8%", "4.4"],
              ].map((row) => (
                <tr key={row[0]} className="border-b border-line/60 last:border-0">
                  <td className="py-2 pr-4 font-medium">{row[0]}</td>
                  <td className="py-2 pr-4 font-mono text-2xs">{row[1]}</td>
                  <td className="py-2 pr-4 font-mono text-2xs">{row[2]}</td>
                  <td className="py-2 font-mono text-2xs">{row[3]}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="mt-6 rounded-lg bg-canvas-subtle px-3.5 py-3 text-caption leading-relaxed text-ink-secondary">
            Suggested owners: Billing → payments squad · Onboarding → growth
            squad · Webhooks → platform squad. Full data attached in Drive.
          </p>
        </article>
      </div>

      {/* Panel footer */}
      <footer className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-2">
        <span className="size-1.5 rounded-full bg-success" />
        <span className="text-2xs text-ink-muted">{artifact.synced}</span>
        <span className="ml-auto font-mono text-2xs text-ink-muted">
          {artifact.sandboxId}
        </span>
      </footer>
    </section>
  );
}
