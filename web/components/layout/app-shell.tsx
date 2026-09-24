import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface AppShellProps {
  sidebar: ReactNode;
  chat: ReactNode;
  artifact: ReactNode;
  /** حالة لوحة المخرجات — مفتوحة أم مطوية (المنطق في workspace). */
  artifactOpen: boolean;
}

/**
 * صدفة مساحة العمل — ثلاث مناطق بأسلوب Manus، RTL-first:
 *   [ السايدبار يمينًا | المحادثة | لوحة المخرجات يسارًا ]
 * مع <html dir="rtl"> ينقلب ترتيب أعمدة الـgrid تلقائيًا،
 * فلا حاجة لأي منطق اتجاه هنا.
 */
export function AppShell({ sidebar, chat, artifact, artifactOpen }: AppShellProps) {
  return (
    <div
      className={cn(
        "grid h-dvh w-full grid-cols-1 overflow-clip bg-canvas",
        "md:grid-cols-[var(--layout-sidebar-width)_minmax(0,1fr)]",
        artifactOpen
          ? "xl:grid-cols-[var(--layout-sidebar-width)_minmax(0,1fr)_minmax(400px,var(--layout-artifact-width))]"
          : "xl:grid-cols-[var(--layout-sidebar-width)_minmax(0,1fr)]"
      )}
    >
      <aside
        aria-label="التنقل في مساحة العمل"
        className="hidden min-w-0 border-e border-line bg-canvas-subtle/60 md:flex"
      >
        {sidebar}
      </aside>

      <main className="flex min-w-0 flex-col">{chat}</main>

      {artifactOpen ? (
        <aside
          aria-label="لوحة المخرجات"
          className="hidden min-w-0 animate-fade-in border-s border-line bg-canvas-subtle/40 xl:flex"
        >
          {artifact}
        </aside>
      ) : null}
    </div>
  );
}
