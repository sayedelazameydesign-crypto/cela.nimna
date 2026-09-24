import { Code2, Download, ExternalLink, Eye, RefreshCw, Terminal, X } from "lucide-react";
import { artifact } from "@/lib/mock";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface ArtifactPanelProps {
  onClose: () => void;
}

/** كود ثابت لعرضه في تبويب "الكود" (يبقى LTR داخل واجهة RTL). */
const SAMPLE_CODE = `import pandas as pd

tickets = pd.read_csv("support_q3.csv")

escalations = (
    tickets[tickets.escalated]
    .groupby(["category", pd.Grouper(key="created_at", freq="W")])
    .size()
    .rename("count")
)

top_issues = (
    tickets[tickets.escalated]["category"]
    .value_counts(normalize=True)
    .head(3)
)

print(top_issues.round(3))`;

/** سجل طرفية ثابت لعرضه في تبويب "الطرفية" (يبقى LTR). */
const SAMPLE_CONSOLE = [
  { level: "info", text: "sandbox nimna-sb-7f3a ready (python 3.12)" },
  { level: "info", text: "skill loaded: csv_analysis" },
  { level: "info", text: "skill loaded: report_writer" },
  { level: "run", text: "python analysis/q3_escalations.py" },
  { level: "out", text: "billing_disputes        0.31" },
  { level: "out", text: "workspace_switcher      0.19" },
  { level: "out", text: "webhook_retry_latency   0.12" },
  { level: "ok", text: "exit 0 · 6.4s" },
] as const;

const LEVEL_COLOR: Record<(typeof SAMPLE_CONSOLE)[number]["level"], string> = {
  info: "text-ink-muted",
  run: "text-info",
  out: "text-ink",
  ok: "text-success",
};

/**
 * لوحة المخرجات — "مكتب الوكيل" بأسلوب Manus.
 * الأولوية الثانية في المرحلة 2: تبديل التبويبات عبر Radix Tabs.
 */
export function ArtifactPanel({ onClose }: ArtifactPanelProps) {
  return (
    <section className="flex h-full w-full min-w-0 flex-col" aria-label="لوحة المخرجات">
      {/* ترويسة اللوحة */}
      <header className="flex h-header shrink-0 items-center gap-2.5 border-b border-line px-4">
        <div className="min-w-0 leading-tight">
          <h2 className="truncate text-caption font-semibold text-ink">
            {artifact.title}
          </h2>
          <p className="text-2xs text-ink-muted">{artifact.kind}</p>
        </div>
        <Badge variant="success" className="ms-1 shrink-0">
          <span className="size-1.5 animate-pulse-dot rounded-full bg-success" />
          {artifact.status}
        </Badge>
        <div className="ms-auto flex shrink-0 items-center gap-1">
          <Button variant="ghost" size="icon-sm" aria-label="تحديث المخرجات">
            <RefreshCw className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={onClose}
            aria-label="إغلاق لوحة المخرجات"
          >
            <X className="size-4" />
          </Button>
        </div>
      </header>

      {/* التبويبات + المحتوى */}
      <Tabs
        defaultValue="preview"
        className="flex min-h-0 flex-1 flex-col"
      >
        <div className="flex shrink-0 items-center gap-1 border-b border-line px-4 pt-2">
          <TabsList>
            <TabsTrigger value="preview">
              <Eye className="size-3.5" />
              معاينة
            </TabsTrigger>
            <TabsTrigger value="code">
              <Code2 className="size-3.5" />
              الكود
            </TabsTrigger>
            <TabsTrigger value="console">
              <Terminal className="size-3.5" />
              الطرفية
            </TabsTrigger>
          </TabsList>
          <div className="ms-auto flex items-center gap-1 pb-1.5">
            <Button variant="ghost" size="icon-sm" aria-label="فتح في تبويب جديد">
              <ExternalLink className="size-4 rtl:-scale-x-100" />
            </Button>
            <Button variant="ghost" size="icon-sm" aria-label="تنزيل المخرجات">
              <Download className="size-4" />
            </Button>
          </div>
        </div>

        {/* تبويب المعاينة */}
        <TabsContent value="preview" className="overflow-y-auto p-5">
          <article className="mx-auto max-w-[40rem] rounded-xl border border-line bg-surface p-8 shadow-card">
            <p className="text-2xs font-medium uppercase tracking-wide text-accent">
              موجز تنفيذي
            </p>
            <h1 className="mt-2 font-display text-display-lg font-semibold tracking-tight text-ink">
              اتجاهات الدعم في الربع الثالث
            </h1>
            <p className="mt-1.5 text-2xs text-ink-muted">
              {artifact.preparedBy} · {artifact.date} · 3 مصادر
            </p>

            <div className="my-6 h-px w-full bg-line" />

            <p className="text-body leading-relaxed text-ink">
              بلغ حجم الدعم في الربع الثالث 12,480 تذكرة بانخفاض 8% عن الربع
              السابق، لكن التركيبة تغيّرت: تتركز التصعيدات في الفوترة
              والاستقبال، وبرزت مشكلة ثالثة تتعلق بموثوقية الـwebhooks بعد عطل
              أغسطس.
            </p>

            <h2 className="mt-6 text-body font-semibold text-ink">
              المشكلات الثلاث الأولى
            </h2>
            <ol className="mt-3 space-y-3">
              {[
                {
                  title: "نزاعات الفوترة",
                  share: "31% من التصعيدات",
                  note: "التسوية بعد تغيير الخطة في منتصف الدورة هي السبب الرئيسي.",
                },
                {
                  title: "استقبال مبدّل مساحات العمل",
                  share: "19% من تذاكر المستخدمين الجدد",
                  note: "لا يلاحظ المستخدمون نقطة التبديل التي أُضيفت في الإصدار 2.4.",
                },
                {
                  title: "بطء إعادة محاولة الـwebhooks",
                  share: "+220% بعد 12 أغسطس",
                  note: "تصطفّ إعادة المحاولات خلف عامل إعادة تشغيل واحد.",
                },
              ].map((issue, index) => (
                <li key={issue.title} className="flex gap-3">
                  <span className="grid size-6 shrink-0 place-items-center rounded-full bg-accent-soft text-2xs font-semibold text-accent">
                    {(index + 1).toLocaleString("ar-EG")}
                  </span>
                  <div className="min-w-0">
                    <p className="text-caption font-medium text-ink">
                      {issue.title}{" "}
                      <span className="ms-1 font-mono text-2xs text-accent">
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
              لقطة ربع سنوية
            </h2>
            <table className="mt-3 w-full border-collapse text-caption">
              <thead>
                <tr className="border-b border-line text-right text-2xs uppercase tracking-wide text-ink-muted">
                  <th className="py-2 pe-4 font-medium">الشهر</th>
                  <th className="py-2 pe-4 font-medium">التذاكر</th>
                  <th className="py-2 pe-4 font-medium">نسبة التصعيد</th>
                  <th className="py-2 font-medium">رضا العملاء</th>
                </tr>
              </thead>
              <tbody className="text-ink">
                {[
                  ["يوليو", "4,310", "6.1%", "4.5"],
                  ["أغسطس", "4,482", "7.4%", "4.3"],
                  ["سبتمبر", "3,688", "6.8%", "4.4"],
                ].map((row) => (
                  <tr key={row[0]} className="border-b border-line/60 last:border-0">
                    <td className="py-2 pe-4 font-medium">{row[0]}</td>
                    <td className="py-2 pe-4 font-mono text-2xs">{row[1]}</td>
                    <td className="py-2 pe-4 font-mono text-2xs">{row[2]}</td>
                    <td className="py-2 font-mono text-2xs">{row[3]}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <p className="mt-6 rounded-lg bg-canvas-subtle px-3.5 py-3 text-caption leading-relaxed text-ink-secondary">
              الجهات المقترحة: الفوترة ← فريق المدفوعات · الاستقبال ← فريق
              النمو · الـwebhooks ← فريق البنية. البيانات الكاملة مرفقة في
              الملفات.
            </p>
          </article>
        </TabsContent>

        {/* تبويب الكود — يبقى لاتينيًا/يساريًا داخل واجهة RTL */}
        <TabsContent value="code" className="overflow-y-auto p-5">
          <div dir="ltr" className="overflow-hidden rounded-xl bg-code-block shadow-card">
            <div className="flex items-center gap-2 border-b border-white/10 px-4 py-2.5">
              <span className="size-2.5 rounded-full bg-white/15" />
              <span className="size-2.5 rounded-full bg-white/15" />
              <span className="size-2.5 rounded-full bg-white/15" />
              <span className="ms-2 font-mono text-2xs text-code-ink/70">
                analysis/q3_escalations.py
              </span>
            </div>
            <pre className="overflow-x-auto p-4 text-left font-mono text-caption leading-relaxed text-code-ink">
              {SAMPLE_CODE}
            </pre>
          </div>
        </TabsContent>

        {/* تبويب الطرفية */}
        <TabsContent value="console" className="overflow-y-auto p-5">
          <div dir="ltr" className="overflow-hidden rounded-xl bg-code-block p-4 shadow-card">
            <div className="space-y-1.5 text-left font-mono text-caption leading-relaxed">
              {SAMPLE_CONSOLE.map((line, index) => (
                <p key={index} className={LEVEL_COLOR[line.level]}>
                  <span className="me-3 select-none text-white/25">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  {line.text}
                </p>
              ))}
            </div>
          </div>
        </TabsContent>
      </Tabs>

      {/* تذييل اللوحة */}
      <footer className="flex shrink-0 items-center gap-2 border-t border-line px-4 py-2">
        <span className="size-1.5 rounded-full bg-success" />
        <span className="text-2xs text-ink-muted">{artifact.synced}</span>
        <span className="ms-auto font-mono text-2xs text-ink-muted" dir="ltr">
          {artifact.sandboxId}
        </span>
      </footer>
    </section>
  );
}
