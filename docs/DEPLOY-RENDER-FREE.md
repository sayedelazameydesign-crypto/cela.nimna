# النشر المجاني على Render Free — `render.yaml`

> **سياق هذا الملف (التوحيد).** كان `render.yaml` + هذا الدليل موجودين **محلياً فقط** في
> `57dc133`، بينما كان على GitHub `a3f9d30` يحمل الـworkflow الحي
> `.github/workflows/live-provider-proof.yml` مع قناة الـannotation (`::notice::`).
> التوحيد حمل الاثنين معاً: عقد النشر المجاني **و** إثبات أن المزود يرد فعلاً.
> `live-provider-proof.yml` لم يُمسّ — أمر التحقق في §7.

> **ما يثبته هذا الملف — وما لا يثبته.** هذا **عقد نشر** + دليل قابل لإعادة الإنتاج.
> ليس دليلاً على أن شيئاً نُشر: لا `render.yaml` ولا هذا الدليل يثبتان deployment على
> Render ولا real inference. **حالة العقد الآن: مُدمَج** — PR #16 دُمِج في
> `2026-09-24T09:26:35Z` وصار `main = 0310c63`؛ هذا يعتمد *عقد التكوين*، ولا يثبت أن
> خدمةً نُشرت. أدوات التحقق من حالة الدمج والنشر في §7 — **استعملها بدل الافتراض**.
> ترتيب البوابة الصحيح، ولا خطوة تُختصر:
>
> ```text
> دمج → إثبات حي (live-provider-proof) → deploy على Render → smoke test على الـpublic URL
> CI PASS ≠ Merge · Merge ≠ Deployment · Deployment ≠ Live verification
> ```

---

## 1) ما الذي يقرره `render.yaml`

Render يقرأ الملف تلقائياً (Blueprint / infrastructure-as-code) وينشئ منه خدمة واحدة.
الملف لا يشغّل شيئاً بنفسه؛ هو **عقد تكوين** يثبّت ما يجب أن يكون ثابتاً:

| الحقل | القيمة | لماذا (قابل للتحقق من الكود) |
|---|---|---|
| `runtime: docker` | يبني من `./Dockerfile` | `Dockerfile`: `python:3.11-slim` + `CMD ["nimna","serve"]` |
| `plan: free` | 0.1 CPU / 512 MB | جدول خطط Web Service في [blueprint-spec](https://render.com/docs/blueprint-spec) |
| `healthCheckPath: /api/health` | فحص HTTP | نفس المسار في `nimna/api/app.py` (السطر `@app.get("/api/health")`) |
| `PORT: "10000"` | المنفذ الذي يوجّه إليه Render | `10000` هو منفذ Render الافتراضي؛ `nimna serve` يقرأ `PORT` عبر `Settings.from_env` |
| `COST_GUARD_ENABLED/HARD: "true"`, `MAX_SPEND_USD: "0"` | بوابة تكلفة صلبة | `nimna/models/registry.py::CostGuard.authorize` — انظر §5 |
| `SANDBOX_BACKEND: "subprocess"` | `run_python` بلا docker.sock | `nimna/config.py`: `sandbox_backend` (docker اختيارياً) |
| `AGENT_AUTO_APPROVE: "false"`, `BROWSER_USE_ENABLED: "false"`, `COMPUTER_ENABLED: "false"` | لا موافقات صورية، لا سطح تحكم | `nimna/tools/builtin/computer.py`, `docs/browser_use_v4.md` |
| `NIMNA_ENV: "production"` + `NIMNA_API_KEY` (`sync: false`) | مصادقة API إلزامية؛ بلا مفتاح لا إقلاع | `nimna/api/security.py::SecurityConfig.from_settings` — انظر `docs/API-SECURITY.md` |
| الأسرار بـ `sync: false` | تُضبط من اللوحة فقط | `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_API_KEY`, `NVIDIA_API_KEY`, `BROWSER_USE_API_KEY`, `NIMNA_API_KEY`, `REDIS_URL` |

**لا شيء في `render.yaml` يحمل مفتاحاً.** `scripts/evaluate_arena.py` في كل PR يفتش الـdiff
عن أسرار، و`00-integrity.yml` يفشل إن تُعقِّب `.env`.

### فخاخ YAML مُغلقة ببوابة

- `autoDeployTrigger: "off"` — بلا اقتباس يقرأها YAML 1.1 كـ **boolean** (`off` → `false`)
  فيرفضها Render أو يتجاهلها.
- كل `value` نصٌّ مُقتبَس: `value: 0` عدد، و`value: true` منطقية — وRender يتوقع نصاً.
- `autoDeploy: false` **مهجور**؛ المفتاح الحالي هو `autoDeployTrigger` (docs blueprint-spec).
- المفاتيح التي لا يقرؤها الكود (خطأ إملائي مثل `MODEL_PROVIDERR`) تُترك بصمت — لذلك
  البوابة تقارن كل اسم متغير بما يقرؤه `nimna/` و`security/` فعلاً.

البوابة نفسها: `python scripts/check_render_blueprint.py` (offline، بلا حساب Render،
بلا بناء صورة) + `tests/test_render_blueprint.py` الذي يثبت أنها **يمكن أن تفشل**
(سلسلة طفرات: قيمة غير مقتبسة، `off` بلا اقتباس، مفتاح مهجور، سرّ مضمّن في النص، اسم
متغير لا يقرؤه الكود، مسار health وهمي، قرص دائم على Free، قيم enum غير معروفة …).

---

## 2) اقرأ هذا أولاً: بلا `GEMINI_API_KEY` لا إقلاع

هذا سلوك مقصود في الكود، وليس خطأً في الدليل. بدون مفتاح، `create_app` يفشل أثناء
التكوين (قبل أول طلب)، والقياس في هذا الفرع:

```text
nimna.providers.base.ProviderError: Gemini API key is not set – set GEMINI_API_KEY
(preferred) or GOOGLE_API_KEY
```

على Render يعني ذلك: الحاوية تسقط، و`/api/health` لا ينجح أبداً، فيُلغى الـdeploy بعد
15 دقيقة (وثيقة [health-checks](https://render.com/docs/health-checks): "If this condition
is not met within 15 minutes, Render cancels the deploy"). **اضبط المفتاح في اللوحة ثم
اضغط Deploy**، لا العكس. المشروع لا يسقط إلى `MockProvider` بصمت: إقلاع بمزوّد وهمي
أخطر من فشل صريح.

**وبالمثل بلا `NIMNA_API_KEY` لا إقلاع** (`NIMNA_ENV=production` مثبت في الـBlueprint):

```text
nimna.api.security.SecurityConfigError: NIMNA_API_KEY is required when NIMNA_ENV=production (the default). ...
```

خادم بلا مصادقة على الإنترنت أخطر من خادم لا يقلع. لا تستخدم `generateValue` لهذا المفتاح:
Render يولّد base64 (يحتوي `/` و`=`) وهي محارف غير صالحة كـWebSocket sub-protocol الذي
تستخدمه الواجهة. ولّده محلياً: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

---

## 3) خطوات النشر

1. **مفتاح Gemini مجاني**: <https://aistudio.google.com/apikey>. حسب ما يوثّقه
   `live-provider-proof.yml` في هذا المستودع: مفاتيح AI Studio الحديثة من نوع
   Authorization (`AQ.*`) مقيّدة بـGenerative Language API، ولا تعمل على مسارات متوافقة
   مع OpenAI — وهذا سبب تثبيت `MODEL_PROVIDER=gemini` هنا وفي الـworkflow.
2. **Dashboard → New + → Blueprint** واختر المستودع `sayedelazameydesign-crypto/cela.nimna`.
   سيكتشف Render `render.yaml` ويعرض خدمة `cela-nimna` بخطة Free.
3. **Environment**: أضف `NIMNA_API_KEY` (إلزامي، ≥ 32 حرفاً — انظر §2) و`GEMINI_API_KEY`
   (و`GOOGLE_API_KEY` اختياري كـfallback). البقية
   مثبتة في الـBlueprint؛ إن أردت مسار NVIDIA/OpenAI المتوافق فأضف `OPENAI_API_KEY` +
   `OPENAI_BASE_URL` من اللوحة ولا تكتبهما في الملف.
4. **Manual Deploy** (لأن `autoDeployTrigger: "off"`). في Logs ابحث عن سطر uvicorn على
   `0.0.0.0:10000`.
5. **اختبار**:

```bash
BASE=https://<your-service>.onrender.com
curl -s "$BASE/api/health" | jq '{status,provider,model,skills,tools,port}'   # عام
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/api/tools"                   # 401 بلا مفتاح
curl -s "$BASE/api/provenance" -H "X-Nimna-Key: $NIMNA_API_KEY" | jq '.runtime_fingerprint'
curl -s "$BASE/api/chat" -H "X-Nimna-Key: $NIMNA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"حلّل workspace/sales.csv باختصار"}' | jq '{status,reply}'
```

المتوقّع من `/api/health` (مقيس في هذا الفرع على بيئة dependencies مطابقة للصورة):

```json
{ "status": "ok", "provider": "gemini", "model": "gemini-2.5-flash",
  "skills": 10, "tools": 28, "port": 10000 }
```

الحقول الفعلية أوسع (`verify_detail`, `limits`, `governance.cost`, `memory`, `infra`,
`provenance`, `browser_use`) — هذه لقطة مختصرة. **الأرقام مقيسة لا منقولة**: 10 مهارات
و28 أداة على `main` (تطابق `README.md` منذ 2026-09-25) — وحين يختلف أي قياس مستقبلاً
مع README فالقياس هو المرجع (وثّق ذلك في الـPR إن لمسته).

> `POST /api/chat` قد يرجع `status: "awaiting_approval"` بدل الرد: أدوات مثل `run_python`
> تطلب موافقة، وتُحلّ عبر `POST /api/approvals/{id}`. كرار الطلب نفسه على جلسة معلقة
> يرجع `409` — وهذا مقصود (`nimna/api/app.py`).

---

## 4) قيود Free Tier — وكيف يمتصّها هذا المشروع

منوثَقة من [render.com/docs/free](https://render.com/docs/free) (تاريخ الاطلاع 2026-09-24):

| القيد (Render) | التأثير على Nimna | ما يفعله المشروع |
|---|---|---|
| نوم بعد **15 دقيقة** بلا traffic؛ الإيقاظ **نحو دقيقة** | أول طلب بطيء، والـWebSocket ينقطع | لا تعتمد على اتصال دائم؛ الواجهة تعيد الاتصال. للإنتاج: Starter |
| نظام ملفات **ephemeral** — يُمسح عند redeploy/restart/spin-down | `data/nimna.db` و`workspace/reports` تضيع | حمّل التقرير فور `/api/runs/{run_id}/evidence`؛ لا persistence مزيّفة |
| **750 ساعة Free/شهر** لكل workspace، ثم تعليق | تعليق الخدمة منتصف الشهر | الخدمة نائمة معظم الوقت عملياً؛ راجع Billing |
| **0.1 CPU / 512 MB** | `run_python` يُقتل أو يبطئ | `SANDBOX_MEMORY_MB=512`, `SANDBOX_TIMEOUT=20` |
| **لا persistent disk، لا SSH، لا one-off jobs** على Free | لا «ادخل الحاوية وشغّل `nimna doctor`» | شخّص عبر `/api/health` و`/api/capabilities` و`/api/provenance`، أو `nimna doctor --offline` محلياً على نفس الـcommit |
| **لا Redis وظيفي مُدار** | ذاكرة Vision بلا cache مشترك | الـfallback مقيس: `{"enabled": false, "redis_url": "fallback:memory"}`. Render يوفر Key Value مجاني (25 MB، in-memory، نسخة واحدة/workspace) — اختياري: اضبط `REDIS_URL` من اللوحة |
| **لا Qdrant** | لا بحث متجهي حقيقي | `EMBEDDING_PROVIDER: "hash"` → hash embeddings‏ 768-dim، deterministic، بلا استدعاء API خارجي، والمخزون في-process يضيع مع إعادة التشغيل |
| منع منافذ `18012/18013/19099`، ومنع outbound على `25/465/587` | لا تعارض معنا | نستخدم `10000` فقط |
| تعليق عند **حجم traffic صادر استثنائي** من الخدمة | agent يستدعي مزوّد النموذج | Free للـdemo؛ للـusage المستمر ارفع الخطة |
| `robots.txt` يُجاب آلياً بـdisallow أثناء النوم | محركات بحث لا ترى الخدمة نائمة | غير مهم للـdemo |

الصورة لا تثبّت `redis` ولا `qdrant-client` ولا `numpy` (تلك في `requirements.txt`
وليس في `pyproject`): لذلك تظهر مساراتها **معطلة بوضوح** في `/api/health` بدل أن تنفجر —
وهذا مقصود: الصورة أصغر، والقدرة غير المتوفرة مُعلنة لا مُموّهة.

---

## 5) CostGuard على Free: ماذا يحدث فعلاً عند سقف صفر

`MAX_SPEND_USD=0` ليس «تحذيراً»؛ إنه `hard` gate قبل استدعاء النموذج. القياس في هذا
الفرع بمزوّد متوافق مع OpenAI وبلا تسعير معلن:

```text
status: error
error: cost guard blocked the run: cost guard blocked model 'meta/llama-3.3-70b-instruct':
       pricing is unknown and MAX_SPEND_USD=0
governance.cost.blocked_count: 1
```

- لا يوجد `status: "BLOCKED"` في الـAPI — الحالات هي `running / awaiting_approval / done /
  error` (`nimna/core/state.py::RunStatus`)؛ الرفض يظهر كـ`error` + `blocked_count`.
- منذ إضافة `CostPolicyError`: الحالة أعلاه (تسعير مجهول + `MAX_SPEND_USD=0` + `hard`)
  لم تعد تصل إلى وقت الطلب أصلاً — الإقلاع يُرفض. وفي Gemini «المجاني» إعلان لا اكتشاف:
  فقط النماذج في `GEMINI_FREE_TIER_MODELS` (افتراضياً `gemini-2.5-flash`) تُعامَل كصفر
  تكلفة؛ تغيير `GEMINI_MODEL` بلا توسيع القائمة يرفض الإقلاع أيضاً.
- Gemini يبقى يعمل لأن له ملف تسعير مجاني معلن (`zero_cost_profile: true`)، بينما
  أي مزوّد مدفوع بلا تسعير صريح يُرفض. للاستخدام المدفوع اضبط `MODEL_COST_INPUT_USD_PER_1K`,
  `MODEL_COST_OUTPUT_USD_PER_1K` وارفع `MAX_SPEND_USD` — ولا تفعل ذلك على Free-tier
  billing دون بوابة صريحة.

---

## 6) لماذا النشر المجاني لا يغني عن الإثبات الحي

`render.yaml` يثبت أن الخدمة **تُبنى وتُخدم وتُبلِغ عن نفسها**. لا يثبت أن النموذج يرد.
ذلك شغل `.github/workflows/live-provider-proof.yml`:

- يعمل بـ`workflow_dispatch` فقط (يدوي) لأنه يستهلك حصة حقيقية، ويرفض إن لم يوجد
  `GEMINI_API_KEY` كـsecret (لا skip صامت).
- يشغّل `scripts/live_provider_proof.py --evidence-out live-proof-evidence.json` ويرفض
  `MockProvider` («mock-refusing»).
- ينشر الخلاصة في `$GITHUB_STEP_SUMMARY` **و** كـ`::notice::` annotation — وهذه هي
  **قناة الـannotation**: الـannotations تعيش على `api.github.com` وتبقى مقروءة حتى حين
  لا يمكن الوصول إلى سجل الـjob الخام.
- يرفع `live-proof-evidence.json` كـartifact.

تسلسل مقترح بعد النشر: (1) deploy على Render، (2) شغّل Live provider proof من Actions،
(3) قرّر من الـannotation، (4) لا تُصدِق «PASS» من Render وحده.

```bash
REPO=sayedelazameydesign-crypto/cela.nimna
RUN=$(gh run list -R "$REPO" -w "Live provider proof (real inference)" \
        --json databaseId -q '.[0].databaseId')
# الـannotation معلقة بـcheck-run الخاص بـjob، وjob.databaseId هو الـcheck-run id
JOB=$(gh run view "$RUN" -R "$REPO" --json jobs -q '.jobs[0].databaseId')
gh api "repos/$REPO/check-runs/$JOB/annotations" -q '.[] | .message'
```

النص المنشور هو `::notice title=Nimna live proof (real inference)::PROOF {…}`
(`scripts/live_provider_proof.py:74`)، والـverdict أحد: `PASS` · `FAIL` · `REFUSED`
(الأخير عند `MODEL_PROVIDER=mock` — الـworkflow يرفض أن يُؤخذ mock كدليل).

---

## 7) ما لم يتغيّر في التوحيد + حالة الدمج والنشر (قابلة للفحص)

```bash
BASE=a3f9d30   # رأس GitHub قبل التوحيد
git diff --stat "$BASE" HEAD -- .github/workflows/live-provider-proof.yml
# متوقع: لا شيء — الـworkflow الحي وقناة الـannotation كما هما بايت ببايت
git diff --name-only "$BASE" HEAD
# متوقع: render.yaml، docs/DEPLOY-RENDER-FREE.md، بوابة الـblueprint (scripts + tests + 00-integrity.yml)

# هل صار العقد جزءاً من main فعلاً؟ (لا تُصدِق نصاً في docs — اسأل origin)
git ls-remote origin main
#   a3f9d30…        ← قبل الدمج: العقد على فرع PR فقط
#   0310c63ebeb2…   ← بعد دمج PR #16: العقد في main، ومن هنا فقط يصير النشر منها ممكناً
gh pr view <PR> --json state,mergedAt,mergeCommit -q '{state:.state,mergedAt:.mergedAt,mergeCommit:.mergeCommit.oid}'
# هل نُشر فعلاً؟ لا دليل في المستودع قبل وجود URL + مخرجات health من الخدمة الحيّة
curl -sS https://<service>.onrender.com/api/health | jq '{status,provider,model,skills,tools,port}'
```

**قاعدة الحوكمة التي يحميها هذا القسم:** نجاح `production-gate` أو وجود
`render.yaml` أو خُضرة CI **ليست** دليلاً على deployment؛ إنها دليل على أن العقد
صحيح ومقروء. الدليل على النشر شيء آخر: URL حيّ + `/api/health` منه + annotation
من `live-provider-proof.yml` (وليس من الـblueprint).

### سجلّ الحالة (مقاس بالأوامر، لا منسوخ من PR)

| اللحظة | الدليل |
|---|---|
| قبل التوحيد | `main = a3f9d30` — لا `render.yaml` في المستودع |
| التوحيد | `e80bb39` فوق `a3f9d30` (عقد + docs + بوابة + اختبارات) ثم `5d758ce` (توثيق الحالة) |
| الدمج | PR #16 `MERGED` في `2026-09-24T09:26:35Z` → `main = 0310c63` (`7 files, +695 / −0`) |
| CI على `main` | `integrity` success وخطوته 7 `Verify Render blueprint` = **success** (run `35981257006`) |
| الـworkflow الحي | `git diff a3f9d30 0310c63 -- .github/workflows/live-provider-proof.yml scripts/live_provider_proof.py` = **0 سطر** |
| آخر إثبات حيّ | run `35977697240` (على `a3f9d30`، أي بنفس سكربت الإثبات الحالي بايت ببايت) — الـannotation: `verdict: PASS` · `provider: gemini` · `model: gemini-2.5-flash` · `direct_call.latency_ms: 597` |
| النشر على Render | ⚪ **لم يُنفَّذ**: لا URL حيّ، لا `/api/health` عام، لا smoke test |

**ملاحظة تشغيلية:** الإثبات الحي يدوي بطبيعته (`workflow_dispatch`)، وحساب الـagent لا يملك
`actions:write` هنا (`403` على `POST /repos/…/actions/workflows/365881318/dispatches`) —
فيُشغَّل من الواجهة: Actions → *Live provider proof (real inference)* → Run workflow على
`main`، ويُقرأ الـverdict من الـannotation. **لا run حيّ بعد الدمج مُسجَّل**: لا تُقرأ خُضرة
`production-gate` على `main` كبديل عنه.

وأمان النشر نفسه: لا `AGENT_AUTO_APPROVE=true`، لا `COMPUTER_ENABLED=true` بلا desktop
معزول، الحمولات تمرّ بـ`redact_payload` (`***REDACTED***`) في `audit_log`.

---

## 8) استكشاف الأخطاء

| العَرَض | السبب المرجّح | الفحص |
|---|---|---|
| Deploy فاشل/Rebooting بلا سبب ظاهر | لا `GEMINI_API_KEY` → `ProviderError` عند الإقلاع (§2) | Logs؛ ثم `curl $BASE/api/health` |
| Deploy فاشل + `SecurityConfigError` في Logs | `NIMNA_API_KEY` غير مضبوط/قصير/ضعيف (§2) | اضبطه من اللوحة (≥ 32 حرفاً، `token_urlsafe`) |
| `401` على `/api/*` | ترويسة `X-Nimna-Key` مفقودة/خاطئة | `curl -H "X-Nimna-Key: …" $BASE/api/tools` |
| `429` على `/api/chat` | تجاوز 30 طلب/دقيقة لكل مفتاح | انتظر `Retry-After` ثانية |
| `502` على أول طلب | الخدمة نائمة (~دقيقة إيقاظ) | أعد الطلب بعد 60s |
| Deploy يُلغى بعد 15 دقيقة | `healthCheckPath` لا يرجع 2xx/3xx خلال 5s | `curl -i $BASE/api/health` — المسار يجب أن يكون `/api/health` |
| `status: error` + `cost guard blocked…` | سقف صفر مع مزوّد غير معلن التسعير (§5) | `jq .governance.cost /api/health` |
| `provider: "mock"` في health | `MODEL_PROVIDER=mock` في اللوحة | اضبطه على `gemini` — الاختبارات وحدها تستخدم mock |
| `409` على `/api/chat` | موافقة معلقة على نفس الجلسة | `GET /api/approvals?session_id=…` ثم `POST /api/approvals/{id}` |
| Redis/Qdrant معطّلان | متوقع على Free (§4) | `jq '.infra, .memory' /api/health` |
| Blueprint رفضه Render | قيمة غير مقتبسة أو مفتاح مهجور | `python scripts/check_render_blueprint.py` |

---

## 9) ما تاليًا

- **Production**: `docker-compose.yml`/`k8s/` + `infra/` (Qdrant, Redis, seccomp/AppArmor)
  بدل Free — انظر `SECURITY.md` و`docs/PRODUCTION-HARDENING-2026-09-24.md`.
- **Benchmark**: `scripts/evaluate_arena.py` (تعليق على كل PR عبر `arena_diff_eval.yml`)
  و`scripts/run_arena_suite.py`.
- **BEP/Audit**: `/api/provenance` (runtime fingerprint) و`/api/runs/{run_id}/evidence`
  (hash chain) — اقرأهما بعد كل deploy كدليل، لا كزينة.

---

### سجلّ القياسات (ما ثبت وما لم يثبت في هذا الفرع)

| الادعاء | كيف فُحص | النتيجة |
|---|---|---|
| 10 مهارات / 28 أداة | `build_agent(Settings.from_env())` ثم `len(...)` على `MODEL_PROVIDER=mock` | `skills: 10`, `tools: 28` |
| `/api/health` سريع بما يكفي لمهلة 5s | `TestClient` على بيئة deps مطابقة للصورة | `HTTP 200` في ~4 ms |
| `PORT=10000` يُحترم | `port` في health مع `PORT=10000` | `10000` |
| بلا مفتاح ← `ProviderError` | `env -u GEMINI_API_KEY MODEL_PROVIDER=gemini` + `create_app` | استثناء عند التكوين (نصه في §2) |
| CostGuard يرفض بلا تسعير | run بمزوّد openai-compatible وسقف 0 | `status: error`, `blocked_count: 1` |
| fallback الذاكرة بلا Qdrant | health على deps الصورة نفسها | `provider: "fallback"`, `vector_dim: 768` |
| لا Redis في الصورة | health | `cache: {enabled: false, redis_url: "fallback:memory"}` |
| كل الاختبارات | `python -m pytest -o addopts="" -q` | `331 passed` = 318 (الحالية) + 13 (بوابة الـblueprint) |
| **بناء صورة Docker الفعلية** | — | **لم يُنفَّذ هنا** (لا `docker` في هذه البيئة). النشر على Render هو الاختبار الحقيقي لذلك البند؛ لا تدّعه PASS |
