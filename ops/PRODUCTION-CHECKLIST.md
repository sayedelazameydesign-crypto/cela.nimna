# Production Checklist — cela.nimna

| # | البند | كيف يُثبت | حالة |
|---|---|---|---|
| 1 | T8 `9885f3c` في تاريخ HEAD | `git merge-base --is-ancestor 9885f3c33b908b131855db4e8ac1277c881ea56c HEAD` | محلياً ✔ |
| 2 | CI بلا أقنعة (`continue-on-error`/`\|\| true`/`fail_action: false`) | `bash scripts/check_ci_falsifiable.sh .github/workflows/ci.yml` | محلياً ✔ |
| 3 | syntax (compileall) | خطوة 3 في البوابة | محلياً ✔ |
| 4 | السيت الكامل | خطوة 4 (318 passed) | محلياً ✔ |
| 5 | pip-audit نظيف + سلسلة أدوات مقوّاة (pip≥26.2, setuptools≥83) | خطوة 5 | محلياً ✔ (بعد الإصلاح) |
| 6 | صحة حية (HTTP حقيقي، mock provider) | خطوة 6 — جسد JSON يُطبع كدليل | محلياً ✔ |
| 7 | `docker build --pull` للمرشح | خطوة 7 — **لا تخطي** | ✔ CI (run 35972996854) |
| 8 | CI `production-gate` أخضر على GitHub | run 35972996854 + 35972994919 (push+PR) — كل الوظائف الست success | ✔ |
| 9 | PR → main مفتوح + مراجعة المالك | gh pr view | يُفتح مع هذا الالتزام |

## أمن الـAPI (PR: security: enforce auth, CORS, rate-limit, headers)

| # | البند | كيف يُثبت | حالة |
|---|---|---|---|
| 10 | `NIMNA_API_KEY` مضبوط في **كل** بيئة نشر (FastAPI Cloud / Render / k8s) قبل الدمج | الخادم يرفض الإقلاع بدونه — `docs/API-SECURITY.md` §0 | على المالك |
| 11 | `/api/*` بلا مفتاح ⇒ 401، ومع المفتاح ⇒ 200 | خطوة 6 في البوابة + ZAP job (`AUTH GATE BROKEN`) | CI |
| 12 | ترويسات `nosniff` / `DENY` / `strict-origin-when-cross-origin` على الخادم الحي | ZAP job (`curl -sI /`) | CI |
| 13 | `NIMNA_ALLOWED_ORIGINS` = أصول صريحة فقط (أو فارغ = رفض الكل) | `nimna doctor --offline` → `cors=…` | على المالك |
| 14 | `VNC_PASSWORD` فريد ≥ 12 حرفاً إن استُخدم profile `computer` | `infra/desktop/vnc-guard.sh` يرفض غير ذلك | على المالك |
