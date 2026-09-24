import { AppShell } from "@/components/layout/app-shell";
import { Sidebar } from "@/components/layout/sidebar";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ArtifactPanel } from "@/components/artifacts/artifact-panel";

/**
 * Home = the workspace shell.
 * Static composition only — no state, no handlers, no data fetching yet.
 */
export default function Home() {
  return (
    <AppShell
      sidebar={<Sidebar />}
      chat={<ChatPanel />}
      artifact={<ArtifactPanel />}
    />
  );
}
