# Nimna — Release Notes (Release Candidate)

> **محصّن وفق نطاق الاختبارات الحالية — ليس إثباتًا للأمان المطلق.**
> الحاويات تشترك في نواة المضيف؛ حافظ على المضيف وDocker محدثين واستخدم seccomp/AppArmor أو SELinux.

## Unreleased — 2026-09-25

### Resilience & scaling sprint

- **مرونة المزوّد**: circuit breaker + bulkhead عند حدّ المزوّد
  (`nimna/resilience/`) — 5 إخفاقات متتالية تفتح القاطع 30s مع fail-fast،
  ورفض الميزانية لا يُسجَّل إخفاقًا أبدًا؛ retry الآن بـ jitter كامل ضد
  القطيع الرعدي (`tests/test_resilience.py`: 17).
- **Idempotency**: ترويسة `Idempotency-Key` على `/api/chat` وحلّ الموافقات —
  نفس المفتاح+الجسم يعيد الرد المخزّن (`Idempotent-Replayed`)، والسباق
  أثناء التنفيذ 409، والجسم المختلف 422 (`tests/test_idempotency.py`: 10).
- **مراقبة**: `X-Request-ID` على كل رد + `GET /api/metrics` بصيغة Prometheus
  (خلف المصادقة) + سياسة cache (`no-store` للـAPI) + قسم `resilience` في
  health (`tests/test_telemetry.py`: 11).
- **توسع**: rate limit مشترك عبر Redis (Lua ذرية) عند ضبط `REDIS_URL` مع
  fail-open مُراقَب عند عطل Redis؛ k8s: anti-affinity + PDB + HPA مُثبَّتة
  باختبارات (`tests/test_redis_limiter.py`: 8، `tests/test_k8s_resilience.py`: 7).
- **عمليات**: نسخ SQLite احتياطي/تحقق/استرجاع (`scripts/backup_sqlite.py`،
  RPO/RTO موثقة) + مسبار حمل (`scripts/load_probe.py`) + `docs/RESILIENCE.md`
  (SLO/runbooks/chaos) (`tests/test_backup_sqlite.py`: 5، `tests/test_load_probe.py`: 4).

### FOSS-only stack (same-day batch)

- **Valkey بدل Redis**: compose وk8s صارا `valkey/valkey:8` (BSD-3) —
  `redis:7-alpine` العائم كان قد يحلّ لنسخة SSPL/RSAL؛ بلا تغيير كود
  (RESP/Lua متوافقان) مع بقاء أسماء `redis`/`REDIS_URL` للتوافق
  (`tests/test_foss_stack.py`: 9).
- **تحقق الأدوات**: كل اسم في القائمة فُحص (وجود+ترخيص) — Orion **موجود
  فعلًا** ([bit2swaz/orion](https://github.com/bit2swaz/orion)، MIT) وصحّحنا
  السجل بعد أن أخطأ البحث (مشروع صغير: نجمتان و11 كوميت في ديسمبر 2025) —
  للتجربة لا للإنتاج الحرج؛ وStrikeMQ/Rafka **مرفوضة للإنتاج** (تصريح
  المؤلفين)، وLucidMQ **مكتبة brokerless** لا وسيط موزع (مؤكد من lib.rs)؛
  وأُضيف Gitness (Apache-2.0 للشجرة مع 27 إصابة (26 ترويسة + 1 مرجع) + CLA مطلوب مع توجيه pin-to-tag + تاريخ PolyForm بالنقل المتكرر + حارس شهري `upstream-license-watch`؛ وForgejo صُحّح MIT←GPL-3.0 بلا CLA وفحصه الكامل نظيف مع LTS ‏v15.0 والـmirror موثق بسلسلة git) وKrkn (CNCF Sandbox من
  Red Hat)؛ و"ResticBorgBackup" **ليست أداة** (Restic وBorgBackup
  منفصلتان)؛ التوصيات: NATS JetStream وpgBackRest (MIT) وOpenTofu
  (MPL-2.0) وSigNoz وPikoCI (Apache-2.0) ؛ وأُضيف فحص أسرار Cloudflare (Keyflare MIT v0.1.0 هادئ، Sigillo MIT نشط بلا LICENSE جذري، رياضيات المجانية + المخرج + خط Infisical المؤسسي بتحفظ ee/ وفشل اختبار فقدان المفتاح للأداتين) — السجل الكامل في `docs/FREE-STACK.md`.
- **أمثلة جاهزة**: `infra/haproxy/haproxy.cfg` (فحص `/api/health`) +
  `infra/monitoring/{prometheus.yml,alerts.yml}` (كشط `/api/metrics` بتوكن
  من ملف، 4 قواعد على مقاييس حقيقية) + `infra/postgres/pgbackrest.conf.example`.

### Hardening & drift fixes (same-day batch)

- **أمن**: ذاكرة الرؤية التخزينية (Vision cache) تستخدم JSON بدل `pickle`
  (إزالة خطر RCE عبر Redis)، و`‎/api/health` العام لم يعد يعرض بيانات
  اعتماد `REDIS_URL` (`tests/test_vision_cache_security.py`: 10).
- **بوابة الإنتاج**: إعادة تثبيت T8-anchor على `0a3a563` (دمج PR #22) —
  التثبيت السابق (`9885f3c`) لم يكن موجودًا في السجل بعد squash-merge فكانت
  الخطوة 1 حمراء دائمًا؛ مع اختبار `anchor-exists` يمنع تكرار ذلك.
- **CLI**: `ask/chat/approvals` تطبع سطرًا واحدًا واضحًا (exit 2) عند غياب
  المفتاح أو رفض cost-guard بدل traceback خام (`tests/test_cli_errors.py`: 4).
- **Shell**: `SHELL_TIMEOUT_MS` صار سقفًا فعليًا للمشغّل (كان يُقرأ ولا
  يُستخدم) والمهلة الفعلية في الـ evidence (`tests/test_shell_operator_cap.py`: 5).
- **النشر**: الصورة تثبّت `.[prod]` (redis/qdrant-client/…) فتتصل فعليًا عند
  ضبط `REDIS_URL/QDRANT_URL`؛ `SHELL_TOOL_ENABLED` أُضيف لـ render.yaml و
  fastapi-cloud.yaml؛ `.env.example` يوثّق كل متغيرات `Settings` الآن.
- **صدق التحقق**: `skills validate` يميّز الأدوات gated (مثل `run_command`)
  عن المجهولة فعلًا؛ README والمصفوفة حُدّثا للأرقام المقيسة
  (539 اختبارًا، 10 مهارات، 28 أداة).

## v0.3.0-governed — Release Candidate — 2026-09-23

> هذا الإصدار يضيف حدود Agent OS محكومة وقابلة للإثبات. الوسم الرسمي ينتظر
> مراجعة ودمج طلب الدمج إلى `main`؛ لا تُفهم اختبارات mock على أنها اتصال إنتاجي.

### 1. Model Registry & Cost Guard

- إضافة `nimna/models/` مع capability-aware model profiles و`GovernedModelProvider`.
- `MAX_SPEND_USD=0` بوابة صلبة قبل Planner وVerifier وAgent loop وSwarm.
- أسعار غير معروفة أو نماذج مدفوعة تفشل مغلقاً في hard mode؛ profile Gemini free-tier معلن صراحةً وليس ضمان فوترة لحساب المزود.

### 2. Governance & Policy Engine

- تطبيق خط المعالجة:
  `scope → validation → policy → approval → execution`.
- المهارات `restricted` ترفع الأدوات إلى طبقة الموافقة.
- الإبقاء على pause/resume وTTL وsession-scoped approval الحالي.

### 3. Provenance & Evidence Chain

- إضافة `nimna/evidence/` و`nimna/provenance/`.
- سجل التدقيق SQLite يحصل على SHA-256 hash chain لكشف التعديل اللاحق.
- runtime manifest يضم commit hash وبصمات الملفات والمهارات والأدوات والتكوينات غير الحساسة.

### 4. Browser Use Cloud API V4

- إضافة `nimna/browser/cloud_v4.py` بدون فرض تثبيت SDK عند الاستيراد.
- المصادقة عبر `X-Browser-Use-API-Key` بدون `Bearer`.
- احترام `Retry-After` وتفسير `X-RateLimit-Limit` كنافذة خمس ثوانٍ.
- إيقاف المتصفحات المملوكة عبر `PATCH /api/v4/browsers/{id}` داخل `finally`.
- إضافة `skills/browser_use/` و`browser_use_run` كقدرة opt-in تتطلب تفعيلاً وميزانية موجبة وموافقة.
- توثيق V4 الكامل في `docs/browser_use_v4.md`، مع التنبيه إلى أن Browser Use Cloud خدمة pay-as-you-go.

### 5. Observability & CI Integrity Gate

- نقاط الفحص الجديدة:
  `/api/models`, `/api/capabilities`, `/api/provenance`,
  `/api/runs/{run_id}/evidence`, `/api/browser-use/status`.
- إضافة `.github/workflows/00-integrity.yml` و`verify_capabilities.py`.
- مصفوفتا القدرات والتحقق تميزان بين `PASS`, `MOCKED`, `BLOCKED`, `PARTIAL` و`PLANNED`.

### نتائج التحقق

- `pytest -q`: **83 passed**.
- `python scripts/verify_capabilities.py`: **integrity PASS**.
- Browser Use live smoke: **لم يُشغّل**؛ لا يوجد API key في CI ولا يجب استهلاك credits أو تعديل بيانات خارجية في Pull Request عادي.
- Docker sandbox الحقيقي وrootless runtime وMCP Gateway وPostgreSQL multi-replica: ما زالت موثقة كـ`partial` أو `planned` في `docs/CAPABILITY-MATRIX.md`.

### ملفات مرجعية

- `docs/CAPABILITY-MATRIX.md`
- `docs/VERIFICATION-MATRIX.md`
- `docs/architecture/agent-os-blueprint.md`
- `docs/browser_use_v4.md`

---

## 0.1.0-rc1 — 2026-09-23

### الاختبارات
- 77 اختبارًا ناجحًا (`pytest -q`) — تشمل 8 اختبارات تقسية جديدة:
  - `test_docker_socket_is_not_required_by_default`
  - `test_ipv4_mapped_ipv6_is_blocked`
  - `test_dns_rebinding_is_blocked`
  - `test_permanent_approval_is_scope_bound`
  - `test_resume_cannot_mutate_approved_call`
  - `test_duplicate_resume_is_rejected`
  - `test_secret_is_absent_from_exception_trace`
  - `test_restart_recovers_pending_run`
- `nimna doctor --offline` — نجح (mock، 0600 perms، 6 skills OK)
- `nimna skills validate` — 6 OK
- `nimna tools` — 17 أداة
- `docker compose config | grep -F docker.sock` — لا يعيد شيئًا في الوضع الافتراضي (مع `--profile local-sandbox` فقط يظهر)
- `MODEL_PROVIDER=mock nimna ask "حلّل sales.csv وأنشئ تقريرًا"` — نجح (mock)
- محاكاة منخفضة المخاطر (قراءة `sales.csv` + كتابة `reports/sales_summary.md` جديد دون كتابة فوق المصدر) — نجحت

### حالة اختبارات المزود الحقيقي والبيئة الفعلية
- تم اختبار المزود الحقيقي: **لا** — الاختبارات الحالية mock فقط؛ يلزم اختبار Gemini وNVIDIA NIM فعليًا قبل الإنتاج.
- تم اختبار Docker sandbox الحقيقي: **لا** — الافتراضي `subprocess` في CI؛ يلزم `docker compose --profile local-sandbox up` على جهاز تطوير.
- تم اختبار rootless runtime: **لا** — موصى به للإنتاج (Docker rootless أو Podman) مع `cap-drop=ALL`, `no-new-privileges`, seccomp/AppArmor.

### فحص الاعتماديات
- `pip-audit -r requirements.txt` — **2026-09-23: No known vulnerabilities found** (انظر `pyproject.toml` المثبت: `pydantic==2.13.5`, `fastapi==0.141.1`, `google-genai==2.25.0`, `httpx==0.28.1`, `uvicorn==0.53.0`, `pyyaml==6.0.3`, `pytest==9.1.1`; أعد التشغيل قبل كل إصدار)
- `trivy image nimna:latest` — **لم يُشغّل في CI الحالي (Docker غير متوفر في بيئة الاختبار)** — شغّل عند بناء صورة النشر: `docker build -t nimna:latest . && trivy image nimna:latest` وتأكد من عدم وجود HIGH/CRITICAL

### المخاطر المعروفة والقيود
- `subprocess` ليس عزلًا أمنيًا — يتطلب موافقة دائمًا.
- `local-sandbox` يركّب `docker.sock` ويعادل صلاحيات واسعة جدًا على المضيف (حتى القراءة فقط غير كافية) — لا تستخدمه في الإنتاج أو CI غير موثوق.
- تغيّر صيغ المزودين (Gemini/OpenAI-compatible) قد يتطلب تحديث `providers/`.
- الحاوية تشترك في نواة المضيف — حدّث المضيف وبيئة التشغيل وفعّل seccomp/AppArmor/SELinux.
- الـ mock والاختبارات لا يثبتان نجاح الاتصال بمزود حقيقي.

### قواعد تشغيل إلزامية
```
لا تستخدم --profile local-sandbox على جهاز يحتوي بيانات حساسة
لا تشغّل الخدمة كـ root
لا تضع مفاتيح API داخل صورة Docker
لا تعتبر subprocess عزلًا أمنيًا
الـ mock والاختبارات لا يثبتان نجاح الاتصال بمزود حقيقي
```

### التوصية
حالة المشروع: **Release Candidate**. لا يُنصح بتسميته إصدار إنتاج نهائي إلا بعد نجاح اختبار Gemini/NVIDIA الحقيقي وفحص صورة النشر وبيئة rootless.

---

## قالب للإصدارات القادمة
```
- 77 اختبارًا ناجحًا
- تم اختبار المزود الحقيقي: نعم/لا (التاريخ، المزود، النتيجة)
- تم اختبار Docker sandbox الحقيقي: نعم/لا (الأمر، النتيجة)
- تم اختبار rootless runtime: نعم/لا (Docker rootless/Podman، النتيجة)
- تم تشغيل pip-audit: التاريخ والنتيجة
- تم تشغيل trivy image: التاريخ والنتيجة
- المخاطر المعروفة: subprocess، local-sandbox، تغيّر صيغ المزودين، مشاركة النواة
```
