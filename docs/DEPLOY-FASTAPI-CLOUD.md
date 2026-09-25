# ربط ومزامنة GitHub ↔ FastAPI Cloud

> **ما يثبته هذا الملف — وما لا يثبته.** هذا **عقد ربط**. وجود الملفات لا يعني أن
> تطبيقاً نُشر الآن، ولا أن النموذج يرد. حالة GitHub Deployments التي يكتبها
> `fastapi-cloud[bot]` هي دليل المزامنة من `main` — لا تختلط مع `CI PASS`.
>
> ```text
> GitHub App مربوط على المستودع
>   → كل دفع إلى main ينشر celanimna-3ffa6b22 و celanimna
>   → ضبط الأسرار في اللوحة (NIMNA_API_KEY, GEMINI_API_KEY)
>   → GET /api/health على الـURL العام
> CI PASS ≠ Merge · Merge ≠ Deployment · Deployment ≠ Live verification
> Missing secrets ≠ successful sync
> ```

| الطرف | القيمة |
|---|---|
| GitHub | https://github.com/sayedelazameydesign-crypto/cela.nimna |
| الفرع الافتراضي | `main` |
| لوحة الفريق | https://dashboard.fastapicloud.com/sayedelazameydesign-424e4d8c/apps |
| الفريق | `sayedelazameydesign-424e4d8c` |
| المزامنة الحية | FastAPI Cloud GitHub App — `fastapi-cloud[bot]` |
| التطبيق الأساسي | `celanimna-3ffa6b22` → https://celanimna-3ffa6b22.fastapicloud.dev |
| التطبيق الثاني | `celanimna` — `spare_role: legacy-duplicate`، **ليست بيئة staging** |
| المدخل | `nimna.api.app:app` (`[tool.fastapi]` في `pyproject.toml`) |
| الصحة | `GET /api/health` |
| Python | `3.11` (`.python-version`) |

العقد الآلي في المستودع: `fastapi-cloud.yaml` (`sync_mode: github_app`).
`fastapi-cloud.yaml` **ليس** فورماتاً تقرأه منصة FastAPI Cloud؛ هو عقد CI فقط.

---

## 1) ما هو مربوط الآن

المستودع مربوط بتطبيقَي FastAPI Cloud عبر **Source Repository** (GitHub App).
كل دفع إلى `main` يُنشئ GitHub Deployment من `fastapi-cloud[bot]`:

- `Production – celanimna-3ffa6b22`
- `Production – celanimna`

هذا هو مسار المزامنة الحي. البوابة تمسح **كل** `.github/workflows/*.{yml,yaml}`،
تتبع `jobs.*.uses` و`steps[].uses` إلى reusable workflows وcomposite actions
داخل المستودع، وتفشل (`exit 1`) فقط إذا مسار نشر FastAPI Cloud يمكن أن ينطلق
على فرع `main` عبر `push` أو `pull_request`. `branches: [staging]` أو
`branches-ignore: [main]` ليسا تعارضاً. `ci.yml` يبقى على `on.push` لأنه لا ينشر.

### لماذا تطبيقان على نفس المستودع؟

`celanimna-3ffa6b22` هو الإنتاج الأساسي. `celanimna` **ليست بيئة staging**:
تكامل GitHub في FastAPI Cloud ينشر **الفرع الافتراضي فقط** (`main`)، فلا فرع
معاينة ولا هدف نشر مختلف. التطبيق الثاني مربوط بنفس المستودع ونفس الفرع
(`spare_role: legacy-duplicate`)؛ كل دفع يولّد Deploymentين من `fastapi-cloud[bot]`.

إن لم يكن له غرض مستقل: التطبيق → **Settings** → **Source Repository** →
**Disconnect**. النشرة الحالية تبقى حتى تُحذف يدوياً. لا يمكن فصل الربط من
داخل المستودع.

## ⚠️ إجراء معلّق (يدوي — لا يُغلق من CI)

الفصل إجراء لوحة فقط. بلا مالك وتاريخ تُنسى الملاحظة كما نُسي الربط المزدوج
قبل تثبيته في العقد.

- [ ] فصل `celanimna` من Source Repository عبر اللوحة (Settings → Source Repository → Disconnect)
- المسؤول: `sayedelazameydesign-crypto`
- منذ: `2026-09-25`
- السبب: تطبيق مكرر ينشر تلقائياً من `main` بلا فائدة مستقلة (ليست staging) — دورة نشر مضاعفة
- عند الإتمام: غيّر `spare_disconnect_todo` في `fastapi-cloud.yaml` من `open` إلى `done` وعلّم الصندوق `[x]`

البوابة تفحص أن الصندوق `- [ ]` موجود طالما `spare_disconnect_todo: open`. لا تفشل
لأن الفصل لم يتم — تفشل إذا اختفى التتبع.

المراجع الرسمية:

- [GitHub Integration](https://fastapicloud.com/docs/source-control/github-integration/)
- [Deploy Tokens](https://fastapicloud.com/docs/advanced-features/deploy-tokens/)
- [`fastapi cloud setup-ci`](https://fastapicloud.com/docs/fastapi-cloud-cli/setup-ci/)

---

## 2) المسار A — GitHub App (الوضع الحي)

تطبيق جديد من المستودع:

1. افتح [لوحة التطبيقات](https://dashboard.fastapicloud.com/sayedelazameydesign-424e4d8c/apps)
2. Create a new app from GitHub
3. اربط حساب GitHub وثبّت FastAPI Cloud GitHub App إن طُلب
4. اختر `sayedelazameydesign-crypto` / `cela.nimna`
5. Root Directory = جذر المستودع
6. Create App — يُنشر أحدث التزام على `main`

تطبيق موجود:

1. التطبيق → **Settings** → **Source Repository** → **Connect**
2. نفس اختيار الحساب/المستودع
3. Connect

لفصل الربط: **Source Repository** → Disconnect. النشرات الحالية تبقى تعمل.
فقط دفعات `main` تُزامَن. الفروع الأخرى وطلبات السحب **لا** تُنشئ معاينات.

---

## 3) المسار B — GitHub Actions يدوي (احتياط، ليس المزامنة الحية)

`.github/workflows/fastapi-cloud-deploy.yml` لا يعمل على `push`. تشغيله:

1. أنشئ Deploy Token من التطبيق → **Deploy Tokens**
2. انسخ App ID (UUID بجانب الاسم، ليس slug الـURL)
3. أسرار المستودع: `FASTAPI_CLOUD_TOKEN` و `FASTAPI_CLOUD_APP_ID`
4. Actions → **FastAPI Cloud — manual redeploy** → Run workflow على `main`

بدون السرّين الـjob **يفشل** (`::error::`) — لا نشر صامت.

بديل محلي بعد `fastapi login`:

```bash
fastapi cloud setup-ci --secrets-only
```

---

## 4) متغيرات البيئة في لوحة FastAPI Cloud

العقد: `fastapi-cloud.yaml` → `env`. **لا قيم سرّية في المستودع.**

اضبطها في **كل** تطبيق مربوط (`celanimna-3ffa6b22` و`celanimna` إن بقي):
**Environment Variables**، وللأسرار فعّل **Secret**.

بدون هذه الأسرار **لا إقلاع** — قبل أي طلب HTTP وقبل أي اتصال بـ Gemini.
الترتيب في `nimna.api.app:create_app` (و`__getattr__("app")` عند استيراد
`uvicorn nimna.api.app:app`):

```text
1. Settings.from_env()
2. SecurityConfig.from_settings()  → SecurityConfigError إن نقص NIMNA_API_KEY
3. build_agent() → create_provider() → GeminiProvider.__init__
                                   → ProviderError إن نقص GEMINI_API_KEY
4. FastAPI(...)  + مسارات /api/health  ← لا تُبنى إن فشل 2 أو 3
```

لا سقوط إلى `mock`. `/api/health` لا يُخدم أصلاً إذا فشل الإقلاع.

```text
nimna.api.security.SecurityConfigError: NIMNA_API_KEY is required when NIMNA_ENV=production
nimna.providers.base.ProviderError: Gemini API key is not set
```

| المتغير | القيمة | سرّ؟ |
|---|---|---|
| `NIMNA_ENV` | `production` | لا |
| `NIMNA_API_KEY` | `secrets.token_urlsafe(32)` | نعم — إلزامي |
| `MODEL_PROVIDER` | `gemini` | لا |
| `GEMINI_MODEL` | `gemini-2.5-flash` | لا |
| `GEMINI_API_KEY` | مفتاح AI Studio | نعم — إلزامي للإقلاع |
| `MAX_SPEND_USD` | `0` | لا |
| `COST_GUARD_ENABLED` / `COST_GUARD_HARD` | `true` | لا |
| `AGENT_AUTO_APPROVE` | `false` | لا |
| `SANDBOX_BACKEND` | `subprocess` | لا |
| `BROWSER_USE_ENABLED` / `COMPUTER_ENABLED` | `false` | لا |
| `SKILLS_DIR` | `skills` | لا — ليست `/app/skills` |
| `WORKSPACE_DIR` | `workspace` | لا |
| `DB_PATH` | `data/nimna.db` | لا — ephemeral |

`HOST`/`PORT` لا تُثبَّت: المنصة تدير الاستماع. مسارات Docker (`/app/...`)
مرفوضة في بوابة العقد.

من CLI:

```bash
fastapi cloud env set NIMNA_ENV "production"
fastapi cloud env set --secret NIMNA_API_KEY "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
fastapi cloud env set --secret GEMINI_API_KEY "…"
```

بعد الدمج: `scripts/preflight-prod-check.sh https://celanimna-3ffa6b22.fastapicloud.dev`

---

## 5) ما الذي يُرفع؟

`.fastapicloudignore` يستبعد `tests/` و`.github/` و`docs/` و`k8s/` وملفات النشر الأخرى.
**ممنوع** استبعاد `skills/` أو `nimna/` أو `*.md` (هذا الأخير يحذف `SKILL.md`).

`.gitignore` محترم: `.env` و`data/` لا يُرفعان. `.fastapicloud/` محلي بعد
`fastapi deploy` ومُتجاهَل — المعرّف في CI يأتي من `FASTAPI_CLOUD_APP_ID`.

---

## 6) البوابة (offline)

```bash
python scripts/check_fastapi_cloud_link.py
```

تفشل إذا: المدخل تغيّر، مسار الصحة وهمي، سرّ كُتب في العقد، متغير لا يقرؤه الكود،
`NIMNA_ENV` ليس `production`، `MAX_SPEND_USD` ليس `0`، الـworkflow صار `on.push`،
أو `.fastapicloudignore` يحجب `skills/`. تشغّلها `00-integrity.yml`.

النشر السحابي **لا** يثبت أن النموذج يرد. ذلك شغل `live-provider-proof.yml`.

---

## 7) أعطال شائعة

| العرض | السبب المحتمل |
|---|---|
| التطبيق لا يظهر في منتقي GitHub | GitHub App غير مثبّت على `sayedelazameydesign-crypto` أو المستودع غير مشمول |
| دفع لم يُنشر | ليس على `main`، أو التطبيق فُصل، أو Application Directory خاطئ |
| البناء ينجح والإقلاع يسقط | `NIMNA_API_KEY` أو `GEMINI_API_KEY` غير مضبوطين كـ Secret في اللوحة |
| نشران/ثلاثة لكل دفع | GitHub App **و** workflow `on.push` — أزل الـpush من Actions |
| حالة GitHub قديمة | النشر موجود في اللوحة لكن GitHub App فقد الوصول |
| تطبيقان يتحدّثان معاً | `celanimna` = legacy-duplicate على نفس `main`، ليست بيئة staging — افصل Source Repository إن لم تُرَد النسخة |

---

## English — the live link

1. FastAPI Cloud GitHub App is already connected. Pushes to `main` deploy **both** `celanimna-3ffa6b22` and `celanimna` via `fastapi-cloud[bot]`.
2. Dashboard: https://dashboard.fastapicloud.com/sayedelazameydesign-424e4d8c/apps
3. Primary URL: https://celanimna-3ffa6b22.fastapicloud.dev — prove it with `GET /api/health`.
4. Set `NIMNA_API_KEY` and `GEMINI_API_KEY` as secrets on **each** app. Boot is fail-closed.
5. Do **not** add `on.push: main` to `.github/workflows/fastapi-cloud-deploy.yml`. That workflow is a manual token redeploy only.
6. Repo contract: `fastapi-cloud.yaml`. Gate: `python scripts/check_fastapi_cloud_link.py`.
