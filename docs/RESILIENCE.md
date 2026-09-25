# Nimna — Resilience & Scaling (قابلية التوسع ومنع الانهيار الكارثي)

> الهدف ليس «غير قابل للهدم» بل **غير قابل للانهيار الكارثي**: إذا سقط جزء،
> يبقى النظام يعمل ويتعافى تلقائيًا. كل ادعاء هنا مدعوم بملف/اختبار قابل
> للتشغيل — ما لم يُنفَّذ مذكور صراحةً في «خارج النطاق».

---

## 1) أين يقف النظام اليوم (2026-09-25)

| المكوّن | الشكل | التوسع | الفشل |
|---|---|---|---|
| API (`nimna serve`) | FastAPI + uvicorn، نسخ عديمة الحالة ظاهريًا | replicas خلف Service/LB + HPA (`k8s/`) | أي نسخة تسقط تُستبدل (probes + RollingUpdate + PDB) |
| الحالة | SQLite واحد (`data/nimna.db`, WAL) | **عمودي فقط** — هذه هي الحدود الصريحة (لا sharding) | نسخ احتياطي + استرجاع مُختبَر (`scripts/backup_sqlite.py`) |
| Rate limit | ذاكرة محلية، أو Redis مشترك عند ضبط `REDIS_URL` | bucket واحد لكل النسخ مع Redis | Redis يسقط ← fail-open محسوب ومُراقَب |
| Cache (رؤية) | Redis + fallback ذاكرة | مشترك عبر Redis | يسقط ← ذاكرة محلية (JSON آمن، بلا pickle) |
| Vector memory | Qdrant + fallback hash محلي | Qdrant خارجي | يسقط ← fallback تلقائي |
| المزوّد (LLM) | Gemini/NVIDIA/OpenAI/mock | — (خارجي) | breaker + bulkhead + retry/jitter |

---

## 2) SLO / SLI (أهداف قابلة للقياس)

| SLI | المصدر | الهدف |
|---|---|---|
| توافر `GET /api/health == 200` | blackbox probe خارجي | ‎99.9% شهريًا (k8s) |
| `p95(http_request_duration_seconds)` لمسارات القراءة | `GET /api/metrics` | ‎< 1s |
| نسبة `chat_runs_total{status="done"}` | `/api/metrics` | ‎> 99% (mock)، يُقاس منفصلًا لكل مزوّد حي |
| `rate_limit_hits_total` | `/api/metrics` | تنبيه عند قفزة مفاجئة (هجوم/عميل مكسور) |
| `breaker_transitions_total{to_state="open"}` | `/api/metrics` | أي انتقال = تنبيه (المزوّد ميت) |
| `rate_limiter_errors_total` | `/api/metrics` | أي قيمة > 0 = تنبيه (Redis متعثر) |
| عمر أحدث نسخة DB احتياطية | cron + `backup_sqlite.py --verify` | ‎< 15 دقيقة |

> لا توجد SLOs للزمن على `/api/chat` الحي — زمنه يملكه مزوّد النموذج، لا نحن.
> ما نملكه: fail-fast (breaker/bulkhead) بدل الانتظار الأعمى.

---

## 3) التوسع الأفقي (كيف تُشغَّل نسخ متعددة)

```bash
kubectl apply -f k8s/          # Deployment(replicas=2) + PDB + HPA + Service
```

1. **عديم الحالة ظاهريًا**: أي نسخة تجيب أي طلب — الجلسات والموافقات و
   Idempotency في SQLite المشترك… **بشرط واحد**: كل النسخ يجب أن ترى **نفس
   ملف SQLite** (مجلد مشترك/NFS) أو تُشغَّل نسخة واحدة للكتابة. SQLite ملف
   واحد — لا تدّعي أكثر من ذلك (انظر «خارج النطاق»).
2. **Rate limit مشترك**: اضبط `REDIS_URL` على كل النسخ فيتحول المحدد تلقائيًا
   من ذاكرة محلية إلى Lua ذرية على Redis (`nimna/api/security.py`).
3. **Anti-affinity + PDB**: النسخ تتوزع على nodes/zones (`preferred`)، و
   `minAvailable: 1` يمنع إخلاء الكل دفعة واحدة.
4. **HPA**: `minReplicas: 2` — التصعيد تلقائي حسب CPU/memory.

---

## 4) مصفوفة التدهور السلس (ماذا يحدث عند كل فشل)

| الفشل | السلوك | الدليل |
|---|---|---|
| مزوّد LLM ميت (5 إخفاقات متتالية) | القاطع يفتح 30s ويفشل سريعًا مع `retry_after` | `nimna/resilience/` + `/api/health → resilience.breaker` |
| ضغط متزامن > 16 استدعاء نموذج | Bulkhead يرفض الفائض (retryable) بدل تكديس الخيوط | `BulkheadFullError` + `bulkhead` في health |
| 429/5xx عابرة من المزوّد | retry ×3 بتراجع أُسّي + jitter كامل (بلا قطيع رعدي) | `with_retries(..., jitter=True)` |
| Redis ساقط | limiter يفتح (fail-open) + cache ذاكرة + عدّاد خطأ | `rate_limiter_errors_total` |
| Qdrant ساقط | fallback hash محلي 768-dim | `memory.provider: fallback` في health |
| عميل يُعيد المحاولة (timeout/شبكة) | `Idempotency-Key`: نفس المفتاح+الجسم = نفس الرد المخزّن؛ سباق أثناء التنفيذ = 409؛ جسم مختلف = 422 | `idempotency_keys` + `Idempotent-Replayed` |
| فيض طلبات | 429 + `Retry-After` على `/api/chat`؛ WS محدودة لكل مفتاح | `rate_limit_hits_total` |
| نسخة ميتة/نشر جديد | readiness تُخرجها من الخدمة، RollingUpdate بصفر انقطاع | probes + `maxUnavailable: 0` |
| SQLite تالف/مفقود | الاسترجاع من نسخة مُتحقَّق منها (إجراء مُختبَر) | القسم 5 |

---

## 5) النسخ الاحتياطي والاسترجاع (RPO/RTO)

- **الأداة**: `scripts/backup_sqlite.py` — نسخ عبر SQLite backup API +
  `sha256` + `integrity_check` على النسخة قبل اعتمادها. لا شيء نصف مكتوب
  (temp + rename ذرية).
- **RPO** (أقصى فقدان بيانات): = الفاصل بين النسخ. الموصى به cron كل
  **15 دقيقة** خارج المضيف. على Render Free (قرص ephemeral) النسخ للذاكرة
  فقط — موثّق في `docs/DEPLOY-RENDER-FREE.md`، لا تدّعي بقاءً هناك.
- **RTO** (زمن العودة): دقائق — `backup_sqlite.py --restore <file> --db
  data/nimna.db` يتحقق أولًا ثم يستبدل مع نسخة أمان `.pre-restore-*.bak`.
- **اختبار الاسترجاع**: `tests/test_backup_sqlite.py` يجري الدورة كاملة
  (نسخ ← تحقق ← تدمير ← استرجاع ← قراءة الصفوف) في كل CI.

```bash
# cron مقترح (خارج المضيف):
*/15 * * * * python /app/scripts/backup_sqlite.py --out-dir /backups/nimna --retain 96
python /app/scripts/backup_sqlite.py --verify /backups/nimna/$(ls -t /backups/nimna/*.db | head -1)
```

---

## 6) المراقبة (ماذا تراقب وأين)

- **`GET /api/metrics`** (بمفتاح API): exposition بصيغة Prometheus بلا
  اعتماديات جديدة — عدّادات الطلبات والمدد والـ breaker والـ limiter
  وإعادة التشغيل. كل نسخة تُكشَط على حدة والتجميع بـ `sum by`.
- **`GET /api/health`** (عام): يتضمن قسم `resilience` (حالة القاطع،
  bulkhead، backend المحدد، إدخالات idempotency، uptime).
- **`X-Request-ID`**: يُولَّد لكل طلب ويُعاد في الرد — اربط به السجلات.
- **وصفة الكشط** (المقاييس خلف `X-Nimna-Key` عمدًا — لا بيانات حساسة فيها
  لكن سياسة default-deny تُطبَّق على كل المسارات غير العامة):
  ```bash
  curl -H "X-Nimna-Key: $KEY" https://API/api/metrics
  # Prometheus Operator: استخدم وكيلًا يحقن الترويسة أو cron يدفع عبر Pushgateway.
  ```

---

## 7) Runbooks (ماذا تفعل عند…)

### القاطع مفتوح (`resilience.breaker.state == "open"`)
1. تحقق من المزوّد (صفحة الحالة / الحصة / المفتاح).
2. لا شيء تُعيد تشغيله — نصف الفتح يُدخل probe تلقائيًا بعد 30s ويُغلق عند
   أول نجاح. إن أصلحت السبب وأردت الإغلاق الفوري: أعد تشغيل النسخ (القاطع
   داخل العملية).
3. راجع `breaker_transitions_total` لزمن أول فتح.

### `rate_limiter_errors_total > 0` (Redis متعثر)
1. النظام يعمل (fail-open) لكن بلا حد مشترك — أولوية متوسطة.
2. أصلح Redis؛ لا حاجة لإعادة تشغيل API (كل hit تُعاد محاولته).
3. إن طال العطل: خفّض `NIMNA_CHAT_RATE_LIMIT` مؤقتًا؟ لا — الحد المحلي ما
   يزال… **تنبيه**: مع Redis مضبوط لا يوجد حد محلي احتياطي — القرار الموثّق
   هو fail-open الكامل. راقب `http_requests_total` للشذوذ أثناء العطل.

### فيض 429 (`rate_limit_hits_total` يقفز)
1. حدّد المفتاح/العميل من السجلات (بالـ request IDs).
2. عميل مكسور (retry بلا backoff) ← أصلحه وأضف `Idempotency-Key`.
3. هجوم ← أدر المفاتيح (`NIMNA_API_KEY` متعددة بالتناوب) واحجب المصدر عند LB.

### نشر سيئ
1. `kubectl rollout undo deployment/nimna-api` (أو FastAPI Cloud: أعد نشر
   لقطة `.snapshots/` — انظر `scripts/snapshot-prod.sh`).
2. بوابة الإنتاج `scripts/production_gate.sh` تمنع الأسوأ قبل الدمج.

### SQLite تالف
القسم 5 — الاسترجاع مُختبَر. لا تحذف `.pre-restore-*.bak` قبل التحقق من عمل الخدمة.

---

## 8) إثبات الصمود (chaos + load)

```bash
# دخان حمل (staging بمزوّد mock — بلا quota):
python scripts/load_probe.py --base-url http://127.0.0.1:8000 --requests 50 --concurrency 4
# ومع chat (يحذّر: ينفق quota على المزوّدين الحقيقيين):
NIMNA_API_KEY=$KEY python scripts/load_probe.py --base-url ... --include-chat --requests 20
```

تمارين chaos المقترحة (يدوية، على staging):

| التمرين | التوقع |
|---|---|
| `kubectl delete pod` أثناء حمل | صفر أخطاء مرئية (replica ثانية + readiness) |
| إيقاف Valkey | `rate_limiter_errors_total` يرتفع، والطلبات تنجح |
| إيقاف Qdrant | `memory.provider` يتحول لـ `fallback` |
| مفتاح مزوّد خاطئ | 5 إخفاقات ثم القاطع يفتح (fail-fast بدل التعليق) |
| `kubectl drain` لعقدة | PDB يُبقي نسخة تخدم |

---

## 9) خارج النطاق (عمدًا، مع السبب)

| البند | السبب |
|---|---|
| Kafka/RabbitMQ/SQS | لا توجد مهام خلفية بعد — الـ agent متزامن لكل طلب. Queue تُضاف مع أول worker حقيقي، لا قبله (YAGNI). |
| Postgres/sharding | SQLite+WAL يكفي الحمل الحالي؛ الترحيل مخطط له عند الحاجة الأفقية الحقيقية للكتابة. الحدود موثقة لا مخفية. |
| Service mesh / microservices | Modular monolith أولًا (نفس مبدأ القائمة) — الشبكة الداخلية صفر. |
| Multi-region نشط | Multi-AZ عبر anti-affinity هو الخطوة المتناسبة؛ الأقاليم تُضاف مع RTO أصغر من دقائق. |
| Vault | الأسرار عبر env + تناوب مفاتيح + عدم تسريب في السجلات — كافٍ لهذا الحجم. |
| OpenTelemetry SDK | request IDs + Prometheus exposition هما الخطوة المتناسبة؛ OTel تالٍ عند تعدد الخدمات. |
| Event sourcing/CQRS/Saga/Outbox | لا معاملات موزعة — SQLite معاملة واحدة. تُعاد الزيارة مع Postgres + queue. |

---

## 10) خريطة الملفات

| القدرة | الكود | الاختبار |
|---|---|---|
| Breaker + bulkhead | `nimna/resilience/` | `tests/test_resilience.py` (17) |
| Retry + jitter | `nimna/providers/base.py::with_retries` | نفسه أعلاه |
| Request-ID + metrics + cache | `nimna/api/telemetry.py` | `tests/test_telemetry.py` (11) |
| Idempotency | `nimna/memory/store.py` + `nimna/api/app.py` | `tests/test_idempotency.py` (10) |
| Redis limiter | `nimna/api/security.py` | `tests/test_redis_limiter.py` (8) |
| نسخ DB | `scripts/backup_sqlite.py` | `tests/test_backup_sqlite.py` (5) |
| مسبار حمل | `scripts/load_probe.py` | `tests/test_load_probe.py` (4) |
| k8s (affinity/PDB/HPA) | `k8s/` | `tests/test_k8s_resilience.py` (7) |
