# Nimna Roadmap — من RC إلى القيادة التقنية

> **القرار الهندسي الحالي (Sprint 1 — المسار 1 + 4 مصغر):** بناء قاعدة صلبة للأداء والأمان حول FastAPI/WebSockets قبل ضخ حركة حقيقية. هذا يمنع الانهيار تحت استدعاءات Vision المتكررة.

**الحالة الحالية:** `v0.1.0-rc1` — 8 مهارات / 22 أداة / 77 اختبار / فصل `computer_control ↔ code_execution` / WebSocket محصّن / واجهة Advanced Computer Use (Neon Boxes + Timeline + Terminal + AI Cursor)

---

## الرؤية — 4 مسارات استراتيجية

### 1. Performance & Scalability
- **Redis Caching Layer** للـ Vision Gateway: مفتاح `hash(16x16 grayscale + viewport)` → TTL 10د، يقلل 80% من استدعاءات Gemini Vision للواجهات المتكررة.
- **K8s HPA** — `min 2 max 10, CPU 65% + 80 WS` مع `Redis PubSub` لمزامنة حالة الجلسات بين pods.
- **gRPC داخلي** بين Planner ↔ Vision بدل `httpx` لتقليل 40ms/ hop.

### 2. Advanced AI Capabilities
- **Long-Term Memory** عبر Vector DB (Qdrant/Milvus) لاسترجاع سياق المشاريع وتفضيلات المستخدم.
- **Multi-Agent System** — Planner يوزع على وكلاء فرعيين (بحث / كود / رؤية) بالتوازي.
- **Self-Healing** في `code_execution` — تحليل `stderr` وإعادة كتابة الكود تلقائياً.

### 3. UX & Integration
- **Tauri Desktop App** — تغليف `index.html` الحالي كتطبيق خفيف مع وصول System APIs.
- **Plugin Ecosystem** — `SKILL.md` + `manifest.json` لأدوات مخصصة (تحكم في برامج هندسية...).
- **Analytics Dashboard** — مقاييس: زمن/أداة، معدل نجاح shell، استهلاك موارد.

### 4. Advanced Security & Monitoring
- **Heuristics Kill Switch (MVP الحالي)** → **ML Anomaly Detection** لاحقاً — مراقبة Terminal Logs لحظياً وقطع الجلسة.
- **Automated PenTesting in CI** — `trivy + zap + sandbox-escape` يفشل الـ PR.

---

## Sprint 1 — المسار 1 + 4 مصغر (أسبوعان) — **قيد التنفيذ الآن**

### الهدف
تحمل **200 جلسة متزامنة** باستقرار: `p95 Vision 2.8s → 0.6s`, لا فقدان جلسة عند HPA.

### التسليمات (Commits)
| الملف | الوصف | KPI |
|-------|-------|-----|
| `infra/redis/` + `docker-compose.yml` (redis) | Redis 7 + config `maxmemory 256mb allkeys-lru` | cache hit >70% |
| `nimna/vision/cache.py` | `VisionCache` — Redis مع fallback ذاكرة، مفتاح `vision:{hash}:{w}x{h}`, `get/set`, `hit/miss` metrics | latency -80% |
| `k8s/hpa.yaml` + `k8s/deployment.yaml` + `k8s/redis.yaml` | HPA + Deployment (2-10) + Redis PubSub لمزامنة `pending_runs` | 0 session loss |
| `security/anomaly.py` | `AnomalyDetector` — هيورستيك (`curl|sh`, `chmod +s`, تكرار `rm`, محاولة `escape workspace`), `should_kill(session, logs)` → `Kill Switch` | block 100% من القواعد |
| `nimna/core/agent.py` (تكامل) | استخدام `VisionCache` قبل استدعاء Gemini | — |
| `.github/workflows/ci.yml` (إضافة) | `trivy image` + `zap` + `sandbox-escape` check (`docker.sock` mount) | PR fails on vuln |

### معايير القبول
- `pytest -q` يمر مع Redis mock.
- `curl /api/health` يعيد `cache: {enabled, hit_rate}`.
- اختبار حمل: `k6 ws -c 200` → لا أخطاء، `p95 < 800ms`.

---

## Sprint+1 — المسار 2 (AI)
- Qdrant (Vector DB) للذاكرة طويلة الأمد — يعتمد على Redis الحالي.
- Multi-Agent (3 وكلاء متوازيين) — يزيد الحمل 3x لذا احتاج 1 أولاً.

## Sprint+2 — المسار 3 (UX)
- Tauri wrapper (نفس `index.html` — 90% جاهز) + Plugin SDK + Analytics فوق `audit_log`.

---

## الترتيب والاعتماديات
```
1 (Performance) + 4 MVP  →  2 (AI)  →  3 (UX)
基础 → ذكاء → توزيع
```
تأجيل 2 و 3 حتى تثبت 1 يمنع تضاعف تكلفة Vision.

---

## Epics (GitHub)

- Epic #1 — `perf/vision-cache` — Redis + `nimna/vision/cache.py`
- Epic #2 — `perf/k8s-hpa` — HPA + PubSub
- Epic #3 — `perf/grpc` — gRPC داخلي (لاحق)
- Epic #4 — `sec/anomaly-mvp` — Heuristics Kill Switch
- Epic #5 — `sec/pentest-ci` — Trivy/ZAP في CI

---

## كيف تشغّل محلياً (بعد Sprint 1)

```bash
docker compose --profile infra up -d redis
docker compose --profile computer up -d desktop
PORT=8001 REDIS_URL=redis://localhost:6379/0 nimna serve
# أو K8s
kubectl apply -f k8s/redis.yaml -f k8s/deployment.yaml -f k8s/hpa.yaml
```

---

## القرار
**نبدأ بـ Redis (الاختناق الأكبر) بالتوازي مع HPA** — Redis يقلل الحمل فوراً، HPA يضمن عدم سقوط الجلسات عند التوسع. سأدفع commits لـ `infra/redis-cache` و `nimna/vision/cache.py` أولاً، ثم `k8s/hpa.yaml` و `security/anomaly.py`.
