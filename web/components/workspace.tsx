"use client";

import * as React from "react";
import { initialMessages, type Message } from "@/lib/mock";
import { AppShell } from "@/components/layout/app-shell";
import { Sidebar } from "@/components/layout/sidebar";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ArtifactPanel } from "@/components/artifacts/artifact-panel";

/**
 * منسّق مساحة العمل — يحمل حالة المرحلة 2:
 *  1) سجل الرسائل وحالة الإرسال (الأولوية الأولى)
 *  2) تبويب لوحة المخرجات داخل ArtifactPanel نفسه (Radix)
 *  3) طيّ/فتح لوحة المخرجات (الأولوية الثالثة)
 *  + الثيم الداكن اختياريًا — الافتراضي فاتح، دون تثبيت في التخزين بعد.
 */
export function Workspace() {
  const [messages, setMessages] = React.useState<Message[]>(initialMessages);
  const [artifactOpen, setArtifactOpen] = React.useState(true);
  const [dark, setDark] = React.useState(false);

  React.useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  const sendMessage = React.useCallback((text: string) => {
    const time = new Date().toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
    });
    setMessages((previous) => [
      ...previous,
      { id: crypto.randomUUID(), role: "user", content: text, time },
    ]);
  }, []);

  return (
    <AppShell
      artifactOpen={artifactOpen}
      sidebar={<Sidebar />}
      chat={
        <ChatPanel
          messages={messages}
          onSend={sendMessage}
          artifactOpen={artifactOpen}
          onToggleArtifact={() => setArtifactOpen((open) => !open)}
          dark={dark}
          onToggleDark={() => setDark((value) => !value)}
        />
      }
      artifact={<ArtifactPanel onClose={() => setArtifactOpen(false)} />}
    />
  );
}
