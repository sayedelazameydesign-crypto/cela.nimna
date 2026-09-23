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

## قواعد ثابتة

1. لا يُضاف سطر `PASS` في وضع mock أبداً (اختبارات `tests/test_arena_suite.py` تمنعه).
2. كل مهمة جديدة تحتاج `judge_rubric` من اليوم الأول (شرط P5).
3. أي سرّ يظهر في مخرجات مهمة = فشل فحص + صف مميّز في التقرير (القيمة لا تُعرض).
4. P5 يضيف: تشغيل live ميزانياً، حَكَم rubric عبر `run_llm_judge`، وworkflow دوري.
