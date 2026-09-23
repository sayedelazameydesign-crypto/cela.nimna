# Architectural Audit + Evolution Blueprint — `cela.nimna` كمنصة وكيل كاملة

> **التاريخ:** 2026-09-23 · **النطاق:** فحص وتصميم فقط — **صفر تعديل على الـ baseline**.
> هذا المستند يجيب على 9 أسئلة: الموجود فعلياً، الناقص، المعمارية المستهدفة، خريطة
> الملفات، الأنظمة السبعة، Self-Improvement الآمن، Arena كمختبر تطور، خارطة الطريق
> المرحلية، والتعليمات التنفيذية للـ coding agent.
>
> **قواعد مصفوفات القدرة تنطبق على هذا المستند نفسه:** كل ما هو «موجود» مدعوم بمسار
> ملف/اختبار قابل للتشغيل، وكل ما هو «مستقبلي» حالة `planned` — لا يُرقَّى إلى
> `implemented` إلا بعقد + اختبار + صف مصفوفة (Definition of Done في
> [`agent-os-blueprint.md`](agent-os-blueprint.md)).

---

## 0. الملخص التنفيذي

`cela.nimna` ليس wrapper حول LLM؛ هو **Agent-OS ناشئ بحوكمة استثنائية**: حلقة محكومة
بحدود، موافقات بشرية قابلة للاستئناف، ذاكرة SQLite بسلسلة إثبات SHA-256، CostGuard
صارم، sandbox بمستويين، و9 مهارات progressive. **لكنه اليوم وكيل «محادثة بأدوات»،
وليس «منفّذ مهام طويلة»** (Manus/Claude-class). الفجوة الحاسمة ليست في الحوكمة بل في
خمس قدرات تنفيذية: **لا Terminal حقيقي، لا تحرير ملفات دقيق، لا خطة طويلة المدى
كحالة قابلة للتحديث، لا بثّ لحظي، لا مختبر تقييم على مستوى المهام**.

الخطة: 6 مراحل تراكمية (P1..P6) فوق الـ baseline دون كسره، كل مرحلة تُغلق ببوابة
أدلة (اختبارات + صفوف مصفوفة + Arena)، وتبدأ بـ **P1 «النواة التنفيذية»** لأنها
أعلى فجوة تأثيراً.

---

## 1. ما الموجود فعلياً الآن (مُثبت بالأدلة)

**الحجم:** ~11,442 سطر Python · 121 ملفاً متتبعاً · 110 اختبار passing · 3 workflows.

| الطبقة | المكوّن (دليل) | الحالة الفعلية |
|---|---|---|
| **Runtime** | `nimna/core/agent.py` (842 سطر) — حلقة `_drive` بحواجز: `max_steps=12`, `max_tool_calls=30`, `max_runtime=300s`, `max_consecutive_failures=5`, كاشف تكرار النص؛ **تعليق/استئناف** الموافقات محفوظ في SQLite (`resume()` + TTL)؛ تدقيق `model_call`/أدوات لكل خطوة | `implemented` — حلقة ReAct محكومة حقيقية |
| **تحقق ذاتي** | `_needs_revision()` — مراجعة واحدة لصياغة الجواب النهائي ضد قوائم مهارات المهام، JSON `{ok, issues}` | `implemented` (محصور بجواب نصي واحد) |
| **تخطيط/توزيع** | `nimna/core/planner.py` (SkillSelector LLM ≤3 مهارات) · `planner_swarm.py` (312 سطر) تفكيك DAG + دفعات متوازية | `partial` — تفكيك upfront فقط، لا خطة تُحدَّث أثناء التنفيذ |
| **مهارات** | 9 مهارات `skills/*/SKILL.md` + references (browser_use, code_execution, computer_control, csv, file_analysis, python_executor, report_writer, skill_author, web_research) | `implemented` — تحميل تدريجي + أدوات مُنطَّقة بالحقل |
| **أدوات** | 26+ أداة: files (**list/read/info/write/delete فقط**) · web (DDG search + fetch مع حجب SSRF) · python_exec (sandbox) · computer (VNC، 691 سطر) · browser_use (Cloud V4) · csv/memory/vector/reports/skill_tools | `implemented` — انظر الفجوات §2-1,2 |
| **ذاكرة** | `nimna/memory/store.py` (497 سطر): جلسات/رسائل/memories kinds+tags/audit بسلسلة SHA-256 (`verify_audit_chain`)/pending بمهلة. البحث: **LIKE كلمات على آخر 2000 صف** · `qdrant.py` اختياري | `implemented` للسجلّ + `partial` للاسترجاع الدلالي |
| **نماذج** | `models/registry.py` (351 سطر) capabilities+cost · `CostGuard` بوابة `$0` صارمة · providers: gemini/openai_compat/mock + `with_retries` | `implemented` — **لا streaming** |
| **حوكمة** | `governance/policy.py` (risk vocabulary) · `core/approval.py` (5 سياسات) · `security/anomaly.py` kill-switch · `provenance/hashchain.py`+`manifest.py` | `implemented` — أقوى طبقة في المشروع |
| **واجهات** | CLI (8 أوامر: ask/chat/skills/tools/serve/approvals/resume/doctor) · FastAPI +20 endpoint + WS dashboard + web UI | `implemented` |
| **CI/تقييم** | `ci.yml` (test/security-blocklist/trivy/zap/sandbox-escape) · `00-integrity.yml` · `arena_diff_eval.yml` (مقاييس + أسرار + **LLM-as-a-Judge** opt-in) | `implemented` — تقييم **على مستوى الـ diff فقط** |
| **Infra** | Dockerfile · docker-compose (redis/qdrant/desktop) · k8s (deployment/hpa) | `configured` |

**الخلاصة الصادقة:** طبقات الحوكمة/الإثبات/الحدود أخبث وأناض من كثير من المشاريع
المماثلة. ما ينقصها هو «العضلات التنفيذية» — وهذا قابل للبناء فوق الأساس الحالي
دون هدم.

---

## 2. الفجوات مقابل منصة وكيل فئة Manus/Claude (بدليل الغياب)

| # | الفجوة | الدليل على الغياب اليوم | الأثر على المنافسة | الأولوية |
|---|---|---|---|---|
| 1 | **لا Terminal/shell حقيقي** | `grep bash\|/bin/sh` في `nimna/` = لا شيء (عدا VNC)؛ `python_exec` يشغّل كود Python بلا جلسة persistent ولا عمليات خلفية | استحالة مهام dev واقعية (بناء/اختبار/git) | 🔴 P1 |
| 2 | **لا تحرير ملفات دقيق ولا بحث محتوى** | `files.py` = list/read/info/write/**delete** فقط؛ الكتابة استبدال كامل؛ لا grep/glob محتوى | مهام برمجية متعددة الملفات = إعادة كتابة كاملة وخطأ | 🔴 P1 |
| 3 | **لا git tool** | لا وجود لـ `git` كأداة مسجلة | لا إصدارات داخل المهمة، لا rollback للوكيل نفسه | 🟠 P1 |
| 4 | **لا خطة طويلة المدى كحالة** | التفكيك upfront في swarm فقط؛ الحلقة reactive بلا artifact خطة تُحدَّث/تُتابَع | ضياع الاتجاه في المهام >10 خطوات | 🔴 P2 |
| 5 | **حدود قصيرة + لا compaction** | `max_steps=12`, `300s`؛ `state.messages` تنمو بلا تلخيص | مهام ساعات مستحيلة؛ رفع الحدود وحدها ينفجر بالسياق | 🔴 P2 |
| 6 | **لا streaming** | `provider.generate()` يعيد `ModelResponse` كاملة | UX متأخر، لا إحساس «وكيل يعمل الآن» | 🟠 P3 |
| 7 | **لا sub-missions معزولة** | swarm يشارك الـ runtime/الحالة، بلا عزل سياق ولا تقارير تلخيص | لا توازٍ حقيقي ولا تكلفة لكل مهمة فرعية | 🟠 P3 |
| 8 | **ذاكرة مسطحة** | `search_memories` LIKE على آخر 2000 صف؛ لا embeddings في المسار القانوني؛ لا سياسة «متى نتذكر» | لا تعلّم تراكمي بين الجلسات | 🟠 P4 |
| 9 | **لا متصفح headless داخلي** | `web.py` نص فقط؛ المتصفح الحقيقي سحابي opt-in | مهام الويب التفاعلية معتمدة على طرف خارجي | 🟡 P3+ |
| 10 | **Arena ليس مختبر وكيل** | `evaluate_arena.py` يقيّم **diff** فقط؛ لا مجموعة مهام قياسية تقيس نجاح الوكيل/تراجعاته | لا يمكن قياس أي تحسين في P1..P6 موضوعياً! | 🔴 P5 (ومعها P0-قياس) |
| 11 | **لا Self-Improvement مُحكم** | مهارة `skill_author` توجد، لكن لا خط أنابيب اقتراح→تجربة→حكم→موافقة→ترقية | المنصة لا تتحسن من مهامها | 🟠 P6 |
| 12 | **لا جدولة/محفزات** | التشغيل من CLI/API فقط؛ لا cron/webhook | لا وكيل يقظ | 🟡 لاحقاً |
| 13 | **مستخدم واحد، لا RBAC** | `SECURITY.md`/blueprint يعترفان (Phase B) | إنتاج مؤسسي لاحق | 🟡 B |
| 14 | **Sandbox ليس حداً أمنياً** | موثّق `partial`؛ subprocess + Docker opt-in، لا microVM | عزل الحَكَم/التجارب التلقائية ضعيف | 🟡 B |
| 15 | **OTel/Prometheus بدون exporter** | audit + metrics summary فقط | لا رصد إنتاجي | 🟡 B |

> ملاحظة منهجية: فجوات P1..P4 هي التي تفصل «شات-بوت بأدوات» عن «وكيل منفّذ».
> فجوة #10 هي **الأهم استراتيجياً**: بدون مختبر مهام، كل تحسين لاحق سيُقاس بالإحساس
> لا بالأرقام — وهذا يناقض روح المستودع (لا PASS وهمي).

---

## 3. المعمارية المستهدفة (نفس المستودع، نفس المبادئ)

نرث **كل** مبادئ `agent-os-blueprint.md` الثمانية (Runtime أولاً، النموذج مورد،
MAX_SPEND=0، SQLite مصدر الحقيقة، Scope قبل execution، المحتوى الخارجي untrusted،
Evidence لا claim، Recovery bounded) ونضيف طبقة واحدة جديدة فوقها:

```text
┌─ Interfaces (موجودة): CLI · FastAPI/WS · Web UI · (+ لاحقاً: Missions API/SSE, Tauri)
│
├─ ★ EVOLUTION LAYER (جديدة — P5/P6)
│    eval suites (tasks/*.yaml) · Arena Runner · LLM-as-a-Judge scorer ·
│    regression ledger · skill-proposal pipeline (اقتراح→تجربة→حكم→موافقة→ترقية)
│
├─ GOVERNANCE (موجودة وتُمدَّد): PolicyEngine · Approvals (mission budgets) ·
│    AnomalyDetector في الحلقة · HashChain evidence · Provenance للأثر (artifacts)
│
├─ ★ COGNITION LAYER (تُعمَّق — P2/P4)
│    TaskPlan artifact (todo_write/read) · Context Compactor ·
│    Sub-missions (عزل سياق + تقارير) · Memory Policies (متى نتذكر/نستدعي)
│
├─ CAPABILITIES (تُمدَّد — P1/P3)
│    Shell (جلسة persistent داخل sandbox) · Edit/Patch · Grep/Glob · Git-safe ·
│    Artifacts (تغليف مخرجات) · Browser headless (لاحقاً) · الموجود كله كما هو
│
├─ RUNTIME (موجود ويُرقَّى بحذر — P2/P3): حلقة `_drive` + وضع plan-driven +
│    checkpoints دورية · (P3) streaming events + sub-mission scheduler
│
└─ MEMORY (موجودة وتُرقَّى — P4): SQLite canonical (يبقى!) + FTS5 فوراً ثم
     embeddings دلالية · compaction-to-memory (خلاصة الجلسة تُرقى لذاكرة)
```

**القرارات المعمارية الثابتة:**

1. **لا استبدال لأي ملف core** — الترقية بالامتداد (`_drive` يكتسب فروعاً جديدة
   خلف flags، والأدوات الجديدة تسجَّل في نفس `ToolRegistry`).
2. **كل أداة تنفيذية جديدة = risk vocabulary + policy + audit event + tests** من
   اليوم الأول (Definition of Done الحالي).
3. **SQLite يبقى مصدر الحقيقة**؛ FTS5/embeddings طبقة استرجاع فوقه (نفس مبدأ Qdrant).
4. **كل ما هو تجريبي (self-improvement, swarm-isolation) خلف موافقة بشرية + Arena
   score** — لا ترقية تلقائية أبداً.
5. **الأثر (Artifacts) لها provenance**: كل ملف ينتجه الوكيل يُبصَم في hash chain.

---

## 4. خريطة الملفات/packages — الإضافة والتعديل والإبقاء

| المسار | عملية | الغرض | المرحلة |
|---|---|---|---|
| `nimna/tools/builtin/shell.py` | **إضافة** | جلسة bash داخل sandbox (subprocess→docker)، مخارج مقصوصة، timeout، background jobs محدودة | P1 |
| `nimna/tools/builtin/edit.py` | **إضافة** | `edit_file` (استبدال دقيق old/new) + `apply_patch` (unified diff مع تحقق) | P1 |
| `nimna/tools/builtin/search.py` | **إضافة** | `grep` محتوى + `glob` أسماء (stdlib، حد نتائج) | P1 |
| `nimna/tools/builtin/git_tool.py` | **إضافة** | git-safe: status/diff/add/commit/log فقط؛ **deny** push/remote/clean | P1 |
| `nimna/tools/builtin/artifacts.py` | **إضافة** | تغليف مخرجات المهمة (zip/manifest + hashchain) | P2 |
| `nimna/core/plans.py` | **إضافة** | `TaskPlan` (pydantic) + أدوات `todo_write/todo_read` + تكامل `_drive` (وضع plan-driven خلف `AGENT_PLAN_MODE`) | P2 |
| `nimna/core/compact.py` | **إضافة** | Context Compactor: تلخيص الرسائل القديمة فوق عتبة tokens، مع حفظ الخلاصة في audit/ذاكرة | P2 |
| `nimna/core/submission.py` | **إضافة** | Sub-mission: تشغيل `Agent` طفل بحدود وسياق معزول، يرجّع تقريراً | P3 |
| `nimna/providers/streaming.py` (+تغيير بروتوكول `ModelProvider`) | **إضافة/تعديل متوافق** | `generate_stream()` اختياري (default يبقى الكامل — لا كسر للمزودين الحاليين) | P3 |
| `nimna/memory/fts.py` ثم `nimna/memory/embeddings.py` | **إضافة** | FTS5 فوراً (stdlib sqlite)؛ embeddings لاحقاً (Provider boundary) | P4 |
| `nimna/memory/policies.py` | **إضافة** | سياسات كتابة ذكريات تلقائية post-mission (استخراج حقائق، توافق دفتر الذاكرة) | P4 |
| `evals/tasks/*.yaml` + `evals/README.md` | **إضافة** | مجموعة مهام قياسية (مدخلات + أدوات مسموحة + أدلة متوقعة + rubric حَكَم) — تبدأ 10 مهام | P0.5/P5 |
| `scripts/run_arena_suite.py` | **إضافة** | تشغيل المجموعة على الوكيل (mock/live) + تسجيل نتائج + تقرير Markdown | P5 |
| `evals/ledger/` (SQLite) | **إضافة** | دفتر نتائج تاريخي: run → score → إصدار → مقارنة | P5 |
| `nimna/evolution/proposals.py` | **إضافة** | خط أنابيب اقتراح التحسين (postmortem → candidate skill/prompt patch) | P6 |
| `.github/workflows/arena_eval_suite.yml` | **إضافة** | تشغيل المجموعة عند PR (advisory) مع تعليق score | P5 |
| `nimna/core/agent.py`, `config.py`, `governance/policy.py`, المصفوفات، `SECURITY.md` | **تعديل تدريجي** | فروع خلف flags + قيم افتراضية آمنة + صفوف قدرة جديدة **فقط عند الهبوط** | كل مرحلة |
| كل ما عدا ذلك | **إبقاء** | الـ baseline لا يُمس | — |

---

## 5. الأنظمة السبعة — الوضع → الهدف → العقد → بوابة الإثبات

### 5.1 Agent Loop
- **الآن:** ReAct محكوم + verify واحد نهائي + suspend/resume (أدلة §1).
- **الهدف:** نفس الحلقة + (أ) وضع plan-driven يقرأ `TaskPlan` كل خطوة، (ب)
  checkpoints دورية لاستئناف بعد crash لا فقط بعد موافقة، (ج) تكامل AnomalyDetector
  كحاجز مباشر، (د) hooks بثّ الأحداث.
- **العقد:** `Agent.run()` توقيعه لا يتغير؛ الجديد عبر `Settings` flags.
- **الإثبات:** اختبارات الحواجز الحالية لا تنكسر + اختبارات جديدة لكل فرع + audit
  events جديدة (`plan_update`, `checkpoint`, `compact`).

### 5.2 Planning
- **الآن:** SkillSelector + swarm decomposition upfront (§1).
- **الهدف:** `TaskPlan` حالة معلنة (خطوات، حالة كل خطوة، معايير إنجاز) يحدّثها
  النموذج بأدوات `todo_*`؛ الحلقة تُذكّر بالخطة في system prompt؛ الانحراف يُسجَّل.
- **العقد:** `TaskPlan(BaseModel)` في `core/plans.py` + أدواتتان في `ToolRegistry`
  (risk=safe) + صفّان في المصفوفتين.
- **الإثبات:** اختبار مهمة متعددة خطوات offline (mock provider) تنشئ/تحدّث/تُكمل
  خطة، واختبار تجاوز حدود الخطة.

### 5.3 Memory
- **الآن:** SQLite canonical + LIKE (§1).
- **الهدف:** FTS5 فوراً (استرجاع كلمات حقيقي بترتيب صلة)، ثم embeddings عبر
  Provider boundary؛ سياسات «متى نكتب ذكرى» post-mission؛ compaction يرقّي الخلاصة
  لذاكرة kind=`episode`.
- **العقد:** `MemoryStore.search_memories` توقيعها ثابت (الترقية داخلية) + دوال
  جديدة `remember_if_worthy`, `promote_episode`.
- **الإثبات:** اختبار استرجاع صلة (FTS يتفوق على LIKE)، اختبار سياسة الرفض/القبول،
  `verify_audit_chain` يظل PASS.

### 5.4 Tools (+Terminal)
- **الآن:** 26+ أداة آمنة النطاق (§1).
- **الهدف:** `shell` (جلسة bash داخل sandbox مع cwd=workspace، timeout، اقتصاص
  مخرجات، سجل أوامر، risk=confirm افتراضياً، denylist عبر PolicyEngine، خيار
  background محدود العدد)، `edit_file/apply_patch` (فشل واضح إذا لم يجد old_text،
  حد حجم)، `grep/glob` (حد نتائج + استبعاد binary)، `git-safe` (لا شبكة، لا
  history-destroying).
- **الإثبات:** لكل أداة: اختبار offline للعقد + اختبار رفض (سياسة/خارج jail) +
  صف مصفوفة + audit event. **حَكَم الأداة:** يجرّبها Arena suite على مهام برمجية.

### 5.5 Browser
- **الآن:** Browser Use Cloud V4 (opt-in, budget-gated, stop-in-finally) + VNC computer (partial) + web نصي.
- **الهدف (P3+):** أداة `browse` headless داخلية (playwright في حاوية، نفس بوابات
  browser_use: disabled افتراضياً، budget=0، confirm)؛ تبقى Cloud V4 للمهام الثقيلة.
- **الإثبات:** نفس نمط `browser/cloud_v4.py`: contract tests + `AVAILABLE_OPT_IN`
  بلا حساب.

### 5.6 Verification
- **الآن:** verify نهائي واحد + حواجز + حَكَم diff (§1).
- **الهدف:** تحقق متدرج: (أ) تحقق أدواتي (هل الملف أُنشئ؟ هل الاختبار مرّ؟) مدمج في
  وضع plan-driven لكل خطوة، (ب) الحَكَم (LLM-judge الموجود!) على مخرجات المهمة،
  (ج) Arena suite كتقويم دوري.
- **الإثبات:** mission تنتهي بـ FAIL تحقُّقي عند عدم تحقق معيار إنجاز خطوة (اختبار
  offline مصمم ليكسر المعيار عمداً).

### 5.7 Interfaces
- **الآن:** CLI/FastAPI/WS (§1).
- **الهدف (P3):** `POST /api/missions` (long-running) + SSE/WS streaming للأحداث +
  `/api/missions/{id}/plan` لعرض الخطة الحية + resume من checkpoint.
- **الإثبات:** اختبارات API الحالية لا تنكسر + contract tests للجديدة.

---

## 6. Self-Improvement الآمن (P6)

**القاعدة:** الوكيل يقترح، ولا يثبّت أبداً بنفسه. ثلاثة أقفال: Sandbox + Arena + Human.

```text
مهمة تنتهي → Postmortem (audit + evidence + judge notes)
   → Proposal: مرشح واحد فقط لكل دورة:
       (أ) مهارة جديدة/محدّثة (SKILL.md)
       (ب) prompt/system patch مقيد
       (ج) preset معاملات أداة
   → Trial: تشغيل subset من evals/tasks داخل sandbox (mock أو live-budgeted)
   → Gate 1: Arena score ≥ baseline score (لا تراجع)
   → Gate 2: static gates (أسرار/مسارات حساسة — evaluate_arena.py الموجود)
   → Gate 3: موافقة بشرية عبر machinery الموافقات الموجودة أصلاً
   → Promote: PR عادي على skills/ (git history = rollback)
   → Ledger: hashchain يسجل الدورة كاملة
```

- المرشح لا يكتب في `skills/` مباشرة أبداً — يكتب في `workspace/proposals/` (خارج
  الحزمة)، والترقية commit بشري.
- كل دورة مقيدة بـ `max_proposal_cycles` وcost budget عبر CostGuard الموجود.

---

## 7. Arena يتحول إلى مختبر تطور (P5 — ومحوره قياس من P0.5)

| المحور | اليوم | المستهدف |
|---|---|---|
| تقييم PR diff | ✅ `arena_diff_eval.yml` (static + judge) | يبقى كما هو |
| مهام الوكيل | ❌ لا شيء | `evals/tasks/*.yaml` (تبدأ 10: 4 برمجية/2 ويب/2 بيانات/2 عربية طويلة) |
| التشغيل | — | `scripts/run_arena_suite.py --mode mock\|live` → تقرير Markdown + JSON |
| الحَكَم | judge على diff | نفس `run_llm_judge` كمصحح rubric لكل مهمة (PASS/FAIL/score/أدلة) |
| الذاكرة | — | `evals/ledger`: كل run → score/model/version، استعلام تراجع (regression) |
| CI | PR comment | `arena_eval_suite.yml` advisory أولاً؛ بوابة `--strict` اختيارية بعد ثبات القياس |
| مقارنة النماذج | — | نفس المجموعة × نماذج registry → جدول score/cost/time (قرار توجيه مدفوع بالأرقام) |

**قاعدة الأمانة تنطبق:** نتائج mock = `MOCKED` بلافتة؛ بلا judge key = `SKIPPED`؛
فشل تشغيل = `ERROR`. لا score وهمي يدخل الـ ledger أبداً.

---

## 8. خارطة الطريق المرحلية (بدون تدمير الـ baseline)

| المرحلة | الاسم | التسليمات | بوابة الخروج (Evidence) | الاعتماد |
|---|---|---|---|---|
| **P0.5** (هذا الأسبوع) | **قياس قبل كل شيء** | 10 مهام `evals/tasks/` + `run_arena_suite.py` بوضع mock + ledger + baseline score موثق | suite يعمل offline، ledger يسجل MOCKED score، لا تراجع في 110 اختبار | — |
| **P1** | النواة التنفيذية | `shell.py`, `edit.py`, `search.py`, `git_tool.py` + policy rows + 4 صفوف مصفوفة | كل أداة: عقد+اختبارات+audit؛ مهمةArena برمجية واحدة تمر live بحدود افتراضية | P0.5 |
| **P2** | التخطيط والأفق الطويل | `plans.py` + todo tools + وضع plan-driven + `compact.py` + `artifacts.py` | مهمة 20+ خطوة offline تنجح بلا انفجار سياق؛ verify لكل خطوة | P1 |
| **P3** | Missions & Streaming | `/api/missions` + SSE + `submission.py` + streaming provider اختياري | API contract tests؛ مهمة طويلة قابلة للاستئناف من checkpoint | P2 |
| **P4** | الذاكرة الدلالية | FTS5 → embeddings + memory policies + promote_episode | استرجاع صلة مُقاس؛ audit chain سليم؛ ذكريات تلقائية مُختبرة | P2 |
| **P5** | Arena Lab كامل | مهام live + judge rubric + regression gate + workflow | baseline score يُنشر؛ أي تراجع يُعلَّم في PR | P1 (ويفيد كل ما بعده) |
| **P6** | Self-Improvement | proposals pipeline بالأقفال الثلاثة | دورة كاملة موثقة بhashchain تنتهي بـ PR مرفوض/مقبول تجريبي | P5 |
| **B** (موازٍ، من blueprint) | إنتاج | Postgres, OTel exporter, microVM sandbox, RBAC/OIDC | كما في blueprint المرحلة B | حسب الحاجة |

**التراجع:** كل مرحلة PR مستقل إلى main؛ rollback = revert؛ لا مرحلة تغيّر سلوكاً
افتراضياً (كل القدرات الجديدة opt-in خلف env/flags حتى تثبت في Arena).

---

## 9. التعليمات التنفيذية للـ coding agent (تذاكر P0.5 وP1 — ابدأ بها عند الموافقة)

> كل تذكرة تتبع Definition of Done: **code + boundary + offline test + policy +
> audit event + matrix rows**، وتُسلَّم PR مستقل مع تشغيل `pytest` (متوقع ≥ الموجود)
> و`verify_capabilities.py` → PASS.

### P0.5-T1 — بنية المجموعة والعدّاء
- أنشئ `evals/tasks/*.yaml` (10 مهام: id, category, prompt, allowed_tools, max_steps, expected_artifacts, judge_rubric).
- أنشئ `scripts/run_arena_suite.py` (stdlib + يعيد استخدام evaluate_arena.py للبوابات الثابتة): يشغّل `Agent` بمزود mock على كل مهمة، يجمع outcome، يصدر Markdown/JSON، حالات MOCKED/SKIPPED/ERROR بلافتات (نفس عقد الأمانة).
- `evals/ledger/` SQLite: run_id, task_id, mode, model, score, verdict, ts, repo_version.
- **Evidence:** `tests/test_arena_suite.py` (تحليل YAML، عدّاء mock، بلافتات، ledger)؛ `python scripts/run_arena_suite.py --mode mock` يعمل ويطبع baseline؛ صف G15 في المصفوفتين.

### P1-T1 — أداة Shell
- `nimna/tools/builtin/shell.py`: `run_command(command, timeout_s≤60, background=false)` — تنفيذ داخل sandbox backend الحالي (subprocess الآن)، cwd=workspace jail، اقتصاص مخرجات (stdout/stderr)، ختم زمني، risk=`confirm` افتراضياً + `effective_risk` يرفع إلى `dangerous` لأنماط (`rm -rf /`, `sudo`, `curl|sh` — يتكامل مع `security/anomaly.py`).
- تسجيل في bootstrap + policy row + `SHELL_TOOL_ENABLED=false` افتراضياً.
- **Evidence:** اختبارات: عقد التسجيل، رفض خارج jail، timeout، اقتصاص، أنماط deny، audit event `tool.call.shell`؛ تحديث CAPABILITY/VERIFICATION rows؛ مهمة Arena برمجية (P0.5) تُعاد فتُقاس.

### P1-T2 — تحرير وبحث
- `edit.py`: `edit_file(path, old_text, new_text, expect_once=true)` — فشل صريح إذا old_text غير موجود/متكرر؛ `apply_patch(diff)` بتحقق مسارات داخل jail؛ حد ملف 1MB.
- `search.py`: `grep(pattern, path, max_results=50)` (regex، يستبعد binary) + `glob(pattern, path)`.
- **Evidence:** اختبارات فشل/نجاح/حدود/jail + صفوف مصفوفة + مهمتا Arena برمجيتان تُحدَّثان للاستفادة.

### P1-T3 — Git آمن
- `git_tool.py`: `git_status/git_diff/git_add/git_commit/git_log` فقط — registry-level deny لأي subcommand آخر (لا push/remote/reset/clean)، هوية commit من env مُعلن، رسالة commit تُسجل في audit.
- **Evidence:** اختبار deny شامل + اختبار دورة commit محلية داخل workspace + صف مصفوفة.

### تعليمات عامة دائمة للمراحل القادمة
1. لا تلمس ملفاً في `nimna/core/` إلا بفرع خلف flag وقيمة افتراضية آمنة.
2. لا صف `implemented` في أي مصفوفة قبل اختباره offline.
3. كل PR: `pytest` كامل + `verify_capabilities.py` + تعليق Arena (الموجود) بدون FAIL جديد.
4. القدرات الخارجية (شبكة/متصفح) تبقى `available_opt_in` بلا مفاتيح في CI.
5. أي سر يظهر في diff = إيقاف فوري (بوابة الأسرار الموجودة ستلقطه — وهذا مقصود).

---

## 10. المخاطر والثوابت

| خطر | التخفيف |
|---|---|
| shell tool تفتح سطح هجوم | disabled افتراضياً + confirm + anomaly patterns + sandbox backend + budget runtime |
| تضخم النطاق (feature creep) | مراحل ببوابات خروج صارمة؛ ما لا يقاس في Arena لا يُدمج |
| قياس Arena متحيز لحَكَم LLM | rubric معلنة في YAML + أدلة متوقعة مصطادة (artifacts) + نتائج MOCKED معلّمة + مقارنة baseline |
| انفجار تكلفة live evals | CostGuard موجود + budget لكل suite + mock افتراضياً في CI |
| إهمال الديون الحالية (B-phase) | مرحلة B تبقى موازية إلزامية قبل أي claim إنتاجي |

**الثوابت غير القابلة للتفاوض (موروثة):** لا PASS وهمي · UNKNOWN لا يُرقّى · SQLite
مصدر الحقيقة · scope قبل execution · المحتوى الخارجي untrusted · كل claim له دليل.
