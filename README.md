# Nimna – وكيل ذكي قابل لإعادة استخدام المهارات

> **حالة المشروع: نسخة مرشحة للإصدار (Release Candidate) — محصّن وفق نطاق الاختبارات الحالية (77 اختبارًا)**
> الاختبارات لا تثبت الأمان المطلق. الحاويات تشترك في **نواة المضيف**؛ يجب إبقاء المضيف وDocker محدثين واستخدام **seccomp/AppArmor أو SELinux** عند النشر. لا تستخدم `subprocess` كعزل أمني، ولا تعتبر `mock` دليلًا على اتصال مزود حقيقي.

وكيل عام يعمل فوق **مفتاح Gemini المجاني** (أو NVIDIA NIM أو أي نموذج متوافق مع OpenAI) ولا يحشر كل التعليمات داخل الـ prompt؛ بل يملك **مكتبة مهارات** على شكل مجلدات `SKILL.md`:

> يقرأ الوكيل أوصاف المهارات أولًا ← يختار المناسب منها ← يحمّل تفاصيلها عند الحاجة ← ينفذها بأدوات محددة ومسموح بها فقط ← يطلب موافقتك قبل أي عملية حساسة ← يتحقق من النتيجة ← يرد.

```
المستخدم → واجهة (CLI / Web / REST)
   → المخطط Planner (يرى الكتالوج المختصر فقط)
   → اختيار المهارات (حتى 3)
   → تحميل SKILL.md + المراجع عند الطلب
   → حلقة النموذج ↔ الأدوات (تحقق Pydantic، صلاحيات، سجل تدقيق)
   → موافقة يدوية للأدوات الخطرة (تعليق التشغيل واستئنافه)
   → مراجعة النتيجة (Verifier)
   → الرد النهائي + حفظه في الذاكرة
```

> **English:** Nimna is a provider-agnostic "agent skills" runtime: skills are `SKILL.md` folders loaded progressively, tools are scoped per skill and validated with Pydantic, sensitive tools require human approval (runs pause/resume), memory and audit live in SQLite, and the model layer (Gemini / NVIDIA / OpenAI-compatible) is swappable without touching skills or tools. See the [Quick start](#quick-start-english) below.

---

## المحتويات

1. [التشغيل السريع](#التشغيل-السريع)
2. [البنية](#البنية)
3. [المهارات](#المهارات-skills)
4. [الأدوات والصلاحيات](#الأدوات-والصلاحيات)
5. [الذاكرة وسجل التدقيق](#الذاكرة-وسجل-التدقيق)
6. [تبديل المزود: Gemini / NVIDIA / غيرهما](#تبديل-المزود)
7. [واجهة REST](#واجهة-rest)
8. [Docker](#docker)
9. [الإعدادات](#الإعدادات)
10. [الأمان وحدود التصميم](#الأمان-وحدود-التصميم)

---

## التشغيل السريع

```bash
git clone <repo> && cd cela.nimna
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# ضع مفتاحك: GEMINI_API_KEY=...   (مجاني من https://aistudio.google.com/apikey)

nimna doctor --offline                 # تحقق قبل التشغيل
nimna skills list                      # المهارات المثبتة
nimna ask "حلّل ملف المبيعات وأنشئ لي تقريرًا"
nimna chat                             # محادثة تفاعلية (الموافقات تُطلب في الطرفية)
nimna serve                            # واجهة ويب + REST على http://localhost:8000
nimna doctor --offline               # فحص البيئة والمفاتيح و Docker والمهارات
pytest                                 # 69 اختبارًا تعمل بلا مفتاح (مزود وهمي) (مزود وهمي)
```

بدون أي مفتاح يمكنك تجربة كل شيء بالمزود الوهمي: `MODEL_PROVIDER=mock nimna serve` — سترى اختيار المهارات والأدوات يعمل فعليًا، والردود فقط تكون وهمية.

مجلد `workspace/` يحتوي `sales.csv` تجريبيًا، لذا الطلب أعلاه يعمل مباشرة: يختار الوكيل مهارتَي `csv_analysis` و`report_writer`، يفحص الملف، يحسب الإحصاءات (إجمالًا وحسب المنطقة)، ويكتب التقرير في `workspace/reports/`.

---

## البنية

```
nimna/
├── config.py            # الإعدادات من متغيرات البيئة / .env (لا مفاتيح داخل الكود)
├── bootstrap.py         # build_agent(): يربط المزود + المهارات + الأدوات + الذاكرة
├── providers/           # طبقة النموذج (قابلة للتبديل)
│   ├── base.py          #   Message / ToolCall / ToolSpec / ModelProvider + إعادة المحاولة عند 429
│   ├── gemini.py        #   Google Gen AI SDK (function calling يدوي، يحافظ على thought signatures)
│   ├── openai_compat.py #   أي /chat/completions: NVIDIA NIM، OpenAI، Groq، Ollama...
│   └── mock.py          #   مزود وهمي للاختبارات والعرض بدون مفتاح
├── skills/              # مدير المهارات: اكتشاف، كتالوج مختصر، ترتيب لفظي، مراجع
├── tools/               # سجل الأدوات: مخطط JSON من Pydantic، مستويات خطورة، سجن المسارات
│   ├── sandbox.py       #   تنفيذ Python: subprocess (rlimits) أو docker (--network none)
│   └── builtin/         #   files, csv, python, web, reports, memory, skills
├── memory/store.py      # SQLite: الجلسات، الرسائل، الذاكرة الدائمة، سجل التدقيق، الموافقات المعلقة
├── core/
│   ├── planner.py       #   اختيار المهارات (LLM + JSON) مع رجوع للترتيب اللفظي
│   ├── agent.py         #   حلقة الوكيل القابلة للتعليق/الاستئناف + المراجعة
│   ├── approval.py      #   سياسات الموافقة: Console / Defer (API) / Auto / Callback
│   └── state.py         #   RunState قابل للتسلسل، AgentResult
├── api/                 # FastAPI + واجهة ويب صغيرة (RTL) تدعم بطاقة الموافقة
└── cli.py               # nimna ask | chat | skills | tools | serve | approvals | resume | doctor
skills/                  # مكتبة المهارات (أضف مجلدًا = مهارة جديدة)
workspace/               # مساحة عمل الوكيل (كل أدوات الملفات مقيدة بداخلها)
tests/                   # pytest – تعمل بالكامل بدون شبكة
```

### دورة عمل طلب واحد

عند قول المستخدم: **«حلّل ملف المبيعات وأنشئ لي تقريرًا»**

1. يُحفظ الطلب في ذاكرة الجلسة، ويُستدعى آخر 20 رسالة كسياق قصير.
2. **المخطط** يرى الكتالوج المختصر فقط (الاسم + الوصف + الكلمات المفتاحية) ويعيد JSON: `{"skills": ["csv_analysis", "report_writer"], "plan": [...]}`. إن فشل النموذج أو أعاد نصًا غير صالح، يُستخدم الترتيب اللفظي (يدعم العربية والإنجليزية).
3. تُحمّل أجسام `SKILL.md` المختارة داخل system prompt، وتُفتح **فقط** الأدوات المذكورة في `allowed_tools` لتلك المهارات (+ أدوات أساسية: `load_skill`, `read_skill_reference`, `memory_*`).
4. حلقة النموذج ↔ الأدوات: كل استدعاء يُتحقق منه بـ Pydantic؛ الأخطاء تُعاد للنموذج ليصحح نفسه.
5. أي أداة بخطورة `confirm` (حذف، كتابة فوق ملف موجود، تنفيذ Python بلا Docker) توقف التشغيل وتطلب موافقة: في CLI تُسأل فورًا، وفي REST تُعاد `status: awaiting_approval` وتُستأنف عبر `POST /api/approvals/{id}`.
6. **المراجع Verifier**: استدعاء إضافي يفحص الجواب مقابل تعليمات المهارة والطلب؛ إن وجد مشاكل حقيقية يُطلب من النموذج إصلاحها (مرة واحدة). يمكن تعطيله بـ `AGENT_VERIFY=false`.
7. يُحفظ الرد في الجلسة، وكل ما حدث (مهارات محمّلة، أدوات، موافقات، استهلاك التوكنات) في `audit_log`.

---

## المهارات (Skills)

كل مهارة مجلد بداخله `SKILL.md`. تُعرض الواجهة الأمامية (front matter) فقط للمخطط، ولا يُحمّل الجسم إلا عند الاختيار — وهذا ما يجعل إضافة عشرات المهارات لا يضخّم الـ prompt.

```
skills/csv_analysis/
├── SKILL.md                 # مطلوب
├── references/              # وثائق يقرأها الوكيل عند الحاجة بـ read_skill_reference
├── scripts/                 # اختياري: سكربتات تُشغَّل عبر run_python
└── tests/                   # اختياري
```

```markdown
---
name: csv_analysis
display_name: تحليل ملفات CSV
description: تحليل ملفات CSV: اكتشاف الأعمدة، القيم المفقودة، الإحصاءات، التجميع، وإنشاء ملخص.
version: 1.0.0
triggers: [تحليل ملف, CSV, إحصاءات البيانات, analyze csv]
allowed_tools: [list_files, read_csv, calculate_statistics, create_chart, read_skill_reference]
---

## التعليمات
1. افحص الأعمدة وأنواعها بـ read_csv ...
2. لا تعدّل الملف الأصلي.
```

- الحقول: `name` (مطلوب)، `description` (مطلوب)، `triggers`، `allowed_tools` (تُقبل أيضًا `allowed-tools` بصيغة agentskills)، `version`، `risk_level` (`safe`/`confirm`/`restricted`)، `tags`، `metadata`.
- إن نسي الكاتب اقتباس نقطتين `:` داخل الوصف، يوجد محلل متسامح احتياطي.
- **إضافة مهارة = إضافة مجلد** ثم `POST /api/skills/reload` (أو إعادة التشغيل). `nimna skills validate` يفحص الصياغة وينبه لأدوات غير مسجلة (`allowed_tools` تشير لأداة غير موجودة) أو مهارة `restricted` تحتاج مراجعة يدوية.
- مهارة `skill_author` تجعل الوكيل نفسه يكتب مهارات جديدة عند طلبك: «أنشئ مهارة لمراجعة ملفات السجلات».

### المهارات المضمّنة

| المهارة | الغرض | الأدوات |
|---|---|---|
| `csv_analysis` | فحص CSV، إحصاءات، تجميع، رسم بياني | read_csv, calculate_statistics, create_chart |
| `report_writer` | تقرير Markdown منظم في `reports/` | write_report |
| `web_research` | بحث (DuckDuckGo بلا مفتاح) + قراءة صفحات + مصادر | web_search, fetch_url |
| `python_executor` | تشغيل سكربتات قصيرة في بيئة معزولة | run_python |
| `file_analysis` | استعراض وقراءة وتلخيص الملفات النصية | list_files, read_file, file_info |
| `skill_author` | تأليف مهارة جديدة بصيغة SKILL.md | write_file, list_skills |

---

## الأدوات والصلاحيات

`nimna tools list` يعرض 17 أداة مع مستوى الخطورة:

- **safe**: قراءة/حساب فقط، تُنفذ مباشرة.
- **confirm**: تحتاج موافقة (`delete_file` دائمًا؛ `write_file`/`write_report` عند الكتابة فوق ملف موجود؛ `run_python` عندما يكون الـ sandbox من نوع subprocess).
- كل أدوات الملفات مسجونة داخل `WORKSPACE_DIR`؛ المسارات المطلقة، `../`، الروابط الرمزية (`symlink`) التي تهرب خارج المساحة، والـ null bytes تُرفض. `list_files` يتجاهل الروابط التي تهرب.
- `fetch_url`/`web_search` ترفض العناوين المحلية والخاصة (SSRF) — يُفحص كل `hostname` قبل الطلب وبعد كل إعادة توجيه (حتى 5 قفزات): `localhost`، `127.0.0.1`، `0.0.0.0`، الشبكات الخاصة/الحلقة/الرابط-المحلي، واللاحقات `.local`/`.internal`.
- النموذج لا يرى إلا الأدوات التي تسمح بها المهارات المحمّلة؛ استدعاء أداة غير مسموحة يُعاد كخطأ.

إضافة أداة جديدة:

```python
from pydantic import BaseModel, Field
from nimna.tools import default_registry, ToolContext

class SendEmailParams(BaseModel):
    to: str = Field(..., description="Recipient")
    body: str

registry = default_registry()

@registry.tool("send_email", "Send an email (requires approval).", SendEmailParams, risk="confirm")
def send_email(params: SendEmailParams, ctx: ToolContext):
    ...
    return {"sent": True}

agent = build_agent(tools=registry)
```

ثم أضف `send_email` إلى `allowed_tools` في المهارات التي يحق لها استخدامه.

---

## الذاكرة وسجل التدقيق

- **قصيرة**: رسائل الجلسة (`session_id`) تُمرر تلقائيًا (`AGENT_HISTORY_MESSAGES`).
- **دائمة**: أداتا `memory_save` / `memory_search`؛ العناصر من نوع `preference` تُحقن في الـ prompt تلقائيًا (مثل «المستخدم يفضل الجداول المختصرة»).
- **سجل التدقيق**: `GET /api/sessions/{id}/audit` أو جدول `audit_log` — كل مهارة حُمّلت، كل أداة استُدعيت (مع الوسائط ومدة التنفيذ والنتيجة)، كل موافقة طُلبت وقرارها، واستهلاك التوكنات.
- البحث في الذاكرة لفظي (LIKE)؛ يمكن استبداله بقاعدة متجهات بتغيير `MemoryStore.search_memories` أو `SkillManager.rank_by_keywords`.

---

## تبديل المزود

طبقة النموذج مستقلة تمامًا عن الوكيل والمهارات:

```python
class ModelProvider(ABC):
    def generate(self, messages: list[Message], tools: list[ToolSpec] | None = None) -> ModelResponse: ...
```

| المزود | `.env` |
|---|---|
| **Gemini (مجاني)** | `MODEL_PROVIDER=gemini` `GEMINI_API_KEY=...` `GEMINI_MODEL=gemini-2.5-flash` |
| **NVIDIA NIM** | `MODEL_PROVIDER=openai` `OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1` `NVIDIA_API_KEY=...` `OPENAI_MODEL=meta/llama-3.3-70b-instruct` |
| OpenAI | `MODEL_PROVIDER=openai` `OPENAI_BASE_URL=https://api.openai.com/v1` `OPENAI_API_KEY=...` `OPENAI_MODEL=gpt-4o-mini` |
| Ollama محلي | `MODEL_PROVIDER=openai` `OPENAI_BASE_URL=http://localhost:11434/v1` `OPENAI_API_KEY=ollama` `OPENAI_MODEL=llama3.1` |
| بلا مفتاح | `MODEL_PROVIDER=mock` |

أو برمجيًا:

```python
from nimna import build_agent, Settings
from nimna.providers.openai_compat import OpenAICompatibleProvider

provider = OpenAICompatibleProvider(api_key="...", base_url="https://integrate.api.nvidia.com/v1",
                                    model="meta/llama-3.3-70b-instruct")
agent = build_agent(Settings.from_env(), provider=provider)
print(agent.run("لخص الملفات الموجودة").reply)
```

ملاحظات المستوى المجاني في Gemini: عند 429 يعيد المزود المحاولة تلقائيًا بتراجع أسّي (3 محاولات). كل طلب يستهلك عادة: استدعاء مخطط + عدد خطوات الأدوات + استدعاء مراجعة؛ عطّل المراجعة بـ `AGENT_VERIFY=false` إن كانت الحصة ضيقة.

---

## واجهة REST

`nimna serve` ثم افتح `http://localhost:8000` (واجهة محادثة RTL تعرض المهارات والأدوات وبطاقة الموافقة) أو `/docs` لتوثيق OpenAPI.

| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/api/health` | المزود، النموذج، عدد المهارات/الأدوات |
| GET | `/api/skills` · `/api/skills/{name}` | الكتالوج / المهارة كاملة |
| POST | `/api/skills/reload` | إعادة اكتشاف مجلد المهارات |
| GET | `/api/tools` | الأدوات مع مخططاتها ومستوى الخطورة |
| POST | `/api/chat` | `{"message": "...", "session_id": "اختياري"}` |
| POST | `/api/approvals/{id}` | `{"approved": true, "always": false}` لاستئناف تشغيل معلق |
| GET | `/api/approvals?session_id=` | الموافقات المعلقة |
| GET | `/api/sessions/{id}/messages` · `/audit` | الذاكرة القصيرة / سجل التدقيق |
| GET | `/api/memories?q=` | الذاكرة الدائمة |

مثال دورة موافقة:

```bash
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"message":"احذف الملف old.csv"}'
# → {"status":"awaiting_approval","pending":{"approval_id":"3f2c...","tool_name":"delete_file",...}}
curl -s localhost:8000/api/approvals/3f2c... -H 'Content-Type: application/json' -d '{"approved":true}'
# → {"status":"done","reply":"تم حذف old.csv ..."}
```

---

## Docker

```bash
cp .env.example .env   # ضع المفتاح (chmod 600 .env)
docker compose up --build          # الوضع الآمن: subprocess sandbox (يطلب موافقة لـ run_python)
# أو للعزل الحقيقي عبر Docker (يحتاج Docker Engine على المضيف):
docker compose --profile local-sandbox up --build   # يشغل nimna-sandbox مع docker.sock
```

- المجلدات `skills/` و`workspace/` مركّبة كـ volumes: أضف مهارة أو ملفًا دون إعادة بناء.
- **تحذير صريح:** تركيب `/var/run/docker.sock` يمنح الحاوية تحكمًا شبه كامل بالمضيف (يمكنها إنشاء حاويات بصلاحيات عالية، حتى لو بوضع القراءة فقط) ويلغي عزل الـ sandbox — الوصول إلى الـ socket يعادل عمليًا صلاحيات واسعة جدًا على المضيف، وحتى التركيب بوضع القراءة فقط ليس عزلًا كافيًا. لذلك **الخدمة الافتراضية `nimna` لا تركّب الـ socket** وتعمل بـ `SANDBOX_BACKEND=subprocess` (يطلب موافقة). `local-sandbox` **خيار تطوير واضح فقط** — يجب ألا يكون متاحًا في أمر نشر اعتيادي أو CI غير موثوق. استخدم الخدمة `nimna-sandbox` ذات الـ profile فقط للتطوير المحلي على جهاز لا يحتوي بيانات حساسة، أو شغّل Nimna خارج Docker واجعل `run_python` يستخدم Docker Engine، أو استخدم proxy محدود الصلاحيات / Podman / خدمة sandbox منفصلة — انظر `SECURITY.md` و`docker-compose.yml`.
- عند استخدام الـ profile، يعمل `run_python` داخل `python:3.11-slim` بلا شبكة (`--network none`)، بحدود ذاكرة/CPU/عمليات، وكل الصلاحيات محذوفة (`--cap-drop ALL` + `--security-opt no-new-privileges`)، ويصبح مصنفًا **safe** (لا يحتاج موافقة).
- **الإنتاج الموصى به:** استخدم **Docker rootless** أو **Podman** مع مستخدم غير `root`، وحدود CPU والذاكرة، و`cap-drop=ALL` و`no-new-privileges` وملف **seccomp** أو **AppArmor/SELinux** مناسب. وضع rootless يقلل أثر اختراق الحاوية مقارنةً بـ Docker daemon يعمل بصلاحيات root.

---

## الإعدادات

كل الإعدادات في `.env.example` مع شرحها. أهمها:

| المتغير | الافتراضي | المعنى |
|---|---|---|
| `MODEL_PROVIDER` | `gemini` | `gemini` / `openai` / `mock` |
| `AGENT_MAX_STEPS` | 12 | أقصى عدد جولات نموذج لكل طلب |
| `AGENT_MAX_TOOL_CALLS` | 30 | أقصى عدد استدعاءات أدوات لكل طلب |
| `AGENT_MAX_RUNTIME_SECONDS` | 300 | حد زمني بالثواني لكل طلب |
| `AGENT_MAX_RESPONSE_TOKENS` | 4096 | سقف توكن الرد (يمرر للنموذج حيثما يُدعم) |
| `AGENT_MAX_CONSECUTIVE_FAILURES` | 5 | توقف بعد أخطاء أدوات متتالية |
| `AGENT_MAX_SKILLS` | 3 | أقصى عدد مهارات تُحمّل لكل طلب |
| `AGENT_VERIFY` | true | تشغيل المراجع على الجواب النهائي |
| `AGENT_AUTO_APPROVE` | false | **خطر**: تجاوز الموافقات (للأتمتة الموثوقة فقط) |
| `AGENT_DEFAULT_TOOLS` | `load_skill,memory_search,list_files` | الأدوات عندما لا تُختار أي مهارة |
| `AGENT_MAX_FILE_BYTES` | 5000000 | أقصى حجم ملف يُقرأ |
| `AGENT_MAX_WRITE_BYTES` | 2000000 | أقصى حجم كتابة |
| `SANDBOX_BACKEND` | `subprocess` | `docker` (عزل حقيقي، يحتاج Docker) أو `subprocess` (افتراضي آمن، يطلب موافقة) |
| `WORKSPACE_DIR` / `SKILLS_DIR` / `DB_PATH` | `workspace` / `skills` / `data/nimna.db` | المسارات |

---

## الأمان وحدود التصميم

> **تنبيه:** Nimna محصّن **وفق نطاق الاختبارات الحالية (77 اختبارًا)** — لا يثبت الأمان المطلق. الحاويات تشترك في نواة المضيف؛ يجب إبقاء المضيف وDocker محدثين واستخدام `seccomp`/`AppArmor` أو `SELinux` عند النشر.

- المفتاح المجاني يوفّر قدرة النموذج فقط؛ نظام المهارات والذاكرة والصلاحيات مبني حوله ولا يعتمد على مزود بعينه.
- `subprocess` sandbox يعزل البيئة والمسار ويحد الموارد لكنه **ليس حدًا أمنيًا** (`confirm` دائمًا) — لا تعتبره عزلًا أمنيًا. الافتراضي الآمن الآن `subprocess`؛ وضع `docker` (`--network none`، `--cap-drop ALL`، `--security-opt no-new-privileges`، حدود ذاكرة/CPU/عمليات) هو **safe** لكنه لا يزال يشارك النواة ويحتاج تحديثات وملف seccomp/AppArmor.
- الوكيل لا يرسل ولا يشتري ولا يحذف شيئًا دون موافقة صريحة، والنظام يفرض ذلك برمجيًا لا بالـ prompt فقط.
- مخرجات الأدوات تُقتطع (`AGENT_TOOL_RESULT_MAX_CHARS`) لحماية نافذة السياق.
- محتوى الويب غير موثوق؛ تعليمات المهارة `web_research` تطلب من النموذج معاملته كبيانات لا كأوامر.


- لا تُسجل الأسرار: كل الحمولات التي تمر عبر `redact_payload`/`MemoryStore.log` تستبدل قيم `api_key`/`secret`/`password`/`token` بـ `***REDACTED***` قبل التدقيق أو عرضها للنموذج (انظر `SECURITY.md`).
- توقف الحلقة تلقائيًا عند: تكرار نفس استدعاء الأداة 3 مرات بلا تقدم، تكرار نفس نص النموذج 3 مرات، تجاوز `MAX_STEPS`/`MAX_TOOL_CALLS`/`MAX_RUNTIME`، أو 5 أخطاء أدوات متتالية.
- لا يمكن لمهارة إعلان أداة غير مسجلة — `nimna skills validate` ينبه وruntime يتجاهلها.
- أوامر التشخيص: `nimna doctor --offline` يتحقق من `.env`، المفاتيح، Docker، صلاحيات `workspace`، اتصال SQLite، وصحة مخططات الأدوات.
- **قواعد تشغيل آمنة (إلزامية):**
  - لا تستخدم `--profile local-sandbox` على جهاز يحتوي بيانات حساسة
  - لا تشغّل الخدمة كـ `root` — الصورة تستخدم `USER nimna` (uid 1000) ويُنصح بـ Docker rootless/Podman
  - لا تضع مفاتيح API داخل صورة Docker — مررها عبر `env_file: .env` (chmod 600) أو متغيرات البيئة
  - لا تعتبر `subprocess` عزلًا أمنيًا
  - الـ `mock` والاختبارات لا يثبتان نجاح الاتصال بمزود حقيقي — يجب اختبار Gemini/NVIDIA فعليًا قبل الإنتاج

```text
لا تستخدم --profile local-sandbox على جهاز يحتوي بيانات حساسة
لا تشغّل الخدمة كـ root
لا تضع مفاتيح API داخل صورة Docker
لا تعتبر subprocess عزلًا أمنيًا
الـ mock والاختبارات لا يثبتان نجاح الاتصال بمزود حقيقي
```

---

## الترخيص والأمان

- الترخيص: MIT — انظر `LICENSE` و`NOTICE`.
- الأمان: `SECURITY.md` يوضح عزل الملفات/الشبكة/الكود والموافقات والحدود.
- المساهمة: `CONTRIBUTING.md` — كيف تضيف مهارة/أداة وتشغّل `pytest` و`nimna doctor`.

---

## Quick start (English)

```bash
pip install -e ".[dev]"
cp .env.example .env            # set GEMINI_API_KEY (free) or NVIDIA/OpenAI settings
nimna ask "analyse sales.csv and write a report"
nimna serve                     # web UI + REST at :8000
nimna doctor --offline     # diagnose env / keys / docker
pytest                          # 69 offline tests (mock provider)
```

Add a skill: create `skills/<name>/SKILL.md` with front matter (`name`, `description`, `triggers`, `allowed_tools`) and instructions, then `POST /api/skills/reload`. Add a tool: register a Pydantic-typed handler on the `ToolRegistry` and list it in the skills allowed to use it. Swap the model: change `MODEL_PROVIDER` — skills, tools, memory and approvals stay exactly the same.
