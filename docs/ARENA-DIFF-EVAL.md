# Arena Diff Evaluation — تقييم الفروقات تلقائياً عبر GitHub Actions

> الهدف: عند كل Pull Request يُستخرج الـ **Git Diff** (التغييرات فقط)، يُقيَّم
> بسكريبت `scripts/evaluate_arena.py` (فحص ثابت + إرسال اختياري إلى بيئة
> Arena)، وتُنشر النتيجة كتعليق واحد داخل الـ PR.
>
> **قاعدة المستودع تنطبق هنا أيضاً:** لا يُرفع `UNKNOWN` إلى `PASS` تلقائياً.
> بدون Arena API مضبوط تظهر حالة الـ Benchmark كـ `SKIPPED` وليس نجاحاً وهمياً
> (راجع [`VERIFICATION-MATRIX.md`](VERIFICATION-MATRIX.md)).

---

## 1. كيف يعمل النظام (Workflow)؟

1. يرفع المطور تعديلاً جديداً (`Pull Request` إلى `main`/`master`، أو تشغيل يدوي `workflow_dispatch`).
2. يقوم GitHub Action بجلب التاريخ كاملاً (`fetch-depth: 0`) ويستخرج الـ Diff:
   `git diff origin/<base>...HEAD > changes.diff`.
3. يُمرَّر الـ Diff إلى سكريبت التقييم `scripts/evaluate_arena.py` الذي:
   - يحسب المقاييس الحقيقية (ملفات، أسطر مضافة/محذوفة، إضافة/حذف/إعادة تسمية/ثنائي)
     مع استبعاد أسطر الترويسة `+++`/`---`.
   - يوزّع التغيير حسب الفئة: `code / tests / skills / docs / workflows / infra / config`.
   - يرفع **إشارات مخاطر** خاصة بهذا المستودع (انظر §5).
   - يرسل الـ Diff + المقاييس إلى Arena **إذا** ضُبط `ARENA_API_URL` و`ARENA_API_KEY`.
4. تُحفظ `changes.diff` و`result.md` و`result.json` كـ Artifacts (14 يوماً).
5. تُنشر النتيجة كتعليق في الـ PR — ويُحدَّث نفس التعليق عند كل push بدلاً من تكرار التعليقات.

```
PR / push → checkout (full history) → git diff base...HEAD
   → evaluate_arena.py ─┬─ static metrics + risk signals
                        └─ (opt-in) POST → ARENA_API_URL
   → result.md / result.json → artifact + PR comment (upsert)
```

---

## 2. الملفات

| الملف | الدور |
|---|---|
| `.github/workflows/arena_diff_eval.yml` | الـ Workflow (PR + تشغيل يدوي)، أقل صلاحيات: `contents: read`, `pull-requests: write` |
| `scripts/evaluate_arena.py` | سكريبت التقييم — **مكتبة قياسية فقط** (لا يحتاج `pip install`) |
| `tests/test_evaluate_arena.py` | 14 اختباراً offline: التحليل، الإشارات، عدم تسريب الأسرار، عقد الـ API، CLI |

---

## 3. الإعدادات المطلوبة في GitHub

### الأسرار (اختيارية — بدونها يعمل الفحص الثابت فقط)

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | الغرض |
|---|---|
| `ARENA_API_URL` | عنوان endpoint التقييم (يستقبل `POST` JSON) |
| `ARENA_API_KEY` | يُرسل كـ `Authorization: Bearer <key>` |
| `OPENAI_API_KEY` | غير مستخدم حالياً — فعّل السطر المعلّق في الـ workflow فقط إذا أضفت LLM-as-a-Judge |

### الصلاحيات

الـ workflow يعلن صلاحياته صراحةً (`permissions:`) فلا حاجة لرفع الإعداد العام إلى
"Read and write permissions". استثناءان:

- **PRs من fork**: يحصل `GITHUB_TOKEN` على قراءة فقط، لذلك خطوة التعليق
  `continue-on-error: true` — يبقى التقرير متاحاً في سجل الخطوة وفي الـ Artifact.
- **سياسة على مستوى المنظمة** قد تمنع الكتابة؛ عندها اسمح بـ `pull-requests: write` من إعدادات المنظمة.

---

## 4. التشغيل محلياً

```bash
git fetch origin main
git diff origin/main...HEAD > changes.diff
python scripts/evaluate_arena.py --diff_file changes.diff            # Markdown إلى stdout
python scripts/evaluate_arena.py --diff_file changes.diff --format json
python scripts/evaluate_arena.py --diff_file - < changes.diff --strict  # exit 2 عند HIGH / FAIL
ARENA_EVAL_MODE=mock python scripts/evaluate_arena.py --diff_file changes.diff  # اختبار خط الأنابيب
pytest -q tests/test_evaluate_arena.py
```

الخيارات: `--output result.md`, `--json-output result.json`, `--base-ref`, `--head-sha`, `--strict`.

المتغيرات البيئية: `ARENA_API_URL`, `ARENA_API_KEY`, `ARENA_EVAL_MODE` (`auto` | `offline` | `mock`),
`ARENA_TIMEOUT` (ثوانٍ، افتراضي 30), `ARENA_MAX_DIFF_BYTES` (افتراضي 200000),
`ARENA_BASE_REF`, `ARENA_HEAD_SHA`, `ARENA_PR_NUMBER`.

---

## 5. مفردات النتيجة

### حالة الـ Arena Benchmark

| الحالة | المعنى |
|---|---|
| `PASS` / `FAIL` | ردّ حقيقي من `ARENA_API_URL` (`status`/`verdict` + `score` اختياري) |
| `SKIPPED` | لا يوجد `ARENA_API_URL`/`ARENA_API_KEY` أو `ARENA_EVAL_MODE=offline` |
| `MOCKED` | `ARENA_EVAL_MODE=mock` — لاختبار خط الأنابيب فقط؛ يبدأ التقرير بلافتة تحذير ⚠️ قبل أي مقياس |
| `ERROR` | الـ API مضبوط لكن الاتصال/الردّ فشل — لافتة تحذير ⚠️ في أعلى التقرير، يُبلَّغ استشارياً ولا يُحوَّل إلى PASS |
| `UNKNOWN` | ردّ JSON بلا حالة مفهومة |

**لماذا لافتة لـ `MOCKED`/`ERROR` وليس لـ `SKIPPED`؟** `SKIPPED` هو السلوك الافتراضي
الموثّق عندما لا يوجد Arena API — حالة متوقعة لا تحمل معلومة مضلِّلة. أما `MOCKED` فيحمل
أرقاماً قد تُقرأ خطأً كنتيجة حقيقية، و`ERROR` يعني أن خدمة كان يُفترض أن تحكم لم تحكم؛
كلاهما انحراف عن المتوقع يستحق تنبيهاً لا يمكن تجاوزه بالقراءة السريعة.

### إشارات المخاطر (Risk signals)

| الكود | المستوى | متى تظهر |
|---|---|---|
| `env-file-tracked` | 🔴 HIGH | ملف `.env` / `.env.local` / `.env.production` داخل التغييرات |
| `secret-like-addition` | 🔴 HIGH | سطر مضاف يشبه مفتاحاً (AWS, Google, GitHub, `sk-…`, Slack, private key, `*_API_KEY=<16+ chars>`) — **القيمة لا تُعرض أبداً**، فقط `الملف:السطر (النمط)` |
| `sensitive-path` | 🟠 MEDIUM | `.github/workflows/`, `nimna/tools/sandbox.py`, `nimna/tools/builtin/`, `nimna/governance/`, `nimna/core/approval.py`, `nimna/api/app.py`, `security/`, `Dockerfile`, `docker-compose.yml`, `k8s/`, `requirements.txt`, `pyproject.toml` |
| `code-without-tests` | 🟠 MEDIUM | تغيير تحت `nimna/` أو `security/` أو `scripts/` بدون أي ملف تحت `tests/` |
| `tests-deleted` | 🟠 MEDIUM | حذف ملفات اختبار |
| `binary-files` | ℹ️ INFO | ملفات ثنائية |
| `large-diff` | ℹ️ INFO | أكثر من 1000 سطر متغير |

المستوى الإجمالي = أعلى إشارة (`HIGH` > `MEDIUM` > `LOW`). القيم المشبوهة التي تحتوي على
`${{ secrets.X }}`, `os.environ`, `your-…`, `example`, `changeme` … لا تُحتسب أسراراً.

---

## 6. عقد الـ Arena API (إذا أردت ربط بيئة تقييم خاصة)

**الطلب** — `POST ${ARENA_API_URL}` مع `Authorization: Bearer ${ARENA_API_KEY}`:

```json
{
  "version": 1,
  "context": {"repository": "owner/repo", "base_ref": "main", "head_sha": "…", "pr_number": "42"},
  "metrics": {"files": 3, "added": 120, "removed": 8, "files_added": 1, "files_deleted": 0, "...": "…"},
  "categories": {"code": {"files": 2, "added": 100, "removed": 8}, "tests": {"…": "…"}},
  "risk": {"level": "MEDIUM", "signals": [{"level": "MEDIUM", "code": "sensitive-path", "message": "…", "files": ["…"]}]},
  "files": [{"path": "nimna/core/agent.py", "status": "modified", "added": 10, "removed": 2, "binary": false, "category": "code"}],
  "diff": "diff --git a/… (مقصوص عند ARENA_MAX_DIFF_BYTES)",
  "diff_truncated": false
}
```

**الردّ المتوقع** (JSON):

```json
{"status": "PASS", "score": 92, "summary": "يوافق معايير الأداء المحددة"}
```

`status` أو `verdict` (تُقبل `passed/ok/success` و`failed/rejected`)، و`score` اختياري،
و`summary`/`notes`/`detail` اختياري (يُعرض حتى 2000 حرف).

---

## 7. سيناريوهات وأدوات شائعة للـ "Arena"

السكريبت **محايد** تجاه نوع الـ Arena؛ ما يتغير هو ما يقف خلف `ARENA_API_URL` أو
ما يُضاف كخطوة إضافية في الـ workflow:

1. **LLM / Prompt Evaluation Arena (Promptfoo, DeepEval):**
   إذا تغيّرت الـ Prompts أو ملفات `skills/*/SKILL.md`، أضف خطوة بعد التقييم تشغّل
   `promptfoo eval` أو `deepeval test run` على مجموعة اختبارات ثابتة (baseline)، ثم ألحق
   ملخصها بـ `result.md` قبل خطوة التعليق. فئة `skills` في التقرير تخبرك متى يلزم ذلك.

2. **Code Arena (AI Code Review — PR-Agent, CodeRabbit):**
   تعمل كتطبيقات GitHub مستقلة تقرأ الـ Diff بنفسها؛ يمكن تشغيلها جنباً إلى جنب مع
   هذا الـ workflow، ويبقى هذا التقرير هو مصدر الإشارات الحتمية (أسرار، مسارات حساسة، اختبارات).

3. **Alibaba Cloud Arena (Kubernetes / Deep Learning):**
   عند تغيّر كود التدريب، أضف خطوة `arena submit …` مشروطة بـ
   `jq -e '.categories.code' result.json` أو بمسار معين داخل `files`.

4. **LLM-as-a-Judge (اختياري):**
   نقطة الربط هي `run_benchmark()` في السكريبت؛ أبسط طريقة هي نشر خدمة صغيرة تستقبل
   العقد أعلاه، تستدعي النموذج (Gemini/OpenAI عبر `nimna/providers/`)، وتعيد
   `{"status","score","summary"}`. لا تفعّل `OPENAI_API_KEY` في الـ workflow قبل ذلك.

---

## 8. الحدود

- الفحص ثابت (static)؛ لا يستبدل `pytest` أو بوابة `00-integrity.yml` أو Trivy/ZAP في `ci.yml`.
- اكتشاف الأسرار heuristic: قد يفوّت أنماطاً غير معروفة وقد ينبّه خطأً؛ الغاية منعُ التسريب
  الواضح قبل المراجعة البشرية، لا إثبات الخلو من الأسرار.
- الـ Benchmark البعيد يعتمد على جودة خدمة Arena التي تشير إليها؛ أي انقطاع يُبلَّغ كـ `ERROR`
  ولا يُوقف الـ workflow (مثل Trivy/ZAP الاستشاريين) إلا مع `--strict`.
