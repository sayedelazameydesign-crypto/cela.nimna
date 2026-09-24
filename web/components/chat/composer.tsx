"use client";

import * as React from "react";
import { ArrowUp, ChevronDown, Cpu, Paperclip, Square } from "lucide-react";
import { MODELS, SKILLS } from "@/lib/mock";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";

export interface ComposerProps {
  onSend: (text: string) => void;
}

/**
 * حقل الإدخال — أول تفاعل مفعّل في المرحلة 2.
 * حالة محلية فقط: النص الحالي، المهارة المختارة، النموذج المختار.
 * لا توجد محاكاة رد من الوكيل بعد؛ الإرسال يضيف رسالة المستخدم إلى السجل.
 */
export function Composer({ onSend }: ComposerProps) {
  const [value, setValue] = React.useState("");
  const [skill, setSkill] = React.useState<string>(SKILLS[0]);
  const [model, setModel] = React.useState<string>(MODELS[0]);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  const trimmed = value.trim();
  const canSend = trimmed.length > 0;

  /* توسيط ارتفاع الحقل مع الكتابة (حد أقصى ~8 أسطر) */
  React.useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 192)}px`;
  }, [value]);

  const submit = () => {
    if (!canSend) return;
    onSend(trimmed);
    setValue("");
    textareaRef.current?.focus();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <div className="shrink-0 px-5 pb-5 pt-2">
      <div className="mx-auto w-full max-w-chat">
        <div className="rounded-2xl border border-line bg-surface shadow-card transition-colors duration-fast focus-within:border-line-strong">
          <Textarea
            ref={textareaRef}
            rows={1}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="اطلب من نِمنا أن تخطط أو تنفذ أو تبني…"
            aria-label="مراسلة نِمنا"
            className="max-h-48 min-h-[52px] resize-none px-4 pt-3.5"
          />

          <div className="flex items-center gap-1.5 px-3 pb-2.5">
            <Button
              variant="ghost"
              size="icon-sm"
              className="text-ink-muted"
              aria-label="إرفاق ملفات"
            >
              <Paperclip className="size-4" />
            </Button>

            {/* منتقي المهارات */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button className="flex h-8 items-center gap-1.5 rounded-md px-2.5 text-2xs font-medium text-ink-secondary outline-none transition-colors duration-fast hover:bg-surface-sunken focus-visible:shadow-ring">
                  <Cpu className="size-3.5" />
                  المهارات: {skill}
                  <ChevronDown className="size-3 text-ink-muted" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start">
                <DropdownMenuLabel>اختيار المهارة</DropdownMenuLabel>
                <DropdownMenuRadioGroup value={skill} onValueChange={setSkill}>
                  {SKILLS.map((option) => (
                    <DropdownMenuRadioItem key={option} value={option}>
                      {option}
                    </DropdownMenuRadioItem>
                  ))}
                </DropdownMenuRadioGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            <div className="ms-auto flex items-center gap-1.5">
              {/* منتقي النموذج */}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className="hidden h-8 items-center gap-1.5 rounded-md px-2.5 font-mono text-2xs text-ink-secondary outline-none transition-colors duration-fast hover:bg-surface-sunken focus-visible:shadow-ring sm:flex">
                    <span dir="ltr">{model}</span>
                    <ChevronDown className="size-3 text-ink-muted" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  <DropdownMenuLabel dir="rtl">النموذج</DropdownMenuLabel>
                  <DropdownMenuRadioGroup value={model} onValueChange={setModel}>
                    {MODELS.map((option) => (
                      <DropdownMenuRadioItem key={option} value={option}>
                        <span dir="ltr">{option}</span>
                      </DropdownMenuRadioItem>
                    ))}
                  </DropdownMenuRadioGroup>
                </DropdownMenuContent>
              </DropdownMenu>

              <Button
                size="icon-sm"
                variant="secondary"
                disabled
                aria-label="إيقاف التوليد"
                title="يتوفر أثناء البث — المرحلة القادمة"
              >
                <Square className="size-3.5 fill-current" />
              </Button>
              <Button
                size="icon-sm"
                className="rounded-full"
                disabled={!canSend}
                onClick={submit}
                aria-label="إرسال الرسالة"
              >
                <ArrowUp className="size-4" />
              </Button>
            </div>
          </div>
        </div>

        <p className="mt-2 text-center text-2xs text-ink-muted">
          تدعم نِمنا إجاباتها بالأدلة — تحقق من المخرجات المهمة.
        </p>
      </div>
    </div>
  );
}
