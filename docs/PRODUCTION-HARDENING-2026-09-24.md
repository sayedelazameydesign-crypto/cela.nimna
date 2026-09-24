# Production Hardening — cela.nimna (أصلي Python-native، 2026-09-24)

قرار المالك: حزمة CeliaOS (npm/PowerShell) **لا تُطبَّق** هنا؛ الجوهر أُعيد بناؤه
محلياً فوق `9885f3c` (T7.1 مُغلقة + T8). المسار:

```
T8 ancestor gate → falsifiable CI invariants → blocking security gates →
real health/runtime evidence → pytest + static → docker build → production-gate → PR → main
```

## ما تغيّر فعلاً (كل بند بمخرجه)
1. **بوابة T8-الأنستور:** `scripts/production_gate.sh` يرفض المرشح ما لم
   `git merge-base --is-ancestor 9885f3c…33b908 HEAD` (الخطاف في مكان واحد =
   السكربت؛ CI يستدعيه بـ`fetch-depth: 0`).
2. **الثوابت قابلة-للكذب:** `ci.yml` القديم كان يمرّ حتى مع المخالفة
   (`! grep … || true` سطر 28، و`|| echo "patterns present"` يكذب عند الغياب).
   الآن: خرق الثابت ⇒ `exit 1`، وغياب الأنماط ⇒ `exit 1`، وانقسام المهارتين
   وسطر `should_kill` **مؤكَّد بـassert** لا print.
3. **أمن حاجب:** `continue-on-error` أُزيل من trivy (fs + image) وZAP؛
   `fail_action: false` ⇒ `true` (+`-I`: تحذيرات سلبية استشارية، فشل-مستوى
   عالٍ يكسر CI). ZAP يستهدف عنوان الـrunner الحقيقي (`hostname -I`) لا
   127.0.0.1 الذي لا يصل إليه من حاويته — السبب الأصلي للقناع.
4. **صحة حية لا شكلية:** خطوة الصحة في CI كانت تمر حتى لو لم يقم الخادم
   (loop بلا فشل) وكان يقفلع أصلاً؟ لا — كان يقفلع بـ`gemini` بلا مفتاح فيغفى
   الفحص ذلك. الآن: `MODEL_PROVIDER=mock` + `curl -sf … || exit 1` + طباعة
   الجسد كدليل (`{"status":"ok",…}` — مُثبت حياً أدناه).
5. **تدقيق تبعيات حاجب:** `pip-audit` (مكافئ npm audit الأصلي). أول تشغيل
   حقيقي أصاب ثغرات **فعليّة** في سلسلة الأدوات: `pip 23.0.1` (14× PYSEC،
   الإصلاح ≥26.2) و`setuptools 66.1.1` (PYSEC-2026-3447، الإصلاح ≥83) —
   **أُصلحت بالترقية** (Dockerfile + gate) لا بقناع؛ إعادة التدقيق: نظيف.
6. **docker build حقيقي:** خطوة البوابة السابعة تبني `nimna:production-candidate`
   — **بلا flag تخطي**؛ بيئة بلا docker تبقى حمراء بصدق (هذه البيئة كذلك —
   انظر النتيجة الحية) والشهادة الكاملة من تشغيل CI.
7. **اختبارات البوابة نفسها:** `tests/test_production_gate.py` (9، منها عدادية:
   نسخة workflow مُعبَّثة بقناع ⇒ المدقق يحمرّها؛ sha وهمي ⇒ بوابة الأنستور
   حمراء) — السيت الكامل: **318 passed**.
8. **مصادقة + CORS + حدود + ترويسات (PR لاحق):** البوابة (الخطوة 6) وZAP يشغّلان الخادم الآن
   بـ`NIMNA_ENV=production` ومفتاح عابر مولَّد لكل تشغيل (مُقنَّع، ليس سراً مخزناً)، ويثبتان
   `401` بلا مفتاح و`200` معه، وZAP يثبت الترويسات الأمنية. التفاصيل: `SECURITY.md` و`docs/API-SECURITY.md`.

## التشغيل
```bash
bash scripts/production_gate.sh          # داخل الـvenv — 7 خطوات متتالية
bash scripts/check_ci_falsifiable.sh .github/workflows/ci.yml
```
