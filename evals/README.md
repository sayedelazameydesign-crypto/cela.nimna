# Evals — مختبر تطور الوكيل (Arena Task Suite)

> P0.5 من [`docs/architecture/agent-platform-audit.md`](../docs/architecture/agent-platform-audit.md):
> قياس **الوكيل نفسه** على مهام كاملة — لا الـ diff فقط.

## التشغيل

```bash
# الوضع الافتراضي: MockProvider — يثبت أن خط القياس يعمل (حكم MOCKED + لافتة، لا PASS أبداً)
python scripts/run_arena_suite.py --mode mock

# مهمة واحدة فقط، مع تقرير Markdown/JSON
python scripts/run_arena_suite.py --mode mock --task code-01-fizzbuzz-module \
    --output report.md --json-output report.json

# صارم: exit 2 عند أي ERROR أو سرّ أو تراجع مقابل الـ ledger
python scripts/run_arena_suite.py --mode mock --strict
```

## البنية

| المسار | الدور |
|---|---|
| `tasks/*.yaml` | المهام القياسية (10): `coding ×4` · `web ×2` · `data ×2` · `arabic_long ×2` |
| `../scripts/run_arena_suite.py` | العدّاء: يشغّل `Agent` الحقيقي + فحوص حتمية + ledger + تقرير |
| `ledger/ledger.db` | دفتر النتائج (SQLite — غير متتبَّع؛ `*.db` في `.gitignore`) |
| `ledger/BASELINE-mock.md` | نسخة موثَّقة من baseline وضع mock |

## عقد المهمة (YAML)

| الحقل | مطلوب | الوظيفة |
|---|---|---|
| `id` | ✓ | معرّف فريد `a-z0-9-` |
| `category` | ✓ | `coding` \| `web` \| `data` \| `arabic_long` |
| `prompt` | ✓ | رسالة المستخدم للوكيل |
| `requires` | — | قدرات (`network` \| `shell_tool` \| `docker_sandbox`) — الناقص ⇒ صف `SKIPPED` |
| `max_steps` / `max_tool_calls` / `timeout_s` | — | حدود الحلقة لهذه المهمة |
| `seed_files` | — | ملفات تُزرع في workspace المهمة (حارس مسارات: لا `..` ولا absolute) |
| `expected_artifacts` | — | فحص حتمي: الملف موجود (+ اختيارياً `contains`) |
| `must_include` / `must_not_include` | — | فحص كلمات على الردّ النهائي |
| `judge_rubric` | ✓ | معايير حَكَم الـ LLM — **محجوزة لـ P5**، لا تُستخدم اليوم ولا يُخترع لها رقم |
| `allowed_tools` | — | توثيقي: الأدوات المتوقعة للمهمة |
| `requires` | — | `shell_tool` يجعل المهمة `SKIPPED` ما لم يُضبط `SHELL_TOOL_ENABLED` |
| `mock_skills` / `mock_script` / `mock_final` | — | سيناريو حتمي لوضع mock: اختيار مهارة + استدعاء أدوات فعلي خطوة بخطوة + ردّ نهائي — يمكّن قياس «Agent + Tool» دون مفاتيح (الحكم يبقى `MOCKED`) |
| `verify` | — | **P1-T3:** مواصفة تحقق حتمية (لا LLM) تُقيَّم على الـ evidence: `file_exists` · `file_absent` · `content_matches` (contains/equals/regex/sha256) · `exit_code` · `delta` · `delta_sha` · `json_keys` · `command`. الأحكام: `PASS/FAIL/INCONCLUSIVE` — spec فارغ أو دليل ناقص = INCONCLUSIVE لا PASS، وتظهر حالة الـ verifier في التقرير والصف |

## مفردات النتيجة (نفس عقد الأمانة في المستودع)

| Verdict | متى |
|---|---|
| `MOCKED` | وضع mock — **دائماً** مع لافتة أعلى التقرير؛ النتيجة تثبت خط القياس لا الجودة |
| `PASS` / `FAIL` | وضع live فقط — فحوص حتمية (الحَكَم يُضاف في P5) |
| `SKIPPED` | قدرة ناقصة أو لا مزوّد live — لا أرقام مخترعة |
| `ERROR` | انهيار/تعليق — صف صريح بلافتة، لا يُحذف بصمت |

`Check score` = نسبة الفحوص الحتمية الناجحة (artifacts + keywords + فحص الأسرار المعاد
استخدامها من `scripts/evaluate_arena.py`). الـ ledger يقارن كل تشغيل بآخر نتيجة
لنفس المهمة والوضع ويضع علامة **regression** عند التراجع.

## مقاييس قبل/بعد (P1-T1: Baseline vs Agent+Shell)

تُستخرج من الـ audit الحقيقي لا من الانطباعات، وتظهر في التقرير و`summary`:

| Metric | الصيغة |
|---|---|
| `verified` | مهام اكتملت بفحوص حتمية 100% |
| `tool_calls` / `failed_tool_calls` | استدعاءات الأدوات / أخطاء التسجيل أو التحقق |
| `shell_executions` / `failed_commands` | تنفيذات `run_command` الموثّقة / ما انتهى `NONZERO_EXIT\|TIMEOUT\|SANDBOX_ERROR` |
| `security_denials` | أحداث `shell_denied` (DENIED/POLICY_BLOCKED/CONFIRMATION_REQUIRED) |
| `evidence_completeness` | % تنفيذات shell التي حملت Evidence كامل العقد (12 مفتاحاً) |
| `recovered` | مهمة اصطدمت بفشل ثم أكملت بفحوص 100% (مثل `code-05`) |
| `mean_wall_ms` | متوسط زمن الجدار للمهام |

**Baseline v8 (P1-T1..T6 + P1-T7 Execution Gateway، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 · **Verifier: PASS 2 · FAIL 0 · INCONCLUSIVE 0** ·
Checkpoint: COMPLETED 2 · Registry: authorized 1 · Policy: ALLOW 1 ·
**Gateway: invoked 1 · refused 0** — إعادة تشغيل التحقق تعبر الآن نقطة العبور
الوحيدة كاملة (Registry → Capability → Policy → Authorization → Execute →
Observe → Verify → Checkpoint → Evidence) مع تحقق داخلي exit_code لكل أمر.
بصمات fs قبل/بعد مطابقة v4–v7: code-01 `cb0a7629ae1a→c9425def3b7f` وcode-05
`fb681ae9c774→02f57fc9b5e6`. راجع `ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

**Baseline v7 (P1-T1..T5 + P1-T6 Capability/Policy، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 · **Verifier: PASS 2 · FAIL 0 · INCONCLUSIVE 0** ·
Checkpoint: COMPLETED 2 · Registry: authorized 1 · **Policy (`arena-suite-policy@1.0.0`):
ALLOW 1 · DENY 0 · REQUIRE_CONFIRMATION 0** — قرار حتمي بقواعد مرتبة
(deny-outside-workspace أولاً ثم allow-sandbox-shell) عبر boundary من T2،
وكل رفض fail-closed. بصمات fs قبل/بعد مطابقة v4–v6: code-01
`cb0a7629ae1a→c9425def3b7f` وcode-05 `fb681ae9c774→02f57fc9b5e6`. راجع
`ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

**Baseline v6 (P1-T1..T4 + P1-T5 Tool Registry، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 · **Verifier: PASS 2 · FAIL 0 · INCONCLUSIVE 0** ·
Checkpoint: COMPLETED 2 · FAILED 0 · diagnosable 0 · **Registry: registered 2 ·
authorized 1 · denied 0 · revoked 0 · schema failures 0** — إعادة تشغيل التحقق
`command` في code-05 تمر الآن عبر بوابات الـ Registry (`sandbox.command`): 
verifier داخلياً **5/5** (كان 4) وكل صف verify يحمل `reg: {a}✓/{d}✗ ev:{tail}`.
بصمات fs قبل/بعد مطابقة v4/v5: code-01 `cb0a7629ae1a→c9425def3b7f` وcode-05
`fb681ae9c774→02f57fc9b5e6`. راجع `ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

**Baseline v5 (P1-T1..T3 + P1-T4 Checkpoint/Recovery، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 · **Verifier: PASS 2 · FAIL 0 · INCONCLUSIVE 0** ·
**Checkpoint: COMPLETED 2 · FAILED 0 · diagnosable 0** — كل صف shell يحمل
`ckpt STATE:id`، والـ checkpoint مرتبط بـ evidence head (هاش سلسلة الـ audit) +
fingerprint (after-root-hash): code-01 `ckpt COMPLETED` (fp `sha256:c9425def3b7f…`)
وcode-05 `ckpt COMPLETED` (fp `sha256:02f57fc9b5e6…`). المخزن ذرّي وخارج مساحة
العمل المُراقبة — البصمات before/after مطابقة لـ v4 (لا انحراف). راجع
`ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

**Baseline v4 (P1-T1 Shell + P1-T2 Observation + P1-T3 Verifier، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 · **Verifier (حتمي): PASS 2 · FAIL 0 ·
INCONCLUSIVE 0** — code-01 **6/6** (وجود الملف + المحتوى + exit 0 + مشاهدة الدلتا +
إعادة تحقق الهاش + مفتاح JSON لا ينطبق) وcode-05 **5/5**، وكل صف يحمل
`fs: Δ{n} before→after ev:{chain-hash}` في التقرير نفسه. راجع
`ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

**Baseline v3 (P1-T1 Shell + P1-T2 Observation، وضع mock، `SHELL_TOOL_ENABLED=1`):**
9 ran / 2 skipped / 0 error / mean 55.6 / evidence completeness 100% / **2 هاش
artifact مُعاد التحقق منهما عبر الـ delta** (`code-01` الآن 5/5 فحوصاً — مخرجات
التشغيل `src/output.txt` تُشاهَد CREATED وتُطابق sha256؛ و`code-05` 4/4).
منذ v2: فحوص الأرتيفاكت المنتَج بـ shell لم تعد "keyword found" بل
**Delta + Re-observation = Evidence**. راجع `ledger/BASELINE-mock.md` و`BASELINE-mock.json`.

## قواعد ثابتة

1. لا يُضاف سطر `PASS` في وضع mock أبداً (اختبارات `tests/test_arena_suite.py` تمنعه).
2. كل مهمة جديدة تحتاج `judge_rubric` من اليوم الأول (شرط P5).
3. مهام `run_command` تتطلب `requires: [shell_tool]` — والتعليق على الأداة لا يعني توفرها (DENIED).
3. أي سرّ يظهر في مخرجات مهمة = فشل فحص + صف مميّز في التقرير (القيمة لا تُعرض).
4. P5 يضيف: تشغيل live ميزانياً، حَكَم rubric عبر `run_llm_judge`، وworkflow دوري.
