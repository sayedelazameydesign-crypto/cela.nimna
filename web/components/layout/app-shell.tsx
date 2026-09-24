import type { ReactNode } from "react";

export interface AppShellProps {
  sidebar: ReactNode;
  chat: ReactNode;
  artifact: ReactNode;
}

/**
 * The workspace shell — three regions, Manus-style:
 *   [ Sidebar 264px | Chat (fluid) | Artifact panel 520px ]
 *
 * Pure layout: no state, no handlers. Responsive behavior is
 * presentational only (sidebar < md, artifact < xl are hidden).
 */
export function AppShell({ sidebar, chat, artifact }: AppShellProps) {
  return (
    <div className="grid h-dvh w-full grid-cols-1 overflow-clip bg-canvas md:grid-cols-[var(--layout-sidebar-width)_minmax(0,1fr)] xl:grid-cols-[var(--layout-sidebar-width)_minmax(0,1fr)_minmax(400px,var(--layout-artifact-width))]">
      <aside
        aria-label="Workspace navigation"
        className="hidden min-w-0 border-r border-line bg-canvas-subtle/60 md:flex"
      >
        {sidebar}
      </aside>

      <main className="flex min-w-0 flex-col">{chat}</main>

      <aside
        aria-label="Artifact panel"
        className="hidden min-w-0 border-l border-line bg-canvas-subtle/40 xl:flex"
      >
        {artifact}
      </aside>
    </div>
  );
}
