# Browser Use Cloud V4 — قواعد مختصرة

- استخدم توثيق `https://docs.browser-use.com/llms.txt` والصفحات المرتبطة به؛ لا تستخدم الصادرات القديمة `/cloud/llms*.txt` أو `/open-source/llms*.txt`.
- الاستيراد الصريح في Python/TypeScript هو V4 عندما يكون SDK هو المسار المستخدم.
- الترويسة: `X-Browser-Use-API-Key: <key>` بدون `Bearer`.
- اقرأ `/api/v2/billing/account` لمعرفة المشروع، السعة، الجلسات النشطة، والرصيد.
- `X-RateLimit-Limit` نافذة مدتها خمس ثوانٍ؛ القيمة 125 تعني 25 طلباً/ثانية، وليست 125 RPS. احترم `Retry-After` و`retry_after_seconds`.
- الجلسة، مساحة العمل، والملف الشخصي معرفات مختلفة. نفّذ العمليات التي تكتب ملفات مشتركة بالتسلسل وانتظر اكتمالها.
- `GPT-6 Astra` يقبل `low`, `medium`, `high`, `xhigh`, و`max` فقط؛ `none` و`minimal` غير صالحين.
- بعد إنشاء متصفح مملوك أوقفه عبر `PATCH /api/v4/browsers/{id}` مع `{"action":"stop"}` داخل `finally`.

المحول المحلي في `nimna/browser/cloud_v4.py` لا يستدعي الشبكة عند الاستيراد، ويفشل مغلقاً عندما لا يوجد مفتاح أو ميزانية.
