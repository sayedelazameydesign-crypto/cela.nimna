/**
 * Static demo content for the workspace shell.
 * Pure data — no logic, no fetchers. Will be replaced by real
 * API/WebSocket bindings once interactions are approved.
 */

export type SessionStatus = "running" | "done" | "needs-approval" | "queued";

export interface Session {
  id: string;
  title: string;
  status: SessionStatus;
  updated: string;
}

export const sessions: Session[] = [
  { id: "s-01", title: "Q3 support analysis", status: "running", updated: "now" },
  { id: "s-02", title: "Competitor pricing sweep", status: "done", updated: "2h" },
  { id: "s-03", title: "Launch email sequence", status: "needs-approval", updated: "5h" },
  { id: "s-04", title: "Churn cohort dashboard", status: "done", updated: "yesterday" },
  { id: "s-05", title: "Arabic onboarding copy review", status: "done", updated: "2d" },
  { id: "s-06", title: "Vendor contract redlines", status: "queued", updated: "3d" },
];

export type StepStatus = "done" | "running" | "queued";

export interface ToolStep {
  id: string;
  label: string;
  detail: string;
  status: StepStatus;
  duration?: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  time: string;
  steps?: ToolStep[];
  artifactRef?: boolean;
}

export const messages: Message[] = [
  {
    id: "m-01",
    role: "user",
    time: "09:41",
    content:
      "Analyze last quarter's support tickets and draft an executive summary with the top three issues.",
  },
  {
    id: "m-02",
    role: "assistant",
    time: "09:41",
    steps: [
      {
        id: "t-01",
        label: "Plan",
        detail: "Selected skills: csv_analysis · report_writer",
        status: "done",
        duration: "2.1s",
      },
      {
        id: "t-02",
        label: "Read support_q3.csv",
        detail: "12,480 rows · 9 columns",
        status: "done",
        duration: "0.8s",
      },
      {
        id: "t-03",
        label: "Run analysis",
        detail: "python_executor · grouped by category and week",
        status: "done",
        duration: "6.4s",
      },
    ],
    content:
      "Q3 closed with 12,480 tickets — down 8% quarter over quarter, but billing disputes jumped to 31% of all escalations. Two more issues stand out: onboarding confusion around the new workspace switcher, and slow webhook retries after the August incident. The full breakdown is in the table inside the brief.",
  },
  {
    id: "m-03",
    role: "user",
    time: "09:44",
    content: "Nice. Turn it into a one-page brief for the leadership sync.",
  },
  {
    id: "m-04",
    role: "assistant",
    time: "09:44",
    artifactRef: true,
    content:
      "Done — I drafted a one-page brief covering the three priority issues, the supporting numbers, and suggested owners. It is open in the artifact panel. Want a stricter tone, or an Arabic version?",
  },
];

export const artifact = {
  title: "Executive Brief — Q3 Support",
  kind: "Document",
  status: "Live",
  synced: "synced 12s ago",
  sandboxId: "nimna-sb-7f3a",
  preparedBy: "Nimna · skills: csv_analysis, report_writer",
  date: "Sep 24, 2026",
  sources: "support_q3.csv · incidents.log · csat_q3.csv",
};

export const user = {
  initials: "SA",
  name: "Sayed",
  plan: "Beta",
  creditsUsed: 1240,
  creditsTotal: 2000,
};
