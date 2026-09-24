# T7.1-C — SHELL_TOOL_ENABLED: افتراضي معطَّل + اختبار سلبي + معايير خروج P5

## الافتراضي بأمر (الطبقات الثلاث — مخرج حي من الاختبار)
```
test_default_is_off_all_three_layers
  fresh.shell_tool_enabled is False                                # config/runtime
  dataclasses field default is False                               # تعريف الحقل
  '_env_bool("SHELL_TOOL_ENABLED", False)' in Settings.from_env src # env
4 passed in 0.28s
```

## الاختبار السلبي (التركيبة الممنوعة)
`test_no_gateway_no_skill_shell_is_unreachable`: وكيل **بلا gateway**
(None)، مسار legacy، وبلا أي مهارة محمَّلة ⇒
1. `run_command` (المعايد المحلي المقيّد بالـ workspace) **غير مُسجَّل** في
   الـ registry (بوابة التسجيل تقرأ env مباشرة)،
2. تنفيذ مباشر عبر الـ registry ⇒ `ok=False` + `unknown tool` (لا fallback)،
3. `allowed_tools` لا يتضمنه (مسار المهارات لا يعرضه بلا مهارة).

**ملاحظة نطاق صادقة:** اسم `shell_execute` في الـ registry يعود لحزمة
computer (bash داخل حاوية سطح المكتب المعزولة) — **خارج نطاق هذا البند** ولا
يُدّعى عنه تغطية داخل الحاوية إطلاقاً.

`test_registration_gate_reads_env_while_handler_gate_reads_settings`: البوابتان
منفصلتان ومثبتتان — التسجيل بـ env، والمعالج يرفض DENIED ما دامت Settings
معطلة حتى لو سُجّلت الأداة.

## معايير خروج الهجرة (كُتبت في تذكرة P5)
`docs/architecture/agent-platform-audit.md` — قسم «★ T7.1-C — معايير خروج
الهجرة لفتح P5» (إلحاق لا يمس الأرقام stale): لا فتح P5 قبل (1) بقاء الافتراضي
معطلاً بالطبقات الثلاث مع اختباره، (2) قرار المزود متخذاً، (3) قاعدة ما قبل
T8 للقدرات الجديدة، (4) أي تخفيف يمر ببوابة T7.1-B.
