# Nimna — وكيل ذكي قابل لإعادة استخدام المهارات (Reusable-Skills Agent)

> **الحالة: Release Candidate Sprint 2 — Vector Memory (Qdrant + fallback, 768-dim) + 26 أداة — 196 اختبارًا**
> الاختبارات لا تثبت الأمان المطلق. الحاويات تشترك في **نواة المضيف**؛ أبقِ المضيف وDocker محدثين واستخدم **seccomp/AppArmor/SELinux**. لا تستخدم `subprocess` كعزل أمني، ولا تعتبر `mock` دليل اتصال حقيقي.

وكيل عام يعمل فوق **مفتاح Gemini المجاني** (أو NVIDIA NIM أو أي نموذج OpenAI-compatible) بمكتبة مهارات `SKILL.md` قابلة للتبديل:

```
المستخدم → واجهة (CLI / Web / REST)
  → Planner (يرى الكتالوج المختصر فقط)
  → اختيار المهارات (≤3) + تحميل SKILL.md عند الطلب
  → حلقة النموذج ↔ الأدوات (Pydantic + صلاحيات + تدقيق)
  → موافقة بصرية/يدوية للأدوات الخطرة (تعليق/استئناف + TTL 5د)
  → بوابة الرؤية (Vision Gateway) للتحكم البصري
  → مراجعة Verifier → الرد + حفظ في الذاكرة
```

> **English:** Provider-agnostic skills runtime — skills are `SKILL.md` folders loaded progressively, tools are scoped per skill with Pydantic validation, sensitive tools require human approval (pause/resume, 128-bit id, TTL, session-scoped), memory/audit in SQLite, model layer (Gemini/NVIDIA/OpenAI) swappable. See [Quick start](#quick-start-english).

---

## المحتويات
1. [التشغيل السريع](#التشغيل-السريع)
2. [البنية](#البنية)
3. [المهارات (10)](#المهارات-skills)
4. [الأدوات (26) والصلاحيات](#الأدوات-والصلاحيات)
5. [الذاكرة والتدقيق](#الذاكرة-وسجل-التدقيق)
6. [تبديل المزود](#تبديل-المزود-gemini--nvidia--openai)
7. [واجهة REST & WebSocket](#واجهة-rest--websocket)
8. [Docker والنشر](#docker-والنشر)
9. [التحكم بالكمبيوتر والواجهة المتقدمة](#التحكم-بالكمبيوتر-والواجهة-المتقدمة-beating-manus-2026)
10. [الإعدادات](#الإعدادات)
11. [Agent OS boundaries](#agent-os-boundaries----الإضافة-المعمارية)
12. [الأمان — المراجعة #1](#الأمان--المراجعة-1-أعلى-مخاطرة)
13. [الاختبار والتشخيص](#الاختبار-والتشخيص)
14. [المساهمة](#المساهمة)

---

## التشغيل السريع

```bash
git clone <repo> && cd cela.nimna
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# ضع مفتاحك: GEMINI_API_KEY=...  (https://aistudio.google.com/apikey)

nimna doctor --offline                    # فحص بيئة
nimna skills list
nimna ask "حلّل ملف المبيعات وأنشئ تقريراً"
nimna chat                               # تفاعلي — الموافقات في الطرفية
PORT=8001 nimna serve                    # واجهة + REST  http://localhost:8001
nimna doctor --offline
pytest -q                                # 196 اختبار بلا مفتاح (mock)
```

بدون مفتاح: `MODEL_PROVIDER=mock nimna serve` — ترى اختيار المهارات والأدوات حياً والردود فقط وهمية.

`workspace/sales.csv` جاهز للتجربة: يختار `csv_analysis` + `report_writer` → يفحص الأعمدة → يحسب الإحصاءات → يرسم `Chart.js` → يكتب `workspace/reports/*.md` بدون كتابة فوق الأصلي.

---

## البنية

```
nimna/
├── config.py            # Settings من .env (لا أسرار في الكود)
├── bootstrap.py         # build_agent(): مزود + مهارات + أدوات + ذاكرة
├── providers/
│   ├── base.py          # Message/ToolCall/ToolSpec/ModelProvider + retry 429
│   ├── gemini.py        # Google Gen AI SDK — function calling يدوي + thought signatures + Vision Gateway (Part.from_bytes)
│   ├── openai_compat.py # /chat/completions: NVIDIA/OpenAI/Ollama
│   └── mock.py          # للاختبار بدون مفتاح
├── skills/              # اكتشاف + كتالوج مختصر + ترتيب لفظي + مراجع
├── tools/
│   ├── sandbox.py       # run_python: subprocess (rlimits) أو docker --network none --cap-drop ALL
│   └── builtin/         # files, csv, python, web (SSRF), reports, memory, skills, computer
├── memory/store.py      # SQLite: sessions/messages/memories/audit_log/pending_runs/approvals (TTL, session-scoped, one-shot)
├── memory/qdrant.py     # Qdrant + fallback (Sprint 2): user_context / execution_history / code_knowledge, hash embeddings 768-dim
├── core/
│   ├── planner.py       # اختيار مهارات (LLM JSON + fallback لفظي عربي/إنجليزي)
│   ├── agent.py         # حلقة قابلة للتعليق/الاستئناف + Verifier + كاسر حلقة بصري (hash)
│   ├── approval.py      # DeferToClient / Console / Auto / Callback
│   └── state.py         # RunState (run_id 128-bit) + AgentResult
├── api/
│   ├── app.py           # FastAPI + WebSocket hardened (1MB, ping, rate 10/s)
│   └── static/index.html# لوحة تحكم 3 أعمدة + Computer Use متقدم
└── cli.py               # nimna ask|chat|skills|tools|serve|approvals|resume|doctor
skills/                  # 10 مهارات — أضف مجلد = مهارة جديدة
workspace/               # مساحة عمل مقيدة (كل أدوات الملفات مسجونة داخلها)
tests/                   # 196 اختبار — بلا شبكة (منها 23 تكامل MCP على socket حقيقي)
```

**دورة طلب واحد** ـ «حلّل المبيعات»:
1. حفظ الطلب + آخر 20 رسالة كسياق.
2. **Planner** يرى الكتالوج المختصر فقط → `{"skills":["csv_analysis","report_writer"],"plan":[...]}`. فشل الـ JSON → ترتيب لفظي.
3. تحميل `SKILL.md` للمهارات المختارة + فتح **فقط** `allowed_tools` الخاصة بها (+ `load_skill, read_skill_reference, memory_*`).
4. حلقة نموذج↔أدوات مع تحقق Pydantic؛ الأخطاء تُعاد للنموذج.
5. أداة `confirm/restricted` → تعليق (`AWAITING_APPROVAL`, id عشوائي 128-bit, TTL 5د, حد 5/جلسة) → موافقة بصرية/طرفية → استئناف ذري one-shot.
6. حقن الرؤية: بعد `take_screenshot` تُحمّل الصورة كـ `user_with_image` للدور التالي (Gemini vision).
7. **Verifier** يفحص الجواب مقابل تعليمات المهارة (مرة واحدة، `AGENT_VERIFY=false` لتعطيله).
8. حفظ الرد + `audit_log` كامل.

---

## المهارات (Skills)

كل مهارة مجلد `SKILL.md` — الواجهة الأمامية (front matter) فقط للمخطط، والجسم يُحمّل عند الاختيار (لا تضخم prompt).

```
skills/csv_analysis/
├── SKILL.md
├── references/statistics_guide.md
└── scripts/
```

```yaml
---
name: csv_analysis
display_name: تحليل ملفات CSV
description: تحليل ملفات CSV: اكتشاف الأعمدة، القيم المفقودة، الإحصاءات...
version: 1.0.0
triggers: [تحليل ملف, CSV, analyze csv]
allowed_tools: [list_files, read_csv, calculate_statistics, create_chart]
risk_level: safe
---
## التعليمات
1. افحص الأعمدة بـ read_csv ...
```

- `nimna skills validate` ينبه لأداة غير مسجلة أو مهارة `restricted` تحتاج مراجعة.
- إضافة مهارة = إضافة مجلد + `POST /api/skills/reload`.

### المهارات المضمّنة (10)

| المهارة | الغرض | الأدوات | المستوى |
|---------|-------|---------|---------|
| `csv_analysis` | فحص CSV + إحصاءات + رسم | `read_csv, calculate_statistics, create_chart` | safe |
| `report_writer` | تقرير Markdown في `reports/` | `write_report` | safe/confirm |
| `web_research` | بحث DuckDuckGo + قراءة صفحات بمصادر | `web_search, fetch_url` | safe |
| `python_executor` | تشغيل Python معزول | `run_python` | safe/confirm |
| `file_analysis` | استعراض وتلخيص نصوص | `list_files, read_file, file_info` | safe |
| `skill_author` | تأليف `SKILL.md` جديدة | `write_file, list_skills` | safe |
| `computer_control` | **تحكم بصري معزول VNC** — تصفح/نقر/كتابة | `take_screenshot, get_element_coordinates, mouse_click, type_text, list_files, read_file` | **restricted** |
| `code_execution` | **تنفيذ أوامر/كود** — فصل أمني عن التحكم البصري | `shell_execute, run_python, write_file` | **restricted** |
| `browser_use` | **Browser Use Cloud API V4** — متصفح سحابي opt-in وبميزانية/موافقة | `browser_use_run` | **restricted** |
| `mcp_servers` | **أدوات خوادم MCP** — أدوات خوادم خارجية مُفعَّلة، بنطاق glob وموافقة لكل نداء | `mcp__*__*` | **confirm** |

> فصل `computer_control` عن `code_execution` يمنع خداع الموافقة عبر حقن في صفحة ويب: موافقتك على نقرة لا تمنح تنفيذ shell.

---

## الأدوات والصلاحيات

`nimna tools` → 26 أداة (22 + 3 vector memory + Browser Use V4):

- **safe**: قراءة/حساب — تنفذ مباشرة.
- **confirm/restricted**: تحتاج موافقة (تعليق). `delete_file` دائماً؛ `write_file/report` عند الكتابة فوق موجود؛ `run_python` مع `subprocess`؛ كل أدوات `computer_control`/`code_execution`.

**العزل:**
- كل أدوات الملفات مسجونة في `WORKSPACE_DIR` (ترفض `..`, مسار مطلق, symlink هارب, null bytes).
- `fetch_url/web_search` ترفض SSRF: فحص `hostname` قبل الطلب وبعد كل redirect (≤5)، حظر `localhost/127.0.0.1/0.0.0.0`, الشبكات الخاصة/الحلقة, `.local/.internal`, ودعم `::ffff:127.0.0.1`. كل hop يُحل DNS من جديد (مضاد DNS rebinding) بدون إعادة استخدام اتصال.
- النموذج يرى فقط أدوات المهارات المحمّلة؛ استدعاء غير مسموح → خطأ.

**إضافة أداة:**
```python
from pydantic import BaseModel, Field
from nimna.tools import default_registry, ToolContext
class SendEmailParams(BaseModel): to: str; body: str
@default_registry().tool("send_email","...",SendEmailParams, risk="confirm")
def send_email(p, ctx: ToolContext): return {"sent": True}
# ثم أضفه إلى allowed_tools في المهارة
```

---

## الذاكرة وسجل التدقيق

### Vector Memory — Sprint 2 (Qdrant + fallback)
- **3 collections**: `user_context` (تفضيلات/أنماط), `execution_history` (سجلات Shell للـ Self-Healing), `code_knowledge` (snippets).
- **Embeddings**: `text-embedding-004` (Gemini) مع fallback محلي hash 768-dim L2-normalized — يعمل بلا Qdrant/بلا مفتاح.
- **API**: `POST /api/memory/vector/upsert` + `GET /api/memory/vector/search?q=&collections=&limit=` + `GET /api/memory/vector/health` + `GET /api/health` → `memory:{provider, vector_dim, counts}`.
- **Tools**: `vector_memory_save` / `vector_memory_search` / `vector_memory_health` — للـ LLM والـ Multi-Agent Swarm القادم.
- **Infra**: `docker compose --profile infra up -d qdrant` (6333/6334) + `k8s/qdrant.yaml` + `QDRANT_URL=http://qdrant:6333` (انظر `infra/qdrant/README.md`). Fallback = In-Memory Cosine (0 فقدان وظيفة).

- **قصيرة:** رسائل الجلسة (`session_id`) تُمرر تلقائياً (`AGENT_HISTORY_MESSAGES`).
- **دائمة:** `memory_save/search`؛ نوع `preference` يُحقن في prompt.
- **التدقيق:** `GET /api/sessions/{id}/audit` أو جدول `audit_log` — مهارات، أدوات (وسائط/مدة/نتيجة), موافقات, استهلاك توكنات. الأسرار تُستبدل `***REDACTED***` عبر `redact_payload`.
- **الموافقات:** جدول `pending_runs` + `approvals` — `id` عشوائي 32 hex (128-bit), TTL 300s, حد 5/جلسة, تنظيف تلقائي للمنتهي, تحقق `session_id` عند الحل, واستهلاك one-shot ذري.

---

## تبديل المزود (Gemini / NVIDIA / OpenAI)

```python
class ModelProvider(ABC):
    def generate(self, messages, tools=None) -> ModelResponse: ...
```

| المزود | `.env` |
|--------|--------|
| **Gemini مجاني** | `MODEL_PROVIDER=gemini` `GEMINI_API_KEY=...` `GEMINI_MODEL=gemini-2.5-flash` |
| **NVIDIA NIM** | `MODEL_PROVIDER=openai` `OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1` `NVIDIA_API_KEY=...` `OPENAI_MODEL=meta/llama-3.3-70b-instruct` |
| OpenAI | `OPENAI_BASE_URL=https://api.openai.com/v1` `OPENAI_API_KEY=...` |
| Ollama | `OPENAI_BASE_URL=http://localhost:11434/v1` `OPENAI_API_KEY=ollama` |
| بلا مفتاح | `MODEL_PROVIDER=mock` |

برمجياً:
```python
from nimna import build_agent, Settings
from nimna.providers.openai_compat import OpenAICompatibleProvider
agent = build_agent(Settings.from_env(), provider=OpenAICompatibleProvider(api_key="...", base_url="https://integrate.api.nvidia.com/v1", model="meta/llama-3.3-70b-instruct"))
print(agent.run("لخص الملفات").reply)
```
Gemini يعيد المحاولة تلقائياً عند 429 بتراجع أسي (3 محاولات). عطّل المراجع إذا ضاقت الحصة: `AGENT_VERIFY=false`.

---

## واجهة REST & WebSocket

`PORT=8001 nimna serve` → `http://localhost:8001` (واجهة RTL) + `/docs`

| الطريقة | المسار | الوصف |
|---------|--------|-------|
| GET | `/api/health` | مزود/نموذج/مهارات/أدوات + `verify_detail` + `port` |
| GET | `/api/skills`, `/api/skills/{name}` | كتالوج / مهارة كاملة |
| POST | `/api/skills/reload` | إعادة اكتشاف |
| GET | `/api/tools` | الأدوات + مخططاتها + `risk` |
| POST | `/api/chat` | `{"message":"...","session_id":"?"}` → `AgentResult` (قد يعود `awaiting_approval`) |
| POST | `/api/approvals/{id}?session_id=` | `{"approved":true,"always":false}` — يتحقق من مالك الجلسة (403 إن اختلفت) |
| GET | `/api/approvals?session_id=` | المعلقة (يُنظف المنتهي TTL) |
| GET | `/api/sessions/{id}/messages`, `/audit` | الذاكرة/التدقيق |
| GET | `/api/memories?q=` | الدائمة |
| GET | `/api/computer/status`, `/api/computer/screenshot` | حالة VNC + آخر لقطة + شبكة نيون |
| GET | `/api/workspace/files?path=.` | مستعرض ملفات مقيد |
| WS | `/ws/{session_id}` | بث حي — `1MB` حد، `ping` 30s، حد 10 رسائل/ث، تحقق جلسة |

**دورة موافقة:**
```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' -d '{"message":"احذف old.csv"}'
# → {"status":"awaiting_approval","pending":{"approval_id":"...32hex...","tool_name":"delete_file"}}
curl -s localhost:8001/api/approvals/<id>?session_id=<sid> -H 'Content-Type: application/json' -d '{"approved":true}'
# → {"status":"done","reply":"تم الحذف"}
```

---

## Docker والنشر

```bash
cp .env.example .env  # chmod 600 .env
docker compose up --build                          # آمن: subprocess (يطلب موافقة)
docker compose --profile local-sandbox up --build  # عزل Docker حقيقي (يحتاج docker.sock)
docker compose --profile computer up -d desktop    # سطح مكتب معزول  http://localhost:6901
```

- `skills/` و`workspace/` كـ volumes.
- **تحذير:** تركيب `/var/run/docker.sock` يعادل تحكم مضيف شبه كامل — الخدمة الافتراضية **لا تركّبه**. استخدم `local-sandbox` فقط محلياً بلا بيانات حساسة، أو شغّل خارج Docker، أو proxy محدود.
- في وضع `docker` يعمل `run_python` في `python:3.11-slim` بلا شبكة (`--network none`), `cap-drop ALL`, `no-new-privileges`, حدود ذاكرة/CPU.
- **الإنتاج:** Docker rootless/Podman + مستخدم غير root + `cap-drop ALL` + `no-new-privileges` + seccomp/AppArmor/SELinux.

---

## التحكم بالكمبيوتر والواجهة المتقدمة (Beating Manus 2026)

### 1) البنية المعزولة
`dorowu/ubuntu-desktop-lxde-vnc:focal` على `http://localhost:6901` (noVNC). لا يلمس المضيف.

### 2) الأدوات وشبكة الإحداثيات + كاسر الحلقة
- `take_screenshot` (safe) — يلتقط PNG، يضيف شبكة نيون شفافة كل 200×100، يحفظ في `workspace/.screenshots/` (TTL 1h, حد 80 ملف)، يحقن تلقائياً كـ `user_with_image` عبر بوابة الرؤية (Gemini `Part.from_bytes` / OpenAI `image_url`).
- `get_element_coordinates(element_name)` (safe) — OCR (`pytesseract` إن وجد) + خريطة heurist (`firefox→140,140`, `حفظ→640,400`)، يعيد `x,y,confidence` وصورة مُعلّمة `locate-*.png` بنقطة حمراء — يحل توهان الإحداثيات.
- `mouse_click(x,y,button,clicks,purpose)` / `type_text(text,submit)` (confirm, restricted) — يظهر **نقطة حمراء** متوهجة في Mirror View قبل التنفيذ.
- `shell_execute` **منفصل** في `code_execution` — لا يمر عبر التحكم البصري.
- **كاسر حلقة:** hash بصري 16×16 لكل لقطة؛ إذا تكررت 3 متتالية → تحذير عربي + `audit screenshot_loop_detected`.

### 3) الواجهة المتقدمة — 3 مناطق
**القائمة الجانبية:** المهام / الذاكرة / المشاريع / الملفات / الإعدادات — تنقل سلس للسياق.

**صندوق المحادثة والأوامر (يسار):** يعرض **سلسلة تفكير** (Chain of Thought) بالتزامن مع الإجراءات.

**العرض المركزي — Advanced Computer Use Display (يتفوق على Manus):**
- **بث حي مع Neon Bounding Boxes:** مربعات نيون متوهجة (`#00ffa3` / `#00d4ff` مع `box-shadow 0 0 28px`) حول الأزرار/الحقول التي يقرأها الوكيل لحظياً.
- **سجل الإجراءات الزمني Overlay:** شريط أفقي يضيف `📸 التقاط` → `🎯 تحديد` → `🖱️ النقر على زر الإرسال` → `⌨️ إدخال` → `💻 تنفيذ` حياً.
- **نافذة Terminal Logs:** لوح سفلي `mono 11px` يعرض `stdout/stderr` لـ `shell_execute` مع شفافية كاملة للمطور.
- **AI Cursor مخصص:** سهم أبيض + نقطة نيون، حركة `0.42s cubic-bezier` مع `ripple` عند النقر — بلون مختلف عن مؤشر المستخدم.

> جرب: `nimna ask "افتح المتصفح وابحث عن الطقس"` — سترى التقاط → تحديد نيون → نقطة حمراء للموافقة → تحقق بلقطة جديدة، مع timeline وterminal حياً.

---

## الإعدادات

كلها في `.env.example`:

| المتغير | افتراضي | المعنى |
|--------|---------|--------|
| `MODEL_PROVIDER` | `gemini` | `gemini`/`openai`/`mock` |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | — | المفاتيح (تُقرأ `GEMINI_API_KEY` أو `GOOGLE_API_KEY`) |
| `AGENT_MAX_STEPS` | 12 | جولات نموذج |
| `AGENT_MAX_TOOL_CALLS` | 30 | استدعاءات أدوات |
| `AGENT_MAX_RUNTIME_SECONDS` | 300 | حد زمني |
| `AGENT_MAX_RESPONSE_TOKENS` | 4096 | سقف توكن |
| `COST_GUARD_ENABLED` / `COST_GUARD_HARD` | true / true | بوابة تكلفة قبل طلب النموذج |
| `MAX_SPEND_USD` | 0 | الحد الصلب الافتراضي؛ unknown/paid يُحظر |
| `MODEL_COST_*_USD_PER_1K` | — | أسعار المزود المدفوع المعلنة صراحة |
| `AGENT_MAX_CONSECUTIVE_FAILURES` | 5 | توقف بعد أخطاء متتالية |
| `AGENT_MAX_SKILLS` | 3 | مهارات/طلب |
| `AGENT_VERIFY` | true | مراجع الجواب |
| `AGENT_AUTO_APPROVE` | false | **خطر**: تجاوز الموافقات |
| `AGENT_DEFAULT_TOOLS` | `load_skill,...` | أدوات بلا مهارة |
| `AGENT_MAX_FILE_BYTES` | 5_000_000 | حد قراءة |
| `AGENT_MAX_WRITE_BYTES` | 2_000_000 | حد كتابة |
| `AGENT_HISTORY_MESSAGES` | 20 | سياق جلسة |
| `SANDBOX_BACKEND` | `subprocess` | `docker` أو `subprocess` |
| `WORKSPACE_DIR`/`SKILLS_DIR`/`DB_PATH` | `workspace`/`skills`/`data/nimna.db` | مسارات |
| `QDRANT_URL` / `EMBEDDING_*` | — / `text-embedding-004` / `768` / `auto` | ذاكرة متجهية (Sprint 2, fallback تلقائي) |
| `BROWSER_USE_ENABLED` | false | تشغيل Cloud V4 اختيارياً |
| `BROWSER_USE_MAX_SPEND_USD` | 0 | بوابة Browser Use الصلبة |
| `BROWSER_USE_API_KEY` | — | مفتاح V4، لا يظهر في API أو audit |
| `PORT` | 8000 | منفذ `nimna serve` |

---

## الأمان — المراجعة #1 (أعلى مخاطرة)

`computer_control` + `shell_execute` = سطح هجوم كامل — الموافقة البشرية وحدها ليست كافية.

**ما تم تطبيقه:**
- فصل المهارتين — موافقة منفصلة لكل طبقة.
- `run_id` عشوائي **32 hex (128-bit)** غير قابل للتخمين، `TTL 300s`، حد 5 معلقة/جلسة، تنظيف تلقائي، تحقق `session_id` عند الحل (403 إن اختلفت)، استهلاك one-shot ذري، جدول `approvals` للتدقيق.
- `shell_execute`: فلتر محتوى **قبل** عرض الموافقة (`rm -rf /, curl|sh, base64|bash, nc, pty, screen/tmux/ssh, fork bomb`), `Popen` + `setsid` + `killpg` على timeout (ليس `await` فقط), `ulimit -t/-v/-n/-f` + `RLIMIT_*`, `cwd=workspace` دائماً, `env` منقّى (بدون `AWS_*/OPENAI_*/SSH_*`), `stdin=DEVNULL`, لا `pty`, شبكة معطلة افتراضياً.
- `mouse_click`: تحقق `0≤x≤2560,0≤y≤1600`; `type_text`: حظر `\n` (استخدم `submit`), حد 5000 حرف؛ `take_screenshot`: TTL 1h وحد 80 ملف، بدون `path` مخصص.
- WebSocket: حد 1MB, `ping` 30s, حد 10/ث, تحقق جلسة.
- `health` يعيد `verify_detail: {checks:[skill_instructions,tool_results,language,completeness]}` وليس bool فقط.

---

## الاختبار والتشخيص

```bash
nimna doctor --offline   # .env, مفاتيح, Docker, صلاحيات, SQLite, مخططات الأدوات
pytest -q                # 196 اختبار (mock) — بلا شبكة
nimna skills validate    # صياغة SKILL.md + restricted
nimna tools              # 26 أداة مع risk ( +3 vector memory + Browser Use V4)
curl -s localhost:8001/api/health | jq
websocat ws://localhost:8001/ws/test
```

توقف تلقائي عند: تكرار نفس استدعاء أداة 3 مرات، تكرار نص 3 مرات، تجاوز `MAX_*`, أو 5 أخطاء متتالية. كل الحمولات عبر `redact_payload` (***REDACTED***).

---

## Agent OS boundaries — الإضافة المعمارية

هذه النسخة لا تحول المستودع إلى `src/` ضخم ولا تدعي قدرات غير مثبتة؛ بل تضيف
حدوداً قابلة للقياس حول الـRuntime الحالي:

```text
Gateway / CLI / Web
        ↓
Mission Runtime
        ├── Model Registry → CostGuard (MAX_SPEND_USD=0 hard gate)
        ├── Skill scope → Tool validation → Governance → Approval
        ├── SQLite canonical Memory + optional vector retrieval
        ├── Sandbox / workspace jail
        └── Audit → SHA-256 Evidence → Runtime fingerprint
```

- `nimna/models/`: capability-aware registry و`GovernedModelProvider`.
- `nimna/governance/`: risk vocabulary وpolicy قبل التنفيذ.
- `nimna/evidence/` و`nimna/provenance/`: سجل أدلة وسلسلة hash وبصمة runtime.
- `nimna/observability/`: تلخيص model/tool/approval metrics من audit canonical.
- `docs/CAPABILITY-MATRIX.md`: ما نملكه فعلاً مقابل المخطط.
- `docs/VERIFICATION-MATRIX.md`: PASS / MOCKED / BLOCKED / PARTIAL بدون خلط.
- `docs/architecture/agent-os-blueprint.md`: خطة ترقية تدريجية بلا كسر النسخة.

### Browser Use Cloud V4 (اختياري ومحكوم)

يوجد محول REST لا يحتاج SDK عند الاستيراد في `nimna/browser/cloud_v4.py`، وأداة
`browser_use_run` داخل skill منفصل. الإعداد الافتراضي مغلق وميزانية Browser Use
صفر؛ أي تشغيل سحابي يحتاج تفعيل صريح، ميزانية موجبة، وموافقة. المحول يرسل
`X-Browser-Use-API-Key` بلا `Bearer`، يحترم نافذة rate-limit ذات الخمس ثواني و
`Retry-After`، ويوقف المتصفح المملوك داخل `finally` عبر V4 stop endpoint.
التفاصيل في [`docs/browser_use_v4.md`](docs/browser_use_v4.md).

### MCP Gateway (اختياري ومحكوم — `implemented`)

طبقة عقود وحدود وسياسة وربط في `nimna/mcp/` تستهدف إصدار MCP **`2026-07-28`**،
وهو إصدار **stateless**: لا `initialize` ولا `Mcp-Session-Id` ولا إعادة إرسال؛ كل
طلب يحمل نسخته وقدراته في `_meta`. الترويسات المطلوبة (`MCP-Protocol-Version`,
`Mcp-Method`, `Mcp-Name`) تُتحقَّق مقابل الـbody **قبل الإرسال**، والبوابة معطّلة
افتراضياً (`MCP_ENABLED=false`).

الأدوات البعيدة تُسجَّل كأدوات عادية بـ`risk="confirm"` فرضاً، فتمر بنفس مسار
الأدوات المحلية: النطاق ← التحقق ← السياسة ← الموافقة ← التنفيذ ← التدقيق. المهارة
`mcp_servers` تعلن النطاق بنمط glob (`mcp__*__*`)، فلا تحصل جولة على أدوات MCP
إلا إذا طلبتها مهارة صراحةً. المخطط المنشور للنموذج هو `inputSchema` بتاع الخادم
نفسه (مواصفة `2026-07-28` تسمح بأي JSON Schema 2020-12، وإعادة إنتاجه عبر
Pydantic ستغيّره بصمت).

**ما يبقى غير مثبت:** لا يوجد اختبار مقابل خادم MCP طرف ثالث حقيقي؛ اختبار
التكامل يستخدم خادماً حقيقياً داخل المستودع على socket حقيقي يتحقق من الترويسات
ويرفض `-32020` عند الاختلاف. لذلك `G18` = `BLOCKED` في `docs/VERIFICATION-MATRIX.md`.
التفاصيل في [`docs/mcp-gateway.md`](docs/mcp-gateway.md).

Endpoints الجديدة:

| Endpoint | الغرض |
|---|---|
| `GET /api/models` | الملف النشط، capabilities، cost profile، budget |
| `GET /api/capabilities` | claims قابلة للآلة مع evidence وحالات صريحة |
| `GET /api/provenance` | commit/files/skills/tools/runtime fingerprint |
| `GET /api/runs/{run_id}/evidence` | audit events + hash verification + metrics |
| `GET /api/browser-use/status` | حالة V4 بدون كشف المفتاح |

**مهم:** `subprocess` ليس عزلاً أمنياً كاملاً؛ وBrowser Use live smoke لا يعمل في
CI العادي لأنه قد يستهلك credits أو يغير بيانات خارجية. راجع `SECURITY.md` قبل
النشر.

---

## المساهمة

- الترخيص MIT — `LICENSE`, `NOTICE` — الأمان `SECURITY.md`.
- إضافة مهارة: مجلد `skills/<name>/SKILL.md` → `POST /api/skills/reload`.
- إضافة أداة: سجلها في `ToolRegistry` (Pydantic) وأضفها لـ `allowed_tools`.
- لا تسجل أسراراً، لا تشغل كـ root، لا تستخدم `local-sandbox` على بيانات حساسة — استخدم rootless/Podman + seccomp/AppArmor.

---

## Quick start (English)

```bash
pip install -e ".[dev]"
cp .env.example .env          # GEMINI_API_KEY (free) or NVIDIA/OpenAI
nimna ask "analyse sales.csv and write a report"
PORT=8001 nimna serve         # http://localhost:8001
nimna doctor --offline
pytest -q                     # 83 offline tests (mock)
```
Add a skill: `skills/<name>/SKILL.md` (`name, description, triggers, allowed_tools`) → `POST /api/skills/reload`. Add a tool: register Pydantic handler on `ToolRegistry`. Swap model: change `MODEL_PROVIDER` — skills/tools/memory stay identical.
