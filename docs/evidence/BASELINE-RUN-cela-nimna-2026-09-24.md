# Baseline Run Evidence — cela.nimna (Steps 0–4)

**التاريخ:** 2026-09-24
**الفرع:** `arena/01a0d288-cela-nimna`
**الـBaseline:** `ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d` (`origin/main`, `HEAD`)
**الحزمة:** Python 3.11.2 / FastAPI 0.141.1 / Uvicorn 0.53.0 / Pydantic 2.13.5

> هذا المستند يوثّق التشغيل الفعلي لهذا المستودع بالذات. لا علاقة له بأي SHA أو بbrief خارجي.

---

## 0) الهوية — مؤكَّدة

```text
$ git rev-parse HEAD origin/main
ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d   (HEAD)
ffcb0c722fb4cc6bc70f43499459fb69f47b5a6d   (origin/main)

$ git status --porcelain
(فارغ)

$ git rev-list --count HEAD
1
```

`c3141990` → `fatal: Not a valid object name` + GitHub API `422 No commit found`.
غير موجود في هذا المستودع ولا في أي ref. **مُستبعَد من هذه السلسلة نهائيًا.**

---

## 1) متطلبات التشغيل الفعلية (مكتشفة بالتجربة)

```bash
pip install -r requirements.txt
pip install -e . --no-deps     # ← إلزامي، وهو ما يفعله CI بالحرف
python -m nimna serve --port 8000
```

`pip install -e .` ليس تجميلًا: `scripts/run_arena_suite.py` يشغّل مهامه في **عمليات فرعية**،
وبدون تثبيت الحزمة تفشل بـ `cannot import nimna: ModuleNotFoundError` وتظهر الصفوف بوصف `ERROR`.
`.github/workflows/ci.yml` ينفّذ هذه الخطوة تحت اسم *"Install Nimna package for test imports"*.

---

## 2) حالة الاختبارات — الأساس الحقيقي

```text
$ python3 -m pytest -q
318 tests / 23 file  →  exit=0   ✅ كاملة خضراء
```

**بشرط ألّا يوجد `.env` يعرّف `MODEL_PROVIDER`.**

### الملاحظة الحرجة F3 — تعارض `.env` مع `test_production_gate.py`

| الحالة | النتيجة |
|---|---|
| بدون `.env` | `exit=0` — كل الـ318 اختبارًا تنجح |
| مع `.env` يحتوي `MODEL_PROVIDER=gemini` | `exit=1` — فشل واحد |

```text
FAILED tests/test_production_gate.py::test_health_endpoint_is_real_runtime_state
E   nimna.providers.base.ProviderError: Gemini API key is not set
```

**السبب الجذري (مُثبت بالتجربة):** `nimna/config.py:load_dotenv()` يكتب قيم `.env` في `os.environ` العام
لعملية Python بأكملها. في `tests/test_production_gate.py:117` يعتمد الاختبار على:

```python
os.environ.setdefault("MODEL_PROVIDER", "mock")   # ← no-op إذا سبق ضبطه
```

أي اختبار سابق استدعى `Settings.from_env()` يجعل `.env` يضبط `MODEL_PROVIDER=gemini` أولًا،
فيتحول `setdefault` إلى no-op، فيُبنى مزوّد Gemini بلا مفتاح → `ProviderError`.

الإثبات المباشر:

```text
start            MODEL_PROVIDER = None
after .env load  MODEL_PROVIDER = gemini   <- .env leaked into global env
after setdefault MODEL_PROVIDER = gemini   <- no-op, already set
RESULT: ProviderError -> Gemini API key is not set
```

### المعالجة المُطبَّقة (بدون لمس أي كود أو اختبار)

```diff
- MODEL_PROVIDER=gemini
+ # MODEL_PROVIDER=gemini   # kept unset on purpose
```

`.env` يبقى موجودًا لحمل المفتاح، و`MODEL_PROVIDER` يُمرَّر عند التشغيل الفعلي.
النتيجة: `exit=0` مع وجود `.env`. ✅

**CI غير متأثر** لأنه لا يوجد `.env` في بيئة CI (الملف في `.gitignore:15`)،
لكن **أي مطوّر محلي** يصطدم بهذا. العِلاج الدائم المقترح (لم يُطبَّق — قرار لك) هو تعديل سطر واحد:

```diff
- os.environ.setdefault("MODEL_PROVIDER", "mock")
+ os.environ["MODEL_PROVIDER"] = "mock"
```

---

## 3) المزوّد — لا يوجد fallback صامت (خطوة 3 محسومة ✅)

```text
$ python3 -c "build_agent(Settings.from_env(None))"   # الافتراضي gemini بلا مفتاح
FAIL-CLOSED: ProviderError -> Gemini API key is not set – set GEMINI_API_KEY (preferred) or GOOGLE_API_KEY
```

- `create_provider()` يرمي `ProviderError` لأي مزوّد مجهول.
- `MockProvider` **لا يُبنى إلا** عند `MODEL_PROVIDER=mock` صراحةً (`nimna/providers/__init__.py`).
- HTTPS مع mock يعطي 200، ومع gemini بلا مفتاح يفشل الإقلاع. لا تدرّج صامت.

**نتيجة:** هذا البند من خطتك محقَّق أصلًا. لا يحتاج إصلاحًا.

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

### 4.2 `/api/health` (حيّ)

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

## 5) تصحيحات على خطة التشغيل

| ورد في الخطة | الواقع |
|---|---|
| `SSE` | **لا وجود لـSSE.** البث عبر `@app.websocket("/ws/{session_id}")` (`nimna/api/app.py:417`). |
| `Mission` ككيان مستقل | لا كيان منفصل. `mission_id` = `run_id` (mission = run). |
| `MockProvider` كخطر fallback | غير ممكن — fail-closed مُثبت. |

### مفاتيح ميتة

`MOCK_TOOL_CALLING` يُقرأ في `nimna/config.py:128,227` و**لا يُستخدم في أي مكان آخر**.
مفتاح يوحي بقدرة لا وجود لها — يخالف مبدأ المستودع نفسه (*code presence never implies capability*).

---

## 6) العوائق الحالية

1. **مفتاح المزوّد:** لا يوجد `GEMINI_API_KEY`. بدونه الإقلاع يفشل مغلقًا (سلوك صحيح).
   الحل: Gemini free tier (بلا بطاقة) → يُلصق في `.env` (موجود الآن، صلاحيات `600`، مُتجاهَل في git).
2. **Docker:** غير مثبّت في هذه البيئة (`which docker` فارغ، لا daemon).
   → الخطوة 5 (بناء الصورة) **غير قابلة للتنفيذ هنا**؛ تحتاج بيئة فيها Docker.

---

## 7) الخطوات التالية

```text
[✓] 0  تثبيت الهوية والـSHA            → ffcb0c7
[✓] 1  تشغيل FastAPI محليًا            → 0.0.0.0:8000
[✓] 2  فحص Provider Router            → fail-closed
[✓] 3  منع MockProvider كـfallback     → أصلاً ممنوع
[~] 4  API + Memory + Approval        → مُثبت (WS بدل SSE)
[ ] 5  بناء Docker                    → محجوب: لا Docker في البيئة
[ ] 6  نشر مجاني أولي
[ ] 7  Smoke / rollback verification
```

**قرارك مطلوب في نقطتين:**
1. تطبيق تعديل السطر الواحد في `tests/test_production_gate.py:117` (عِلاج دائم لـF3)؟
2. هل نُكمل بمفتاح Gemini حقيقي (لإثبات المزوّد الفعلي) أم نكتفي بأدلة mock الموسومة؟
