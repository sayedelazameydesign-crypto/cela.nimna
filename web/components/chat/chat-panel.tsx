"use client";

import {
  Moon,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Share2,
  Sun,
} from "lucide-react";
import type { Message } from "@/lib/mock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { MessageList } from "./message-list";
import { Composer } from "./composer";

export interface ChatPanelProps {
  messages: Message[];
  onSend: (text: string) => void;
  artifactOpen: boolean;
  onToggleArtifact: () => void;
  dark: boolean;
  onToggleDark: () => void;
}

/** العمود الأوسط: ترويسة + سجل الرسائل + حقل الإدخال. */
export function ChatPanel({
  messages,
  onSend,
  artifactOpen,
  onToggleArtifact,
  dark,
  onToggleDark,
}: ChatPanelProps) {
  return (
    <section className="flex h-full min-w-0 flex-col" aria-label="المحادثة">
      {/* الترويسة */}
      <header className="flex h-header shrink-0 items-center gap-3 border-b border-line px-5">
        <div className="min-w-0 leading-tight">
          <h1 className="truncate text-body font-semibold text-ink">
            تحليل دعم الربع الثالث
          </h1>
          <p className="text-2xs text-ink-muted">جلسة s-01 · بدأت 09:41</p>
        </div>

        <div className="ms-auto flex items-center gap-2">
          <Badge variant="success" className="hidden sm:inline-flex">
            <span className="size-1.5 animate-pulse-dot rounded-full bg-success" />
            البيئة الرملية جاهزة
          </Badge>
          <Badge variant="default" className="hidden lg:inline-flex">
            RC-2 · 26 أداة
          </Badge>

          <Separator orientation="vertical" className="mx-1 h-5" />

          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggleDark}
            aria-label={dark ? "التبديل إلى الوضع الفاتح" : "التبديل إلى الوضع الداكن"}
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="مشاركة الجلسة">
            <Share2 className="size-4" />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="خيارات إضافية">
            <MoreHorizontal className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onToggleArtifact}
            aria-label={artifactOpen ? "طيّ لوحة المخرجات" : "فتح لوحة المخرجات"}
          >
            {artifactOpen ? (
              <PanelLeftClose className="size-4" />
            ) : (
              <PanelLeftOpen className="size-4" />
            )}
          </Button>
        </div>
      </header>

      {/* السجل */}
      <MessageList messages={messages} />

      {/* حقل الإدخال */}
      <Composer onSend={onSend} />
    </section>
  );
}
