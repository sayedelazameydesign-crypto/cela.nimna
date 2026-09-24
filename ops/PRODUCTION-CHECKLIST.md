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
