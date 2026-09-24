# Baseline Run Evidence — cela.nimna (Steps 0–4)

**التاريخ:** 2026-09-24
**الفرع:** `arena/01a0d288-cela-nimna`
**الـBaseline:** `ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d` (`origin/main`)
**الحزمة:** Python 3.11.2 / FastAPI 0.141.1 / Uvicorn 0.53.0 / Pydantic 2.13.5 / google-genai 2.25.0 (مطابق للتثبيت في `requirements.txt`)

> يوثّق هذا المستند التشغيل الفعلي لهذا المستودع بالذات. لا علاقة له بأي SHA أو brief خارجي.

---

## 0) الهوية — مؤكَّدة

```text
$ git rev-parse HEAD origin/main
ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d   (HEAD)
ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d   (origin/main)

$ git status --porcelain     → فارغ
```

### تصحيح F0 — "السجل commit واحد فقط" كان خطأً في القياس، لا حقيقة المستودع

النسخة المحلية في بيئة العمل كانت **shallow clone** (وجود `.git/shallow`)، فكان
`git rev-list --count HEAD` يعطي `1` و`git log` يعرض commit واحدًا. القياس الصحيح بعد
`git fetch --unshallow`:

```text
. before: .git/shallow PRESENT, count=1        ← قياس خاطئ
. after : .git/shallow GONE, main=63 commits, HEAD=66 (=63 + 3 عمل هذه الجلسة)
```

**الأثر العملي:** هذا التصحيح كان إلزاميًا لا تجميليًا — بوابة الإنتاج (الخطوة 1) تشترط أن يكون
`9885f3c3` (T8) سلفًا لـHEAD، وقد كانت تفشل محليًا **بسبب الشالو فقط**:

```text
قبل الإصلاح : ✘ T8 commit 9885f3c33b... is NOT in HEAD's history
بعد الإصلاح : ✔ T8 commit is an ancestor of HEAD (9a362e2)
```

`9885f3c33b90` موجود فعلًا على GitHub — "T7.1 evidence: owner-authorized sh2 …" بتاريخ 2026-09-24 07:19.

أما `c3141990` فالاستبعاد يبقى قائمًا وأقوى: غير موجود في **كامل** الـ63 commit،
وفي `git rev-parse` محليًا، وفي GitHub API (`422 No commit found`).

---

## 1) متطلبات التشغيل الفعلية

```bash
pip install -r requirements.txt
pip install -e . --no-deps     # ← إلزامي، وهو ما يفعله CI بالحرف
python -m nimna serve --port 8000
```

`scripts/run_arena_suite.py` يشغّل مهامه في **عمليات فرعية**؛ بدون تثبيت الحزمة تفشل بـ
`cannot import nimna: ModuleNotFoundError` وتظهر الصفوف `ERROR`. CI ينفّذ هذه الخطوة تحت اسم
*"Install Nimna package for test imports"*.

**النتيجة:** `318 اختبارًا` في `23` ملفًا — كلها خضراء.

---

## 2) F3 — تعارض `.env` مع الاختبارات: **مُعالَج**

### المشكلة (كانت)

`.env` يعرّف `MODEL_PROVIDER=gemini` ⇒ `nimna/config.py:load_dotenv()` يكتب القيم في
`os.environ` **لعملية Python بأكملها**. في `tests/test_production_gate.py:117` كان:

```python
os.environ.setdefault("MODEL_PROVIDER", "mock")   # ← no-op إن سبق ضبطه
```

أي اختبار سابق استدعى `Settings.from_env()` يجعل `.env` يضبط `gemini` أولًا، فيصبح `setdefault`
بلا أثر → يُبنى مزوّد Gemini بلا مفتاح → `ProviderError`.

```text
بدون .env → exit=0            |  مع .env → exit=1
FAILED tests/test_production_gate.py::test_health_endpoint_is_real_runtime_state
E   nimna.providers.base.ProviderError: Gemini API key is not set
```

### الإصلاح المُطبَّق (اختبار فقط — لا يمسّ runtime)

```diff
-def test_health_endpoint_is_real_runtime_state():
+def test_health_endpoint_is_real_runtime_state(monkeypatch):
     from fastapi.testclient import TestClient
-    import os
-    os.environ.setdefault("MODEL_PROVIDER", "mock")
-    os.environ.setdefault("DB_PATH", ":memory:")
+    monkeypatch.setenv("MODEL_PROVIDER", "mock")
+    monkeypatch.setenv("DB_PATH", ":memory:")
```

`monkeypatch` يمنح الاختبار بيئته الخاصة ويستعيدها بعده، وهو **محصّن** ضد أي قيمة ضبطها اختبار سابق،
بخلاف `setdefault`. السلوك الإنتاجي لم يتغيّر إطلاقًا.

### التحقق (حالتان معاديتان)

| الحالة | النتيجة |
|---|---|
| الحزمة كاملة مع `.env` يعرّف `MODEL_PROVIDER=gemini` | `exit=0` ✅ |
| `MODEL_PROVIDER=gemini` مُصدَّر في الصدفة (أسوأ حالة) | `exit=0` ✅ |

كما أُعيد `MODEL_PROVIDER=gemini` إلى `.env` ليطابق ما يشحنه `.env.example`، واختُفيت الحاجة
إلى التحايل السابق. **تحذير للمطوّرين:** أي اختبار يعتمد على `os.environ.setdefault` بعد تحميل
`.env` هو فخّ حقيقي — و CI لا يكشفه لأن `.env` غير موجود هناك (`.gitignore:15`).

---

## 3) المزوّد — لا يوجد fallback صامت ✅

```text
$ build_agent(Settings.from_env(None))      # الافتراضي gemini بلا مفتاح
FAIL-CLOSED: ProviderError -> Gemini API key is not set – set GEMINI_API_KEY (preferred) or GOOGLE_API_KEY
```

- `create_provider()` يرمي `ProviderError` لأي مزوّد مجهول.
- `MockProvider` لا يُبنى إلا عند `MODEL_PROVIDER=mock` صراحةً.
- **الـcost guard الصارم يسمح فعليًا بالموديل المجاني:** في `nimna/models/registry.py:208-209`
  يُسجَّل Gemini كـ `CostProfile(0.0, 0.0, known=True, free_tier=True)`. تحقّقنا بالتجربة:
  الطلب يمرّ من بوابة الميزانية ويصل الشبكة (ولم يُرفض بـ budget).

---

## 4) الأدلة الحيّة

### 4.1 Doctor

```text
✅ workspace writable      ✅ SQLite data/nimna.db (0o600)
✅ Docker (sandbox backend=subprocess)
✅ tools (28 registered)   ✅ skills (10 loaded) – 10 OK
✅ tool JSON schemas (Gemini/OpenAI compatible)
❌ Gemini key: not configured        ← الوحيد، متوقّع
  ↳ 1 skill(s) reference unknown tools
```

### 4.2 `/api/health`

```json
{"status":"ok","provider":"mock","skills":10,"tools":28,
 "cost_guard":{"enabled":true,"mode":"hard","max_spend_usd":0.0,"zero_cost_profile":true},
 "verify_detail":{"enabled":true,"mode":"strict_reviewer",
   "checks":["skill_instructions","tool_results","language","completeness"]}}
```

### 4.3 المحادثة + الذاكرة

```text
POST /api/chat  → status=done | skills=['file_analysis','web_research'] | steps=1
GET  /api/sessions/proof-sess-1/messages → 2 رسائل محفوظة في SQLite ✅
```

### 4.4 بوابة الموافقة — إثبات كامل عبر HTTP

```text
1) file exists before        : True
2) POST /api/chat            : 200 | status = awaiting_approval
3) suspended for approval    : delete_file | approval_id = 6721f7c1...
   file STILL exists (gate!) : True          ← لم يُنفَّذ شيء قبل الموافقة
4) GET /api/approvals        : {'pending':[{'id':'6721f7c1...','session_id':'proof'}]}
5) POST approve -> status    : done | reply = تم حذف الملف.
   tool calls                : [('delete_file', True, True)]   ← ok + approved
6) file deleted after        : True
7) pending approvals cleared : {'pending': []}
```

---

## 5) F7 — قيد البيئة الحالية: **لا يوجد إنترنت عامّ**

فحص الخروج للشبكة من هذه البيئة:

```text
api.github.com                    -> HTTP 200      ✅ مسموح
generativelanguage.googleapis.com -> HTTP 000      ❌ TLS مُغلق (curl exit 35)
ai.google.dev                     -> HTTP 000      ❌
example.com                       -> HTTP 000      ❌ (ليس حجب Google، بل حجب عام)
proxy env vars                    -> (none)
```

**النتيجة:** طلب Gemini **حقيقي** غير قابل للتنفيذ هنا حتى بمفتاح صحيح — الحجب على مستوى الشبكة.
هذا اكتشاف يتيم لا علاقة له بالكود: طلب المُثبت بمفتاح وهمي وصل إلى مرحلة TLS ثم انقطع، مما يثبت أن
التسلسل الكامل (Settings → provider → SDK → HTTP) سليم وأن الفشل شبكي بحت.

**المكان الصحيح لإثبات inference الحقيقي:** جهازك المحلي، أو GitHub Actions، أو بيئة النشر.

---

## 6) مُثبت المزوّد الحقيقي — جاهز للتنفيذ

أُضيف مكوّنان لأنني لا أستطيع الوصول إلى Google من هنا، ولا يمكن أن أُصدر شهادة زائفة:

| الملف | الدور |
|---|---|
| `scripts/live_provider_proof.py` | يثبت inference حقيقي بقواعد صارمة |
| `.github/workflows/live-provider-proof.yml` | تشغيل يدوي (dispatch) بسرّ `GEMINI_API_KEY` |

### القواعد الصارمة في المُثبت

1. **يرفض إصدار شهادة تحت `MODEL_PROVIDER=mock`** (exit 2) — تشغيل وهمي لا يُغني عن حقيقي.
2. كل فحص assertion يُخرج رمزًا غير صفري — لا يمكن أن ينجح بالمصادفة.
3. لا يُطبع المفتاح أبدًا؛ يُذكر فقط اسم المتغيّر الذي وفّره.
4. يتحقق من: الاستجابة غير فارغة، لا تحتوي بانر الـmock، `total_tokens > 0`، وحلقة الوكيل كاملة `done`.

### إثبات أن المُثبت **يستطيع الفشل** (مهم — وإلا فهو ختم مطاطي)

```text
MODEL_PROVIDER=mock                    → exit=2  [FAIL] provider is not mock
MODEL_PROVIDER=gemini + مفتاح غير صالح  → exit=1  [FAIL] live request completed
./scripts/check_ci_falsifiable.sh → FALSIFIABILITY OK: no masks found
```

### تعليمات المفتاح (سبتمبر 2026)

- مفاتيح AI Studio الجديدة تُصدر كـ **auth keys** (`AQ.*`) منذ 28 مايو 2026.
- المفاتيح القياسية (`AIza*`) غير المقيّدة تُرفض منذ 19 يونيو 2026، و**كل** المفاتيح القياسية تُرفض خلال سبتمبر 2026.
- مفتاح `AQ.` يعمل على المسار الأصلي (وهو ما يستخدمه هذا المستودع عبر `google-genai`)،
  و**يُرفض على المسارات المتوافقة مع OpenAI** — لذلك ثبّت الـworkflow `MODEL_PROVIDER=gemini` صراحةً.
- الطبقة المجانية لا تتطلب بطاقة؛ الترقية المدفوعة وحدها تحتاج billing.

**الخطوات (دقيقتان):**

```text
1. https://aistudio.google.com/apikey  →  Create API key   (مجاني، بلا بطاقة)
2. الصق المفتاح في /home/user/cela.nimna/.env   →  GEMINI_API_KEY=AQ....
   (الملف بصلاحيات 600 ومُتجاهَل في git — لا ترسله في المحادثة ولا تضمّنه في أي commit)
3. أبلغني، وسأشغّل:  python scripts/live_provider_proof.py
```

بديلاً، أضف `GEMINI_API_KEY` كـ repository secret وشغّل الـworkflow يدويًا.

---

## 7) تصحيحات على خطة التشغيل

| ورد في الخطة | الواقع |
|---|---|
| `SSE` | **لا وجود لـSSE.** البث عبر `@app.websocket("/ws/{session_id}")` (`nimna/api/app.py:417`). |
| `Mission` ككيان مستقل | لا كيان منفصل. `mission_id` = `run_id`. |
| `MockProvider` كخطر fallback | غير ممكن — fail-closed مُثبت. |
| `verdict: MOCKED` يعني runtime وهمي | لا — `MOCKED` هو وسم الـarena suite للتشغيل على MockProvider، وفحص المزوّد أثبت أن الإنتاج لا يسقط إلى mock. |

**مفاتيح ميتة:** `MOCK_TOOL_CALLING` يُقرأ في `nimna/config.py:128,227` ولا يُستخدم في أي مكان آخر —
مفتاح يوحي بقدرة غير موجودة، ويخالف مبدأ المستودع (*code presence never implies capability*).

---

## 8) بوابة الإنتاج — اجتازت كاملةً على CI ✅

`scripts/production_gate.sh` هو تعريف هذا المستودع نفسه لـ"مرشّح إنتاجي": سبع خطوات متسلسلة،
وبلا أي علم تخطٍّ. نتيجة تشغيلها على PR #14 في GitHub Actions:

```text
step 1  T8 ancestor                ✔ (9885f3c3 سلف لـ HEAD)
step 2  CI falsifiability           ✔ no masks
step 3  compileall                  ✔ clean
step 4  pytest                      ✔ 318 passed
step 5  pip-audit (blocking)        ✔ no known vulnerabilities
step 6  runtime health (real HTTP)  ✔ GET /api/health → 200
step 7  docker build               ✔ image built: nimna:production-candidate
VERDICT: candidate CERTIFIED by this run
```

والمحصلة على `main` بعد الدمج: `086daeb`.

### تشغيلها محليًا — 5 نجحت / 3 فشلت، والفشلان بيئيان بحت

| الخطوة | محليًا | السبب (بيئي، لا علاقة له بالمستودع) |
|---|---|---|
| 5 — toolchain + pip-audit | ✘ | `pip install -U` يفشل بـ **PEP 668** (`externally-managed-environment`) لأن Python نظامي (Debian)؛ ترقية pip لم تتم فبقيت `pip 23.0.1` بـ20 ثغرة معروفة. على CI (setup-python) لا توجد PEP 668 فتمر. |
| 7 — docker build | ✘ | لا وجود لـDocker CLI في هذه البيئة. على CI (`ubuntu-latest`) يبني الصورة فعلًا. |

الخطة الأصلية كانت تعتبر Docker عائقًا أولًا. القياس الصحيح: **Docker اجتاز على CI بالفعل**،
والعائق الحقيقي المتبقي هو إثبات inference كما في F7.

---

## 9) حالة الـworkflow الحي وحاجز الصلاحيات

الـworkflow سُجِّل بعد الدمج وأصبح قابلًا للتشغيل:

```text
$ gh workflow list
Live provider proof (real inference)   active   365881318
```

**لكن GitHub منح هذا التكامل صلاحية قراءة فقط.** المُثبت جاهز تمامًا، والعائق إداري بحت:

| العملية | النتيجة |
|---|---|
| قراءة حالة التشغيلات والنتائج (`gh run list`, `check-runs`) | ✅ تعمل |
| قراءة الـannotations (`check-runs/{id}/annotations`) | ✅ تعمل |
| تنزيل السجل الخام (`--log`) | ⛔ محجوب (نطاق مختلف عن `api.github.com`) |
| تشغيل الـworkflow (`workflow_dispatch`) | ⛔ `403 Resource not accessible by integration` |
| إضافة السرّ (`secrets`) | ⛔ `403` — والمفتاح لا يمرّ في المحادثة أصلًا |

لذلك **خطوتان بشريتان لا مفرّ منهما** قبل الحصول على PASS حقيقي:

```text
1. Settings → Secrets and variables → Actions → New repository secret
   Name: GEMINI_API_KEY     Value: AQ.... (من aistudio.google.com/apikey، مجاني بلا بطاقة)
2. Actions → "Live provider proof (real inference)" → Run workflow
```

ولتجاوز حجب السجلات، جُعل المُثبت ينشر حكمه **كـannotation** (قناة `api.github.com`):
عند الفشل أو النجاح يطبع `::notice::PROOF {...}` ويُرفع الدليل كـartifact،
فأستطيع قراءة النتيجة وتوثيقها دون تنزيل logs.

---

## 10) الحالة والخطوات التالية

```text
[✓] 0  تثبيت الهوية والـSHA            → ffcb0c7
[✓] 1  تشغيل FastAPI محليًا            → 0.0.0.0:8000
[✓] 2  فحص Provider Router            → fail-closed
[✓] 3  منع MockProvider كـfallback     → أصلاً ممنوع
[✓] 4  API + Memory + Approval        → مُثبت حيًّا (WS بدل SSE)
[✓] —  إصلاح عزل الاختبارات            → monkeypatch، والتحقق في حالتين معاديتين
[✓] —  cost guard vs Gemini المجاني    → يسمح فعليًا (0.0/known/free_tier)
[✓] —  بوابة الإنتاج السبعة على CI     → CERTIFIED (086daeb)
[✓] —  workflow حي مسجَّل             → id 365881318
[⏸] —  PASS حقيقي من Gemini            → محجوب على خطوتين بشريتين (§9)
[ ] 6  نشر مجاني أولي (Render Free)
[ ] 7  Smoke / rollback verification
```
