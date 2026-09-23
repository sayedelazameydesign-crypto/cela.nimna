# Nimna Agent OS Blueprint

هذا المستند يحول فكرة **Code → Runtime → Tools → Memory → Models →
Governance → Evidence → Tests → CI → Release** إلى حدود تناسب المستودع الحالي.
لا ندعي أن عدد الملفات أو اسم Claude/Manus يثبت القدرة؛ كل claim له حالة
ودليل في [CAPABILITY-MATRIX](../CAPABILITY-MATRIX.md).

## ما هو موجود الآن

```text
CLI / Web / REST
       ↓
Mission Runtime (nimna/core/agent.py)
       ├── Skill selector + progressive SKILL.md loading
       ├── ModelProvider boundary + ModelRegistry + hard CostGuard
       ├── ToolRegistry → scope → validation → PolicyEngine → approval
       ├── SQLite MemoryStore (source of truth) + optional vector index
       ├── Sandbox / workspace jail
       └── audit_log → SHA-256 Evidence Journal → runtime fingerprint
```

الطبقة الجديدة لا تستبدل ما يعمل. هي تضيف boundaries قابلة للاختبار:

| Boundary | مكانه | القرار |
|---|---|---|
| Model registry / cost | `nimna/models/` | أي نموذج يجب أن يعلن capabilities وcost profile؛ unknown يفشل مغلقاً عند `$0`. |
| Governance | `nimna/governance/` + `core/approval.py` | المحتوى غير الموثوق لا يمنح أداة صلاحية. |
| Evidence / provenance | `nimna/evidence/`, `nimna/provenance/` | SQLite canonical؛ hash chain ليس توقيعاً قانونياً. |
| Observability | `nimna/observability/` + audit API | mission/run/tool/model/evidence قابلة للتتبع. |
| Browser Use V4 | `nimna/browser/`, `skills/browser_use/` | opt-in، confirm-gated، budget-gated، stop in `finally`. |
| Web UI / API | `nimna/api/` | `/api/models`, `/api/capabilities`, `/api/provenance`, `/api/runs/{id}/evidence`. |

## مبادئ التشغيل

1. **Runtime أولاً:** الواجهة لا تستدعي LLM أو أدوات خارج الـruntime مباشرة.
2. **النموذج مورد قابل للتبديل:** الـplanner والـverifier وswarm يمرون من
   Provider boundary نفسه.
3. **MAX_SPEND=0 بوابة فعلية:** profile مجاني معلن فقط يمر. سعر unknown لا
   يمر في hard mode، ولا يكفي تسجيل warning.
4. **SQLite مصدر الحقيقة:** Qdrant/fallback طبقة استرجاع، وليست سجل القرار.
5. **Scope قبل execution:** لا يكفي أن تكون الأداة مسجلة؛ يجب أن تكون ضمن
   skill scope، ثم تُفحص arguments، ثم policy، ثم approval، ثم التنفيذ.
6. **المحتوى الخارجي untrusted:** صفحة الويب أو نتيجة البحث لا تغيّر السياسة.
7. **Evidence لا claim:** status `implemented` يجب أن يشير إلى كود/اختبار؛
   `configured` يعني وجود إعداد فقط، وليس نجاحاً في الإنتاج.
8. **Recovery bounded:** المهلة، الخطوات، tool calls، retries، cost كلها حدود
   مستقلة؛ timeout العميل لا يعني إلغاء العمل الخارجي.

## Browser Use V4

التكامل السحابي منفصل عن Computer Control المحلي. راجع
[`docs/browser_use_v4.md`](../browser_use_v4.md). القواعد غير القابلة للتفاوض:

- `X-Browser-Use-API-Key` بلا `Bearer`؛
- فحص `/api/v2/billing/account` للسعة والرصيد؛
- `X-RateLimit-Limit` نافذة 5 ثوانٍ؛ احترام `Retry-After`؛
- stop صريح للمتصفح المملوك عبر `PATCH /api/v4/browsers/{id}` داخل `finally`؛
- `GPT-6 Astra`: `low|medium|high|xhigh|max` فقط؛
- الأداة مغلقة والميزانية صفر افتراضياً.

## مراحل الترقية دون كسر النسخة الحالية

### المرحلة A — حدود قابلة للإثبات (موجودة في هذه النسخة)

- Model Registry + CostGuard؛
- Policy vocabulary مع approval الحالي؛
- Evidence hash chain وruntime manifest؛
- Browser V4 REST adapter لا يحتاج SDK؛
- API endpoints ومصفوفات capability/verification؛
- CI integrity gate.

### المرحلة B — تشغيل إنتاجي

- PostgreSQL بديل لمخزن SQLite عند multi-replica، مع migration اختبارية؛
- queue/worker فعلي مع idempotency keys؛
- OpenTelemetry/Prometheus exporter فعلي لا مجرد audit summary؛
- sandbox خارجي (microVM/container service) بدل اعتبار subprocess عزلاً أمنياً؛
- secrets manager وOIDC وRBAC متعدد المستخدمين.

### المرحلة C — توسيع capabilities

- MCP gateway بسياسة ومصادقة؛
- connectors مجزأة (Gmail/Drive) بصلاحيات least privilege؛
- browser/computer agents مستقلون لكن يرثون نفس Runtime/Memory/Governance؛
- long-horizon وArabic/multi-agent evaluations؛
- SBOM، signed releases، وSLSA provenance.

## Definition of Done للقدرة

```text
code exists
  + scoped boundary
  + deterministic offline test
  + integration/contract test where external
  + policy and failure behavior
  + audit/evidence event
  + capability row and verification row
= claim may become implemented
```

Mock أو fallback يثبت contract/offline behavior فقط، ويجب أن يظهر كـ`MOCKED` أو
`PARTIAL` لا كـ`PASS production`.
