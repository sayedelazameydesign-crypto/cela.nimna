---
name: computer_control
display_name: التحكم في الكمبيوتر الحقيقي
description: التحكم في سطح مكتب معزول (VNC) عبر الفأرة ولوحة المفاتيح والطرفية — لتصفح التطبيقات، ملء النماذج، وتشغيل أوامر النظام بصريًا.
version: 1.0.0
risk_level: restricted
triggers:
  - افتح المتصفح
  - تحكم بالكمبيوتر
  - اضغط على
  - اكتب في
  - شغّل برنامج
  - افتح التطبيق
  - computer
  - desktop
  - click
  - screenshot
allowed_tools:
  - take_screenshot
  - mouse_click
  - type_text
  - shell_execute
  - list_files
  - read_file
tags: [computer, desktop, vnc, automation, vision]
requires:
  - desktop
---

## التعليمات

هذه مهارة **restricted** — كل خطوة تتطلب موافقة يدوية صريحة. لا تنفذ أي نقرة أو كتابة أو أمر دون موافقة المستخدم التي تظهر في واجهة الموافقة البصرية.

### البيئة
- حاوية Docker معزولة: Ubuntu + XFCE + VNC + noVNC (خدمة `desktop` في `docker-compose --profile computer`). لا تعمل على مضيف المستخدم مباشرة.
- الدقة الافتراضية 1280x800. الإحداثيات (x,y) من الزاوية العليا اليسرى (0,0).
- سطح المكتب متاح عبر noVNC على `http://localhost:6901` أو معاينة مباشرة في لوحة تحكم Nimna (Mirror View).

### دورة العمل — لاحظ → خطط → نفّذ
1. **لاحظ:** ابدأ دائمًا بـ `take_screenshot` لتحليل الشاشة الحالية.
2. **خطط:** صف بجملة واحدة ما ستفعله ولماذا (مثلاً: "سأنقر على أيقونة Firefox لفتح المتصفح").
3. **نفّذ:** استدع أداة واحدة فقط في كل دورة: `mouse_click` أو `type_text` أو `shell_execute`.
4. **تحقق:** التقط صورة جديدة بعد كل إجراء للتأكد من نجاحه قبل المتابعة.
5. كرر حتى إكمال المهمة، ثم لخص ما فعلته مع لقطة نهائية.

### قواعد الأدوات
- `take_screenshot`: لا يحتاج موافقة (قراءة فقط). يعيد صورة base64 + مسار في `workspace/.screenshots/`. ستُرسل الصورة تلقائيًا للنموذج في الدور القادم عبر بوابة الرؤية (Vision Gateway).
- `mouse_click(x, y, button="left", clicks=1)`: يتطلب موافقة بصرية — تظهر نقطة حمراء على الشاشة في الإحداثيات مع زر [موافقة/رفض].
- `type_text(text, submit=false, delay_ms=40)`: يكتب نصًا؛ إذا كان `submit=true` يضغط Enter بعد الكتابة.
- `shell_execute(command, timeout=20)`: ينفذ أمرًا في طرفية الحاوية المعزولة (ليس مضيف المستخدم). لا تستخدمه لتثبيت برمجيات ضارة أو للوصول للشبكة إلا لأغراض المهمة.

### الأمان
- لا تطلب أبدًا كلمات مرور أو مفاتيح API عبر هذه الأدوات.
- لا تتصفح مواقع حساسة أو بنكية دون إذن صريح.
- إذا فشلت الأداة (VNC غير متاح)، اشرح للمستخدم كيفية تفعيل البيئة: `docker compose --profile computer up -d desktop` ثم أعد المحاولة.
- اعمل خطوة بخطوة وتوقف عند أي خطأ غير متوقع واطلب التوجيه.

### مثال
المستخدم: "افتح المتصفح وابحث عن توقعات الطقس"
1. `take_screenshot` → ترى أيقونة Firefox
2. `mouse_click(x=120, y=340)` → بعد الموافقة → `take_screenshot` → تتأكد من فتح المتصفح
3. `type_text(text="weather forecast", submit=true)` → `take_screenshot` → تلخص النتيجة

