#!/usr/bin/env node
/**
 * دفع بنود S110/S112 الحية من ruff إلى لوحة الترياج عبر POST /api/triage/sync
 *
 *  externalRef = ruff:<rule>:<path>:<line>   (مفتاح المزامنة الثابت)
 *  - upsert: الموجود يُحدَّث (بلا status ⇒ لا يمسّ قرارًا يدويًا)، والجديد يُدرَج
 *  - إغلاق تلقائي: مرجع كان "open" واختفى من ruff ⇒ status=resolved
 *    (ما راجعته يدويًا "reviewing" لا يُمسّ — يُترك للمراجعة البشرية)
 *
 * التشغيل (من جذر المستودع):
 *   TRIAGE_API_KEY=... node tools/triage-sync/sync-push.mjs
 *
 * env: TRIAGE_BOARD_URL (default http://127.0.0.1:3000) · TRIAGE_API_KEY (إلزامي)
 *      RUFF_BIN (default <repo>/.venv/bin/ruff) · RUFF_CWD (default جذر المستودع)
 *      RUFF_SELECT (default S110,S112) · DRY_RUN=1 لعرض الحِمل بلا إرسال
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const REPO_ROOT = fileURLToPath(new URL("../../", import.meta.url));
const BOARD = (process.env.TRIAGE_BOARD_URL ?? "http://127.0.0.1:3000").replace(/\/$/, "");
const KEY = process.env.TRIAGE_API_KEY ?? "";
const RUFF_BIN = process.env.RUFF_BIN ?? `${REPO_ROOT}.venv/bin/ruff`;
const RUFF_CWD = process.env.RUFF_CWD ?? REPO_ROOT;
const SELECT = process.env.RUFF_SELECT ?? "S110,S112";
const BATCH = 200;

if (!KEY) {
  console.error("TRIAGE_API_KEY مطلوب (نفس مفتاح اللوحة).");
  process.exit(2);
}

const headers = { "content-type": "application/json", authorization: `Bearer ${KEY}` };

// 1) تشغيل ruff — الخروج 1 عند وجود نتائج، وليس فشلًا
let out = "";
try {
  out = execFileSync(RUFF_BIN, ["check", ".", "--select", SELECT, "--output-format", "concise", "-q"], {
    cwd: RUFF_CWD,
    encoding: "utf8",
  });
} catch (err) {
  if (typeof err.stdout === "string" && err.stdout.trim()) out = err.stdout;
  else {
    console.error("فشل تشغيل ruff:", err.message, `(RUFF_BIN=${RUFF_BIN})`);
    process.exit(2);
  }
}

const items = out
  .split("\n")
  .map((line) => line.trim())
  .filter(Boolean)
  .map((line) => {
    const [path, lineno, , rest] = line.split(":", 4);
    const [rule, ...msgParts] = rest.trim().split(" ");
    return {
      externalRef: `ruff:${rule}:${path}:${lineno}`,
      filePath: `${path}:${lineno}`,
      issueType: rule,
      summary: `${msgParts.join(" ")} — بانتظار الترياج (حارس ميت / fallback مقصود / منطق حقيقي)`,
      details: "دفعة hygiene المتبقية — تعديل واحد لكل ملف + قراءة تحقق بعد كل apply",
    };
  });

console.log(`المصدر: ${items.length} بندًا من ruff (${SELECT})`);

if (process.env.DRY_RUN === "1") {
  console.log("DRY_RUN — عيّنة:", JSON.stringify(items.slice(0, 2), null, 1));
  process.exit(0);
}

const post = async (batch, label) => {
  const res = await fetch(`${BOARD}/api/triage/sync`, {
    method: "POST",
    headers,
    body: JSON.stringify({ items: batch }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    console.error(`FAIL ${label}: ${res.status}`, JSON.stringify(body));
    process.exit(1);
  }
  console.log(`${label}: total=${body.total} inserted=${body.inserted} updated=${body.updated}`);
  return body;
};

// 2) دفع البنود الحالية على دفعات
let inserted = 0;
let updated = 0;
for (let i = 0; i < items.length; i += BATCH) {
  const r = await post(
    items.slice(i, i + BATCH),
    `دفعة ${i / BATCH + 1}/${Math.max(1, Math.ceil(items.length / BATCH))}`,
  );
  inserted += r.inserted;
  updated += r.updated;
}

// 3) لقطة للحالة ثم إغلاق ما اختفى (open فقط — المراجعة اليدوية لا تُمسّ)
const snap = await fetch(`${BOARD}/api/triage/snapshot`, { headers }).then((r) => r.json());
const current = new Set(items.map((i) => i.externalRef));
const vanished = (snap.refs ?? []).filter(
  (r) => r.externalRef?.startsWith("ruff:") && r.status === "open" && !current.has(r.externalRef),
);

let closed = 0;
if (vanished.length === 0) {
  console.log("لا بنود مختفية تحتاج إغلاقًا.");
} else {
  const r = await post(
    vanished.map((v) => ({ externalRef: v.externalRef, issueType: v.issueType, status: "resolved" })),
    `إغلاق ${vanished.length} بندًا اختفى من ruff`,
  );
  closed = r.updated;
}

console.log(
  `النتيجة: inserted=${inserted} · updated=${updated} · أُغلق=${closed} · ` +
    `مفتوح باللوحة الآن=${snap.totals?.open ?? "?"} / ${snap.totals?.total ?? "?"}`,
);
