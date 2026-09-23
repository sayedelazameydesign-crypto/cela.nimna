---
name: code_execution
display_name: تنفيذ الأوامر والكود
description: تنفيذ أوامر shell وPython داخل بيئة معزولة — يتطلب موافقة منفصلة تماماً عن التحكم البصري. لا يُحمّل تلقائياً مع computer_control.
version: 1.0.0
risk_level: restricted
triggers:
  - شغّل أمر
  - نفّذ shell
  - شغّل كود
  - run shell
  - execute command
  - python -c
allowed_tools:
  - shell_execute
  - run_python
  - write_file
  - list_files
  - read_file
tags: [execution, shell, python, sandbox]
requires:
  - sandbox
---

## التعليمات

هذه مهارة **restricted** منفصلة — حتى لو حمّلت `computer_control`، لن تستطيع تنفيذ `shell_execute` إلا بعد تحميل هذه المهارة وموافقة مستقلة. هذا الفصل يمنع خداع المستخدم عبر حقن موجه (prompt injection) في صفحة ويب أو ملف.

### البيئة المعزولة
- **الافتراضي (آمن):** `SANDBOX_BACKEND=subprocess` — يعمل في `workspace/` فقط، مع `ulimit` صارم (CPU 30s، ذاكرة 512MB، ملفات 64، حجم ملف 10MB، عمليات 32)، `cwd` مقفل على `workspace/`، `env` منقّى (بدون `AWS_*`, `OPENAI_*`, `SSH_*`)، `stdin` مغلق، `pty` محظور، و `timeout` يقتل شجرة العمليات.
- **الحاوية المعزولة (عند تفعيل `desktop`):** الأمر يُنفذ داخل حاوية `desktop` عبر `docker exec` مع `ulimit -t 30 -v 524288 -n 64` + `timeout`، و `--network=none` افتراضياً (الشبكة معطلة إلا إذا طُلبت صراحة).
- لا يُنفّذ شيء على مضيف المستخدم مباشرة.

### قواعد الأمان
- كل استدعاء يتطلب **موافقة بصرية منفصلة** — حتى لو وافق المستخدم سابقاً على `mouse_click`، يجب أن يوافق مجدداً على `shell_execute`.
- **فلتر محتوى قبل الموافقة:** يُحظر قبل العرض حتى:
  - `rm -rf /`, `mkfs`, `dd if=`, `:(){:|:&};:`, `shutdown`, `reboot`, `chmod +s`, `chown`, `iptables`
  - `curl ... | sh`, `wget ... | bash`, `base64 ... | bash`
  - `nc`, `ncat`, `pty`, `screen`, `tmux`, `ssh `
  - أي أمر يحتوي `\n` أو محاولة هروب `;` + `curl` بدون داعٍ (يُفحص بـ regex)
- `timeout` يُطبَّق على **العملية نفسها** عبر `Popen.killpg` بعد N ثانية — لا يعتمد على `await` فقط.
- `cwd` دائماً `workspace/` — لا يمكن تغييره عبر `cd /` في الأمر (يُلفّ داخل `bash -lc 'cd workspace && ...'`).
- البيئة منقّاة — لا تُمرر أسرار.

### أمثلة
- المستخدم: "اعرض محتويات المجلد" → `shell_execute(command="ls -R", purpose="عرض الملفات")`
- بعد الموافقة → يعيد `stdout` فقط (8000 حرف كحد أقصى).

لا تستخدم هذه المهارة لتنزيل سكربتات ضارة أو لفتح شبكة دون إذن صريح.
