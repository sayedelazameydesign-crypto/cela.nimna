# T7.1-D — عزل وتوازي سكربت الطفرات (أدلة حية، 2026-09-23)

**السكربت:** `scripts/mutations/run_t71_mutations.sh`
**sha256 للسكربت الملتزم:** يُطبع في MANIFEST.sha256 (وبنود هذا الملف أدناه).

## العزل (لكل دورة طفرة)
- `WORK=$(mktemp -d /tmp/mut_t71.XXXXXX)` — مجلد فريد لكل دورة؛ لا مسار ثابت.
- `trap`-less design: التنظيف صريح `rm -rf "$WORK"` بعد كل دورة (والـ summary
  وملفه يُنشآن بـ mktemp ويُحذفان في النهاية). الإثبات الحي بعد التشغيلين
  المتزامنين: `ls -d /tmp/mut_t71.* | wc -l` ⇒ **0** بقايا.
- الشجرة العاملة لا تُمس إطلاقاً — النسخة هي المُختبَرة.

## التوازي
- `--jobs N` (افتراضي 1): يوزّع MB1–MB4 على N عاملاً خلفياً، **لكل عامل
  مجلد mktemp خاص به**؛ المستخدم المشترك الوحيد = ملف summary ويُحدَّث تحت
  **flock(1) حقيقي** (`flock 9` / `flock -u 9` على fd مخصص) — لا منع كلامي.
- الدليل الحي (تشغيلان متزامنان، كل منهما `--jobs 2` ⇒ 8 عمال):

```
$ grep "WORK:" /tmp/conc1.log        $ grep "WORK:" /tmp/conc2.log
[mb4] WORK: /tmp/mut_t71.t8j2Zj      [mb2] WORK: /tmp/mut_t71.r8WRaH
[mb2] WORK: /tmp/mut_t71.uxtaaO      [mb1] WORK: /tmp/mut_t71.WlCerb
[mb3] WORK: /tmp/mut_t71.2863Bh      [mb3] WORK: /tmp/mut_t71.BruZdJ
[mb1] WORK: /tmp/mut_t71.xdPnSv      [mb4] WORK: /tmp/mut_t71.byNPCT

$ grep -h "WORK:" /tmp/conc1.log /tmp/conc2.log | awk '{print $3}' | sort -u | wc -l
8                                     ← 8 مجلدات فريدة، صفر تصادم

$ ls -d /tmp/mut_t71.* 2>/dev/null | wc -l
0                                     ← تنظيف كامل بعد الانتهاء
```

- كلا الاستدعاءين أكمل الطفرات الأربع (أعداد الفشل تحت الحمل المتوازي تتفاوت
  ±1 اختبار حساس-لتوقيت — كل الطفرات بقيت قاتلة ≥1 في كلتا التشغيلتين).

## sha256 للنسخة المُختبَرة (نسخة الطفرات، لا الشجرة)
يُطبع داخل المخرج لكل طفرة (تشغيلة `--jobs 1` الحية):

```
[mb1] mutated-copy sha256: 8264b4cc145e28a1658ca1c71c54cfe230f4f23a75a2c0c9cc6361d631953e97  (core/agent.py)
[mb2] mutated-copy sha256: ad3fea1a179544891c3ea42c5a95c0af78dcd039f85d2a472d1c6673d7c07765  (core/agent.py)
[mb3] mutated-copy sha256: 7cdc42024c7db29ad8390f15b6a2826bd5ed7cc2d13c7e93bb5b63ade7ad4566  (agents/base.py)
[mb4] mutated-copy sha256: 7fbbd14d1668c7610c5a20d330cc1ea6890f4ef411d57199e183f6db7a01aaa3  (core/agent.py)
```

## النتائج (تشغيلة --jobs 1، بأسماء الاختبارات)
| MB | القتلة | 
|---|---|
| mb1 refusal→legacy-fallback | `test_bound_gateway_refuses_unregistered…` + `test_shell_execute_bound_gateway_never_reaches_handler` (2 failed) |
| mb2 failure→legacy-fallback | `test_gateway_failure_never_falls_back_to_legacy` (1 failed) |
| mb3 swarm-refusal removed | 3 failed (swarm tests بالأسماء في المخرج الحي أعلاه) |
| mb4 binding ignored | 6 failed بالأسماء |
baseline نظيف: **30 passed**.
