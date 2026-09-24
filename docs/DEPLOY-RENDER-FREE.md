# النشر المجاني على Render — cela.nimna (Free Tier)

> هذا الملف كان موجوداً محلياً فقط في `57dc133`، بينما كان على GitHub `a3f9d30` يحمل الـworkflow الحي + قناة الـannotation. هذا المستند يوحّد الاثنين.

## لماذا Render Free؟

- يقرأ `render.yaml` تلقائياً (Blueprint) وينشئ Web Service واحد Docker.
- لا يحتاج `docker-compose` ولا `k8s` — مناسب للعرض الأولي والـdemo.
- مجاني لكن بقيود: نوم بعد خمول، قرص مؤقت، بلا Redis/Qdrant.

## ما في `render.yaml`؟

- `runtime: docker` → يستخدم `Dockerfile` الموجود (python:3.11-slim + `nimna serve`).
- `plan: free` → خطة مجانية.
- `healthCheckPath: /api/health` → نفس المسار الذي يفحصه `ci.yml` و`live-provider-proof.yml`.
- `PORT=10000` → المنفذ الذي يحقنه Render تلقائياً. التطبيق يحترم `PORT` من البيئة.
- `COST_GUARD_ENABLED=true`, `MAX_SPEND_USD=0` → بوابة التكلفة الصلبة تمنع أي استهلاك مدفوع افتراضياً.
- `SANDBOX_BACKEND=subprocess` → لا docker.sock في Free (آمن).
- الأسرار (`GEMINI_API_KEY`…) بـ `sync: false` → تُضبط من لوحة Render ولا تُكتب في الملف.

## خطوات النشر (5 دقائق)

### 1) حضّر المفاتيح

- Gemini مجاني: https://aistudio.google.com/apikey
  - المفاتيح الجديدة من نوع `AQ.*` (Authorization) — تعمل فقط مع `MODEL_PROVIDER=gemini` (المسار الأصلي)، وهو ما يفعله هذا المشروع افتراضياً.
  - لا تستخدم `AIza*` القديم بعد سبتمبر 2026 — سيرفضه API.

### 2) اربط GitHub بـ Render

1. ادخل https://dashboard.render.com → New + → Blueprint
2. اختر المستودع `sayedelazameydesign-crypto/cela.nimna`
3. Render سيكتشف `render.yaml` تلقائياً ويعرض خدمة `cela-nimna`.
4. اترك `autoDeploy: false` (كما في الملف) حتى تراجع كل Push يدوياً.

### 3) اضبط متغيرات البيئة

في لوحة الخدمة → Environment:

**مطلوب واحد على الأقل:**
- `GEMINI_API_KEY = AQ....` (أو `GOOGLE_API_KEY` كـ fallback)

**اختياري:**
- `OPENAI_API_KEY` + `OPENAI_BASE_URL` إذا أردت NVIDIA NIM / OpenAI
- `LOG_LEVEL=INFO`, `AGENT_VERIFY=true` (موجودة افتراضياً في yaml)

> لا تضع `.env` في Git — الملف ممنوع في `00-integrity.yml` و`ci.yml` يفشل إذا وُجد.

### 4) Deploy

- اضغط Manual Deploy → Deploy latest commit.
- تابع Logs حتى ترى `Uvicorn running on 0.0.0.0:10000`.
- افتح `https://<your-service>.onrender.com/api/health` — يجب أن ترى:

```json
{
  "status": "ok",
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "skills": 9,
  "tools": 26
}
```

- الواجهة: `https://<your-service>.onrender.com/` (RTL dashboard).

### 5) اختبار سريع

```bash
curl -s https://<your-service>.onrender.com/api/health | jq
curl -s https://<your-service>.onrender.com/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"حلّل workspace/sales.csv باختصار"}' | jq .reply
```

إذا كان `MAX_SPEND_USD=0` ومزوّدك مدفوع بلا تسعير صريح، سيرفض الطلب بـ `BLOCKED` — هذا مقصود (CostGuard). ضع `MODEL_PROVIDER=gemini` مع مفتاح مجاني أو اضبط `MODEL_COST_*`.

## قيود Free Tier وكيف نتعامل معها

| القيد | التأثير | الحل في هذا المشروع |
|---|---|---|
| **النوم بعد 15 دقيقة خمول** | أول طلب بعد النوم يتأخر 30-60 ثانية | طبيعي للـdemo؛ للـprod ارفع إلى Starter |
| **قرص مؤقت (ephemeral)** | `data/nimna.db` و`workspace/reports` يُمسحان عند redeploy | لا تعتمد على persistence؛ حمّل التقارير فوراً أو استخدم S3/Postgres خارجي |
| **لا Redis / لا Qdrant مجاناً** | `REDIS_URL` و`QDRANT_URL` فارغان | يوجد fallback تلقائي: Vision cache بلا Redis، وhash embeddings 768-dim بلا Qdrant (يعمل لكن بلا بحث متجهي حقيقي) |
| **لا docker.sock** | `SANDBOX_BACKEND=docker` مستحيل | نستخدم `subprocess` (يطلب موافقة) — آمن للـFree |
| **موارد محدودة (512MB RAM)** | `run_python` قد يُقتل | `SANDBOX_MEMORY_MB=512`, `SANDBOX_TIMEOUT=20` افتراضياً |

## العلاقة مع الـworkflow الحي + قناة الـannotation

على GitHub في `a3f9d30` لدينا:

- `.github/workflows/live-provider-proof.yml` — يعمل فقط `workflow_dispatch` (يدوي) ويستهلك حصة حقيقية.
  - يفحص وجود `GEMINI_API_KEY` كـ secret ولا يمررها في Logs.
  - يشغّل `scripts/live_provider_proof.py --evidence-out live-proof-evidence.json`
  - ينشر الخلاصة في `$GITHUB_STEP_SUMMARY` **و** كـ `::notice::` annotation (قناة قابلة للقراءة عبر `api.github.com` حتى لو كان سجل الـjob غير reachable).
  - يرفع `live-proof-evidence.json` كـ artifact.

**كيف تستخدمه بعد نشر Render؟**

1. في GitHub → Settings → Secrets → Actions → أضف `GEMINI_API_KEY` (نفس المفتاح الذي وضعته في Render، لكن كـ secret منفصل).
2. Actions → Live provider proof (real inference) → Run workflow.
3. النتيجة ستظهر في:
   - Annotations (أعلى صفحة الـrun) — مثال: `PROOF {"verdict":"PASS","provider":"gemini",...}`
   - Step Summary
   - Artifacts → `live-proof-evidence`

هذا يثبت أن المزود يرد فعلاً، وليس `MockProvider` الذي تستخدمه كل اختبارات `pytest -q`.

## الأمان في Render Free

- لا تفعّل `AGENT_AUTO_APPROVE=true` — خطر.
- لا تركّب `computer_control` مع `COMPUTER_ENABLED=true` إلا إذا كنت تتحكم في desktop معزول (ليس في Free).
- الأسرار تُستبدل `***REDACTED***` في `audit_log` عبر `redact_payload`.
- `render.yaml` لا يحتوي أي مفتاح — كل الأسرار `sync: false`.

## استكشاف الأخطاء

- **Health يرجع 502**: انتظر cold start، أو افحص Logs → `nimna doctor --offline` داخل الحاوية.
- **BLOCKED بسبب CostGuard**: `MAX_SPEND_USD=0` يمنع المدفوع — استخدم Gemini مجاني أو اضبط `MODEL_COST_*` و`MAX_SPEND_USD`.
- **Mock banner يظهر**: تأكد أن `MODEL_PROVIDER` ليس `mock` وأن `GEMINI_API_KEY` مضبوط.
- **Workspace فارغ**: طبيعي — Render يبني من Git فقط. ارفع ملفاتك عبر `/api/chat` أو ضعها في `workspace/` في المستودع.

## ما التالي؟

- للـproduction: استخدم `k8s/` + `infra/` (Qdrant/Redis) أو Docker rootless + seccomp/AppArmor.
- للـbenchmark: شغّل `scripts/evaluate_arena.py` محلياً أو عبر `arena_diff_eval.yml` في كل PR.
- للـevidence: `/api/provenance` و`/api/runs/{run_id}/evidence` يعطيان fingerprint + hash chain.

---

**الخلاصة:** `render.yaml` + هذا الملف = نشر مجاني قابل لإعادة الإنتاج، و`live-provider-proof.yml` + `::notice::` = إثبات حي أن النموذج يرد فعلاً — الاثنان الآن في نفس الفرع بعد التوحيد.
