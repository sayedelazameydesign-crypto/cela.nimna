/**
 * بيانات عرض ثابتة لصدفة مساحة العمل.
 * لا منطق هنا ولا جلب بيانات — تُستبدل بربط REST/WebSocket لاحقًا.
 */

export type SessionStatus = "running" | "done" | "needs-approval" | "queued";

export interface Session {
  id: string;
  title: string;
  status: SessionStatus;
  updated: string;
}

export const sessions: Session[] = [
  { id: "s-01", title: "تحليل دعم الربع الثالث", status: "running", updated: "الآن" },
  { id: "s-02", title: "مسح أسعار المنافسين", status: "done", updated: "قبل ساعتين" },
  { id: "s-03", title: "سلسلة رسائل الإطلاق", status: "needs-approval", updated: "قبل 5 ساعات" },
  { id: "s-04", title: "لوحة تحليل تسرّب العملاء", status: "done", updated: "أمس" },
  { id: "s-05", title: "مراجعة نصوص الاستقبال", status: "done", updated: "قبل يومين" },
  { id: "s-06", title: "تعديلات عقد المورد", status: "queued", updated: "قبل 3 أيام" },
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

export const initialMessages: Message[] = [
  {
    id: "m-01",
    role: "user",
    time: "09:41",
    content:
      "حلّل تذاكر الدعم للربع الأخير وأعِدّ ملخصًا تنفيذيًا بأبرز ثلاث مشكلات.",
  },
  {
    id: "m-02",
    role: "assistant",
    time: "09:41",
    steps: [
      {
        id: "t-01",
        label: "التخطيط",
        detail: "اختيار المهارات: تحليل الجداول · كتابة التقارير",
        status: "done",
        duration: "2.1ث",
      },
      {
        id: "t-02",
        label: "قراءة support_q3.csv",
        detail: "12,480 صفًا · 9 أعمدة",
        status: "done",
        duration: "0.8ث",
      },
      {
        id: "t-03",
        label: "تشغيل التحليل",
        detail: "منفّذ بايثون · تجميع حسب الفئة والأسبوع",
        status: "done",
        duration: "6.4ث",
      },
    ],
    content:
      "أغلق الربع الثالث على 12,480 تذكرة — بانخفاض 8% عن الربع السابق، لكن نزاعات الفوترة قفزت إلى 31% من إجمالي التصعيدات. وتبرز مشكلتان إضافيتان: ارتباك المستخدمين الجدد حول مبدّل مساحات العمل، وبطء إعادة محاولة الـwebhooks بعد عطل أغسطس. التفاصيل الكاملة في الجدول داخل الموجز.",
  },
  {
    id: "m-03",
    role: "user",
    time: "09:44",
    content: "ممتاز. حوّله إلى موجز من صفحة واحدة لاجتماع القيادة.",
  },
  {
    id: "m-04",
    role: "assistant",
    time: "09:44",
    artifactRef: true,
    content:
      "تم — أعددت موجزًا من صفحة واحدة يغطي المشكلات الثلاث الأولى والأرقام الداعمة والجهات المقترحة للمتابعة، وهو مفتوح في لوحة المخرجات. هل تفضّل نبرة أكثر رسمية أم نسخة إنجليزية؟",
  },
];

export const artifact = {
  title: "موجز تنفيذي — دعم الربع الثالث",
  kind: "وثيقة",
  status: "مباشر",
  synced: "تمت المزامنة قبل 12 ثانية",
  sandboxId: "nimna-sb-7f3a",
  preparedBy: "نِمنا · المهارات: تحليل الجداول، كتابة التقارير",
  date: "24 سبتمبر 2026",
};

export const user = {
  initials: "س",
  name: "السيد",
  plan: "نسخة تجريبية",
  creditsUsed: 1240,
  creditsTotal: 2000,
};

export const SKILLS = [
  "تلقائي",
  "تحليل الجداول",
  "كتابة التقارير",
  "منفّذ بايثون",
  "تصفح الويب",
] as const;

export const MODELS = [
  "gemini-2.5-pro",
  "gemini-2.5-flash",
  "mock-local",
] as const;
