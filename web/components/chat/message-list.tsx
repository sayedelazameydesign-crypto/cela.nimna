import { messages } from "@/lib/mock";
import { MessageBubble } from "./message-bubble";

/** Scrollable transcript column, centered on a readable measure. */
export function MessageList() {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-chat flex-col gap-6 px-5 py-8">
        {messages.map((message) => (
          <MessageBubble key={message.id} message={message} />
        ))}

        {/* End-of-transcript marker (static) */}
        <p className="pb-2 pt-4 text-center text-2xs text-ink-muted">
          Nimna is waiting for your next instruction
        </p>
      </div>
    </div>
  );
}
