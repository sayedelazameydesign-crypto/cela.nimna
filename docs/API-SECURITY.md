# API security — operator runbook

The policy (what is enforced and why) lives in [`../SECURITY.md`](../SECURITY.md#api-access-control-http--websocket).
This page covers **how to operate it**: generating and rotating keys, per-platform setup,
calling the API, and troubleshooting. Code: `nimna/api/security.py`. Tests: `tests/test_auth.py`.

## ما قبل الدمج

> **بعد الدمج، أي نشر لا يملك `NIMNA_API_KEY` صالحاً يرفض الإقلاع** (`SecurityConfigError` عند استيراد
> `nimna.api.app:app`). FastAPI Cloud ينشر تلقائياً عند كل push إلى `main` — لذا تُنجز هذه القائمة **قبل** Merge.

### أ. متغيرات البيئة الجديدة

| المتغير | إلزامي في الإنتاج؟ | القيمة الافتراضية | ماذا يحدث إن غاب |
|---|---|---|---|
| `NIMNA_API_KEY` | **نعم** | لا يوجد | الإنتاج: رفض الإقلاع (`nimna serve` ⇒ exit 2؛ uvicorn/FastAPI Cloud ⇒ خطأ عند الاستيراد). القيمة الفارغة أو المسافات أو `" , "` = غائب. التطوير: الـ API لعملاء loopback فقط. قيمة أقصر من 32 أو ضعيفة أو بمحارف خارج `[A-Za-z0-9._~+-]` ⇒ رفض الإقلاع. عدة مفاتيح بفواصل للتدوير. |
| `NIMNA_ENV` | لا (غيابه = إنتاج) | `production` | يُعامَل كإنتاج ⇒ المفتاح مطلوب (fail-closed). أي قيمة غير `production`/`development` ⇒ رفض الإقلاع. لا تضع `development` على خادم عام. |
| `NIMNA_ALLOWED_ORIGINS` | لا | غير معرَّف | الإنتاج: كل الطلبات عبر-الأصل مرفوضة (الواجهة المدمجة على `/` من نفس الأصل فلا تتأثر). التطوير: `localhost`/`127.0.0.1` بأي منفذ. `*` أو أصل مشوَّه ⇒ رفض الإقلاع. |
| `NIMNA_CHAT_RATE_LIMIT` | لا | `30` (طلب/دقيقة/مفتاح على `POST /api/chat`) | يُستخدم 30. قيمة `< 1` أو غير رقمية ⇒ رفض الإقلاع (لا يمكن تعطيل الحد). |
| `NIMNA_WS_MAX_CONNECTIONS` | لا | `10` (اتصال متزامن/مفتاح على `/ws/*`) | يُستخدم 10. قيمة `< 1` أو غير رقمية ⇒ رفض الإقلاع. |
| `VNC_PASSWORD` | فقط مع `docker compose --profile computer` | لا يوجد | خدمة `desktop` وحدها تخرج بـ 64 قبل تشغيل VNC (غائب/ضعيف/أقل من 12 حرفاً). التطبيق وبقية الخدمات لا تتأثر. لا يقرؤه تطبيق Nimna. |

### ب. خطوات يدوية لدى مزوّد النشر

**FastAPI Cloud** (المزوّد الفعلي: GitHub Deployments على هذا المستودع ينشئها `fastapi-cloud[bot]`):

- [ ] ولّد مفتاحاً محلياً: `python -c "import secrets; print(secrets.token_urlsafe(32))"` واحفظه في مدير كلمات مرور.
- [ ] اعرض كل البيئات: `gh api repos/sayedelazameydesign-crypto/cela.nimna/deployments --jq '.[].environment' | sort -u` — عند كتابة هذا كانت اثنتان: `Production – celanimna` و`Production – celanimna-3ffa6b22`.
- [ ] أضف `NIMNA_API_KEY` في لوحة **كل** تطبيق منهما (App → Environment Variables). حفظ المتغير مع إعادة نشر الكود الحالي آمن: الكود القديم لا يقرؤه.
- [ ] لا تضف `NIMNA_ENV` (الافتراضي إنتاج) أو اجعله `production`؛ لا تضع `development` أبداً.
- [ ] أضف `NIMNA_ALLOWED_ORIGINS` فقط إن كانت واجهة على أصل آخر تستدعي الـ API.
- [ ] نفّذ `scripts/snapshot-prod.sh` (نسخة offline من `origin/main`: SHA + tarball + أسماء المتغيرات) وانسخ مجلد `.snapshots/` خارج الجهاز.
- [ ] انسخ **قيم** متغيرات البيئة من لوحة كل تطبيق يدوياً (السكربت يحفظ الأسماء فقط، عمداً).
- [ ] نفّذ `scripts/rollback-prod.sh --env "<البيئة>"` (قراءة فقط). **اليوم** يخرج بـ 1 (لا يوجد هدف: 55ef6d6 هو النشر الوحيد الناجح وهو الحي). **بعد الدمج** سيكون 55ef6d6 هدفاً في `Production – celanimna-3ffa6b22` سواء نجح نشر PR #20 أو فشل (مُختبر). إن لم يوجد هدف فالبديل `scripts/emergency-revert.sh` — انظر [Emergency Rollback (manual)](#emergency-rollback-manual).
- [ ] اقرأ قسم «Emergency Rollback (manual)» أدناه مرة واحدة قبل الدمج.
- [ ] تأكد أن كل العملاء (CLI، سكربتات، الواجهة عبر حقل المفتاح) سيحصلون على المفتاح.

**Render** (`render.yaml`):

- [ ] `NIMNA_API_KEY` معرَّف بـ `sync: false`: الصق المفتاح في Dashboard → Environment عند أول تطبيق للـ Blueprint (لا تستخدم Generate: ينتج `/` و`=` المرفوضة).

**Kubernetes** (`k8s/deployment.yaml` يقرأ `secretKeyRef: nimna-secrets/NIMNA_API_KEY` دون `optional`):

- [ ] `kubectl create secret generic nimna-secrets --from-literal=NIMNA_API_KEY="$NIMNA_API_KEY"` (أو أضف المفتاح إلى السر الموجود) **قبل** `kubectl apply` — وإلا يبقى الـ pod في `CreateContainerConfigError`.
- [ ] مع أكثر من replica: العدّادات لكل عملية؛ الحد الفعلي ≈ 30 × عدد النسخ، وسيفشل فحص [7] في preflight خلف موزِّع الحمل (قيد معروف).

**docker compose**:

- [ ] `NIMNA_API_KEY=` في `.env` (`chmod 600 .env`)؛ و`VNC_PASSWORD` فقط إن كنت تشغّل `--profile computer`.

### ج. بعد النشر مباشرة: `scripts/preflight-prod-check.sh`

```bash
export NIMNA_API_KEY=...              # نفس مفتاح الإنتاج؛ لا يُطبع ولا يظهر في ps (يُمرَّر لـ curl عبر ملف 0600)
scripts/preflight-prod-check.sh https://<app-host>              # أو --chat-limit N إن غيّرت NIMNA_CHAT_RATE_LIMIT
```

| # | الفحص | المتوقع |
|---|---|---|
| 1 | `GET /` | 200 |
| 2 | `GET /api/health` | 200 + JSON فيه `"status": "ok"` |
| 3 | `GET /api/chat` بلا مفتاح | 401 |
| 4 | `GET /api/chat` بالمفتاح الصحيح | 200 أو 4xx غير 401 (عملياً 405: المسار POST فقط، أي أن المصادقة نجحت) |
| 5 | `GET /api/chat` بمفتاح خاطئ | 401 |
| 6 | الترويسات الثلاث على `/` و`/api/health` واستجابة 401 | `nosniff`، `DENY`، `strict-origin-when-cross-origin` |
| 7 | 31 × `POST /api/chat` بجسم `{}` | 1..30 ليست 429 (عملياً 422 قبل تشغيل أي نموذج، فلا تكلفة)، والـ 31 = 429 مع `Retry-After` بين 1 و60 |

أكواد الخروج: `0` كل الفحوص نجحت · `1` فشل فحص · `2` خطأ شبكة/TLS (يتوقف فوراً؛ لا `-k` ولا تجاهل للأخطاء) ·
`64` استخدام خاطئ (مفتاح غائب، `http://` لغير loopback). يستهلك الفحص حصة المفتاح لدقيقة: لا تُعِده قبل 60 ثانية.

### د. التراجع: `scripts/rollback-prod.sh`

```bash
scripts/rollback-prod.sh --env "Production – celanimna-3ffa6b22"            # الافتراضي: dry-run، لا يغيّر شيئاً
scripts/rollback-prod.sh --env "Production – celanimna-3ffa6b22" --execute  # يفتح PR تراجع
```

- **الافتراضي dry-run:** يقرأ GitHub Deployments وحالاتها ويطبع `ROLLBACK TARGET SHA: <sha>` ثم يخرج بـ 0.
- **اختيار الهدف:** إن كان أحدث نشر فاشلاً ⇒ آخر نشر ناجح (ما يزال المزوّد يخدمه)؛ وإلا ⇒ أحدث نشر ناجح أقدم بـ SHA مختلف.
  النشر «ناجح» إن احتوى تاريخ حالاته على `success` (GitHub يحوّل النشر الأقدم إلى `inactive`).
- **`--execute`:** ينشئ commit جديداً فوق الفرع الافتراضي شجرته مطابقة تماماً للـ SHA المستهدف (لا force-push ولا إعادة كتابة للتاريخ)،
  على فرع `rollback/<sha7>-<timestamp>`، ويفتح PR. **دمج ذلك الـ PR هو ما يطلق نشر FastAPI Cloud.** يحتاج `gh` بصلاحية `repo`
  (و`workflow` إن كانت الشجرة المستعادة تغيّر `.github/workflows`).
- إنشاء «deployment» عبر GitHub API لا يعيد النشر في FastAPI Cloud، لذا لا يفعل السكربت ذلك.
- ⚠️ **التراجع إلى commit سابق لهذا الـ PR يعيد الـ API بلا مصادقة.** إن كانت المشكلة مفتاحاً ناقصاً فالحل الأسرع إضافة `NIMNA_API_KEY` في لوحة المزوّد وإعادة النشر، لا التراجع.
- أكواد الخروج: `0` خطة/PR/لا شيء لفعله · `1` لا يوجد هدف تراجع · `2` خطأ `gh`/API · `64` استخدام خاطئ (مثلاً عدة بيئات بلا `--env`).

## Emergency Rollback (manual)

**What FastAPI Cloud actually does** ([Deployments](https://fastapicloud.com/docs/builds-and-deployments/deployments/),
[GitHub Integration](https://fastapicloud.com/docs/source-control/github-integration/), [`fastapi deploy`](https://fastapicloud.com/docs/fastapi-cloud-cli/deploy)):

- A push to the default branch deploys **every** connected app. The rollout is zero-downtime, and **if a new deployment fails, the last successful deployment stays live.**
  So a deploy that fails to boot (for example because `NIMNA_API_KEY` is missing) does **not** take the site down. It keeps serving the previous code, which is the unauthenticated pre-PR-20 code.
  A revert is needed when the new deployment **succeeds but is broken** (for example, clients can't authenticate).
- **Save and Redeploy** re-releases the *current image* with the new configuration, without rebuilding from source.
- You can't upload a tarball. The only non-GitHub path is `fastapi deploy <folder> --app-id <id>` (FastAPI Cloud CLI, logged in).
- FastAPI Cloud has no deploy hook to call. Merging to `main` is the trigger.

**When `scripts/rollback-prod.sh` has a target** (GitHub Deployments history):

| Situation | Target in `Production – celanimna-3ffa6b22` |
|---|---|
| Today, before merging | **none** (exit 1): 55ef6d6 is the only successful deployment and it is live |
| PR #20 merged, its deploy **failed** | 55ef6d6 (the newest failed; the last success is still live) |
| PR #20 merged, its deploy **succeeded** | 55ef6d6 (the earlier success with a different SHA) |
| `Production – celanimna` (3 failures, 0 successes) | **none, ever**, until that app has a successful deployment |

`scripts/emergency-revert.sh` doesn't use Deployments history at all. It works on a repo that has never had a successful deploy.

**Before merging:** `scripts/snapshot-prod.sh` writes the file set below, and you copy `.snapshots/` off the machine:
- `.snapshots/<ts>.sha`
- `<ts>.tar.gz` and `<ts>.tar.gz.sha256`
- `<ts>.env.names` (names only, never values)

Record the env var **values** from each app's dashboard by hand.

**If a deploy after merging PR #20 is broken:**

1. Open the FastAPI Cloud dashboard → each app → **Deployments**. If the new deployment is `Build Failed` / `Verification Failed`, the previous one is still **Live**. Read the logs. A missing or invalid key is fixed with **Save and Redeploy** after setting `NIMNA_API_KEY`, not with a revert.
2. **Don't delete `NIMNA_API_KEY`.** The pre-PR-20 code ignores it, so it doesn't need to go. Deleting it and pressing Save and Redeploy re-releases the *current* image without a key, and that fails verification. You'll also need the key again when you re-apply the change.
3. Create the revert:
   - Dry run: `scripts/emergency-revert.sh --pr 20`. It shows the diffstat and confirms the result is IDENTICAL to the pre-merge tree.
   - Then run it with `--execute`. This opens the PR **"EMERGENCY REVERT: &lt;sha&gt;"**.
   - By hand, with the same result: `git revert -m 1 <merge-commit-sha>` for a merge commit, or `git revert <sha>` without `-m` for a squash merge.
4. Merge the revert PR. If branch rules allow it, `git push origin main` with the manual revert works too. The push to `main` **is** the deploy.
5. FastAPI Cloud redeploys every connected app automatically. Wait for **Ready** in each app's Deployments view.
6. Verify: `curl -fsS https://<app>/api/health` returns 200 and `curl -fsS -o /dev/null https://<app>/` returns 200.
   Don't use `preflight-prod-check.sh` here: the old code has no auth, so it is supposed to fail.
7. If the GitHub path is unavailable, redeploy the snapshot directly:
   ```bash
   sha256sum -c .snapshots/<ts>.tar.gz.sha256
   mkdir /tmp/nimna-restore && tar -xzf .snapshots/<ts>.tar.gz -C /tmp/nimna-restore
   fastapi deploy /tmp/nimna-restore --app-id <app-id>
   ```
   The next push to `main` replaces this deployment, so revert `main` as well.

> ⚠️ Reverting PR #20 brings the API back **without authentication** (everything on `/api/*` is public again).
> Keep the revert as short as possible. Re-apply the change with `git revert <revert-commit>` once the cause is fixed.

## 1. Generate a key

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 43 chars, [A-Za-z0-9_-]
```

Production rules (checked at boot): at least 32 characters, characters limited to `[A-Za-z0-9._~+-]`
(valid both in an HTTP header and in a WebSocket sub-protocol token), not a placeholder or low-entropy value.
Base64 values containing `/` or `=` (e.g. Render `generateValue`) are refused on purpose.

Store it in the platform secret manager / `.env` (`chmod 600`). Never commit it, never put it in a URL.

## 2. Rotate a key without downtime

```bash
NIMNA_API_KEY=<old>,<new>    # 1. deploy with both
                             # 2. move every client to <new>
NIMNA_API_KEY=<new>          # 3. deploy with only the new key
```

Each key has its own rate-limit bucket and WebSocket budget.

## 3. Per-platform setup

| Platform | What to set |
|---|---|
| FastAPI Cloud | `NIMNA_API_KEY` in the app's environment variables (`NIMNA_ENV` can stay unset: production is the default). |
| Render | `render.yaml` already pins `NIMNA_ENV: "production"` and declares `NIMNA_API_KEY` as `sync: false` → paste the key in the dashboard before *Manual Deploy*. See [`DEPLOY-RENDER-FREE.md`](DEPLOY-RENDER-FREE.md). |
| Kubernetes | `kubectl create secret generic nimna-secrets --from-literal=NIMNA_API_KEY=<key> …` — the reference in `k8s/deployment.yaml` is **not optional**, so a missing key blocks the pod from starting. |
| docker compose | `NIMNA_API_KEY=<key>` in `.env` (loaded through `env_file`). The `desktop` profile also needs `VNC_PASSWORD` (≥ 12 chars, not a default). `nimna-sandbox` is **DEV ONLY — لا تنشر**. |
| Local dev | Either set a key, or `NIMNA_ENV=development` without one: the API is then served to loopback clients (`127.0.0.1`, `::1`) only. |

`nimna doctor --offline` prints the effective posture, for example
`✅ API security: env=production, auth=api-key (1 key), cors=deny all, chat=30/min/key, ws=10/key`.

## 4. Calling the API

```bash
# public
curl -s "$BASE/api/health"
# everything else
curl -s "$BASE/api/tools" -H "X-Nimna-Key: $NIMNA_API_KEY"
curl -s "$BASE/api/chat"  -H "X-Nimna-Key: $NIMNA_API_KEY" -H 'Content-Type: application/json' \
     -d '{"message":"hello"}'
# WebSocket (CLI clients can send the header)
websocat -H "X-Nimna-Key: $NIMNA_API_KEY" "wss://<host>/ws/my-session"
```

Browser / JavaScript (browsers cannot set headers on a WebSocket, so the key rides in a sub-protocol
that the server never echoes back):

```js
await fetch('/api/tools', { headers: { 'X-Nimna-Key': key } });
const ws = new WebSocket(`wss://${location.host}/ws/my-session`, ['nimna.v1', 'nimna.key.' + key]);
```

The built-in UI on `/` does this for you: it asks for the key on the first `401`, keeps it in
`sessionStorage` (per tab), and the 🔑 button changes or clears it.

## 5. CORS

- The built-in UI is same-origin: **no CORS entry is needed**.
- A separate frontend (e.g. the Next.js app under `web/`) needs its exact origin:
  `NIMNA_ALLOWED_ORIGINS=https://app.example.com,http://localhost:3000`.
- `*`, wildcard hosts, paths, and `user@host` are rejected at boot. Credentials (cookies) are never allowed;
  auth is the header only.

## 6. Limits

| Limit | Default | Response |
|---|---|---|
| `POST /api/chat` per key | 30 / minute (sliding window) | `429` + `Retry-After: <seconds>` |
| Concurrent `/ws/*` per key | 10 | close `1008` before accept (HTTP 403) |
| Messages per WebSocket | 10 / second (pre-existing) | `{"type":"error"}` |

The counters are per process. With several workers/replicas, the effective ceiling is
`limit × processes`. Put a shared limiter (Redis / API gateway) in front if you need a global cap.

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Boot fails: `NIMNA_API_KEY is required when NIMNA_ENV=production` | No key in the environment | Set it (§1, §3) |
| Boot fails: `… is too short` / `placeholder` / `characters outside` | Weak or base64 key | Regenerate with `token_urlsafe(32)` |
| Boot fails: `NIMNA_ALLOWED_ORIGINS …` | Wildcard or non-origin entry | Use `scheme://host[:port]` only |
| `401 {"detail":"missing or invalid API key"}` | Header missing/wrong | Send `X-Nimna-Key` |
| `401` in development without a key | Request did not come from loopback (Docker bridge, LAN, proxy) | Set a key |
| `429` on `/api/chat` | More than 30 requests in the last minute for that key | Wait `Retry-After` seconds |
| WebSocket closes immediately (1008 / HTTP 403) | Bad key, or 10 connections already open for that key | Check the key; close idle tabs |
| `desktop` container exits with `nimna-vnc-guard: …` | `VNC_PASSWORD` empty/weak/short | Set a unique ≥ 12-char value in `.env` |
