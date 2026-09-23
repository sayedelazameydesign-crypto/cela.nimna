# Nimna — Release Notes (Release Candidate)

> **محصّن وفق نطاق الاختبارات الحالية — ليس إثباتًا للأمان المطلق.**
> الحاويات تشترك في نواة المضيف؛ حافظ على المضيف وDocker محدثين واستخدم seccomp/AppArmor أو SELinux.

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
- Docker sandbox الحقيقي وrootless runtime وPostgreSQL multi-replica: ما زالت موثقة كـ`partial` أو `planned` في `docs/CAPABILITY-MATRIX.md`.
- MCP Gateway: صار `implemented` بعد ربطه بحلقة الوكيل (`nimna/mcp/registry.py` + مهارة `mcp_servers`). يبقى `G18` (`BLOCKED`) لأن اختبار خادم طرف ثالث حقيقي لم يُشغَّل، والبوابة معطّلة افتراضياً.

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
