import type { Message } from "@/lib/mock";
import { MessageBubble } from "./message-bubble";

/** عمود السجل القابل للتمرير، متمركز على عرض قراءة مريح. */
export function MessageList({ messages }: { messages: Message[] }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-chat flex-col gap-6 px-5 py-8">
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        <p className="pb-2 pt-4 text-center text-2xs text-ink-muted">
          نِمنا بانتظار تعليماتك التالية
        </p>
      </div>
    </div>
  );
}
