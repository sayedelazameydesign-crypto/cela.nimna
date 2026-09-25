# تقرير تدقيق المستودع + الخطة التنفيذية — cela.nimna

**التاريخ:** 2026-09-25 · **الCommit:** `0a3a563` (main) · **بيئة الفحص:** Python 3.11.2، تثبيت مطابق لبيئة CI (`pip install -r requirements.txt` + `pip install -e . --no-deps`)

---

## 1) الملخص التنفيذي

المستودع **بحالة جيدة جدًا في عموده التشغيلي**: 510/510 اختبارًا ناجحًا، لا ثغرات معروفة في التبعيات (pip-audit)، سلسلة أدلة T7.1 مطابقة الهاشات، والـ API يقلّد بوابة المصادقة والرؤوس الأمنية كما هو مصمَّم. لكن التدقيق كشف **5 أخطاء وظيفية حقيقية** (أحدها عيب تصميمي في نقطة دخول الحزمة)، **3 مخاطر أمنية**، **6 فجوات نشر/تشغيل** (أبرزها: مقياس HPA الموثَّق غير منفَّذ في الكود إطلاقًا)، و**دينًا فنيًا قابلًا للقياس** (651 ملاحظة ruff في كود الإنتاج + 88 خطأ mypy، ولا يوجد أي تحليل ساكن في CI).

| الفئة | العدد | أقصى خطورة | أين |
|---|---|---|---|
| أخطاء وظيفية مثبتة (P1) | **6** | عالية | `nimna/__main__.py`، `execution/__init__.py`، سكربت التحقق، مدقق المهارات |
| مخاطر أمنية (P2) | **3** | متوسطة | MD5 ×6 مواضع، `pickle.loads` من Redis، افتراضات الربط 0.0.0.0 |
| فجوات نشر/تشغيل (P3) | **6** | عالية تشغيليًا | `/metrics` غير موجود، PubSub غير منفَّذ، k8s SQLite بلا PVC |
| جودة كود (P4) | 651 ruff + 88 mypy | منخفضة-متوسطة | كود الإنتاج والاختبارات |
| انحراف وثائقي (P5) | **3** | منخفضة | أرقام اختبارات قديمة، مراجع مسارات، تعارض إصدارات actions |

> **قاعدة المصداقية:** كل بند أدناه مثبت بتجربة فعلية أو بموقع `ملف:سطر`. البنود التي فحصتها وثبتت سلامتها مذكورة في §7، والإنذارات الكاذبة التي **يجب عدم إصلاحها** في §6.

---

## 2) الأخطاء الوظيفية المثبتة (P1)

### P1-1 · استيراد `nimna.__main__` ينفّذ الـ CLI ويقتل العملية — عالي
- **الموقع:** `nimna/__main__.py:4` — `sys.exit(main())` بلا حارس `if __name__ == "__main__":`
- **الإثبات:** `pkgutil.walk_packages(nimna.__path__, ...)` ثم استيراد الوحدات → العملية تموت بـ `SystemExit(2)` ورسالة `the following arguments are required: command`.
- **الأثر:** أي أداة تستورد الحزمة برمجيًا (فهرسة IDE، مولدات توثيق pdoc/Sphinx، اكتشاف إضافات، سكربتات فحص) تنهار.
- **الحل:** تغليف الاستدعاء بالحارس القياسي (سطران) + اختبار regression يستورد `nimna.__main__` ويتأكد أنه لا ينتج `SystemExit`.

### P1-2 · ثلاث فئات مختلفة تحمل اسم `PolicyDecision`، إحداها تحجب الأخرى — عالي (كامن)
- **المواقع:**
  - `nimna/governance/policy.py:22` → `Enum` (ALLOW/APPROVAL_REQUIRED/DENY) — المستخدمة فعليًا في `core/agent.py`
  - `nimna/execution/policy.py:67` → dataclass غنية (effect, reason, policy_id, version, rule)
  - `nimna/execution/tool_registry.py:348` → dataclass بسيطة (allowed: bool, reason)
- **الخلل:** `nimna/execution/__init__.py` يستورد **كليهما** بنفس الاسم (سطرا 35 و54) — الثاني يحجب الأول في فضاء الحزمة، و`__all__` يذكر `PolicyDecision` مرتين (سطرا 74 و79). أي `from nimna.execution import PolicyDecision` مستقبلًا سيُرجع الفئة الخطأ **بصمت**.
- **الحل:** إعادة تسمية الاستيراد (`RegistryPolicyDecision`) أو الاعتماد على المسار الكامل، وإزالة الازدواج من `__all__`، وتوثيق الفرق.

### P1-3 · `tomllib` يكسر دعم Python 3.10 المعلَن — متوسط
- **الموقع:** `scripts/check_fastapi_cloud_link.py:17` — `import tomllib` بلا حماية، بينما `pyproject.toml` يعلن `requires-python = ">=3.10"`.
- **الأثر:** على Python 3.10 ينهار السكربت بـ `ModuleNotFoundError`. CI لا يكتشفه لأن كل الـ workflows على 3.11 فقط.
- **الحل (اختر واحدًا):** رفع `requires-python` إلى `>=3.11` (الأصدق — CI وDockerfile كلاهما 3.11)، أو fallback إلى `tomli` مع إضافته شرطيًا للتبعيات.

### P1-4 · تحذير كاذب في `nimna skills validate` و`nimna doctor` للأدوات opt-in — متوسط
- **الإثبات:** بدون `SHELL_TOOL_ENABLED` يظهر: `shell_execution → references unknown tools: run_command (will be ignored at runtime)` و«1 skill(s) reference unknown tools». مع `SHELL_TOOL_ENABLED=1` يصبح `[OK]` — لأن الأداة `run_command` مسجّلة شرطيًا (`nimna/tools/builtin/shell.py:66`).
- **الأثر:** أداة التحقق تعطي إنذارًا خاطئًا طالما الراية مطفأة (وهي الافتراض الآمن) — يستنزف ثقة المستخدم بالمدقق.
- **الحل:** يعرف المدقق قائمة الأدوات المسجّلة شرطيًا (استدعاء التسجيل الافتراضي مع تفعيل كل الرايات داخل بيئة محاكاة، أو ثابت `OPT_IN_TOOLS`) ويصنّفها «متاحة عند التمكين» بدل «unknown».

### P1-5 · `scripts/run_arena_suite.py` يفشل كليًا بدون `pip install -e .` — متوسط
- **الإثبات:** بتثبيت `requirements.txt` فقط: **11/11 مهمة → verdict `ERROR` / `cannot import nimna: ModuleNotFoundError`**، واختبار `tests/test_arena_suite.py::test_cli_subprocess_writes_markdown_and_json` يفشل. يعمل فقط بعد `pip install -e .` (لأن `sys.path[0]` عند تشغيل سكربت = مجلد `scripts/` لا الجذر).
- **الأثر:** أي مشغّل محلي (أو CI مستقبلي ينسى خطوة التثبيت) يحصل على مجموعة تقييم حمراء بالكامل بلا سبب حقيقي.
- **الحل:** في أول السكربت: `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` قبل استيراد `nimna`.

### P1-6 · أرقام اختبارات قديمة في الوثائق — منخفض (لكنه يشوّه الحقيقة المنشورة)
- **الإثبات:** README يقول «83 اختبار» في 3 مواضع (أسطر 59، 373، 463) وCONTRIBUTING يقول «42+» — **الحقيقة 510 اختبارًا** (اجتازت كلها).
- **الحل:** حذف الأرقام الثابتة من الوثائق أو ربطها بأمر حي (`pytest --collect-only -q`)، مع إضافة فحص انحراف (§المرحلة 4).

---

## 3) المخاطر الأمنية (P2)

### P2-1 · `hashlib.md5` في 6 مواضع (bandit: HIGH)
- **المواقع:** `nimna/core/agent.py:776,778` (بصمة شعورية للقطات الشاشة)، `nimna/tools/builtin/computer.py:148,151`، `nimna/vision/cache.py:32,34` (مفاتيح كاش).
- **التقييم:** استخدامات غير تشفيرية (بصمات/مفاتيح) — الخطورة العملية منخفضة، لكنها تخالف سياسة أمنية صارمة وتُظهر بوابة bandit حمراء.
- **الحل:** `hashlib.md5(..., usedforsecurity=False)` (أدق وأرخص) أو SHA-256. لا يؤثر على أدلة T7.1 (هاشاتها sha256 أصلًا — تحققت).

### P2-2 · `pickle.loads` على بيانات قادمة من Redis — متوسط (RCE عند اختراق الكاش)
- **الموقع:** `nimna/vision/cache.py:68`.
- **السيناريو:** Redis مُخترق أو مشترك أو مُسمَّم → قيمة pickle خبيثة → تنفيذ كود في الحاوية.
- **الحل:** تسلسل JSON (القيم بيانات صورة/وصف فقط)، أو `hmac` توقيع للقيم، أو تقييد Redis بشبكة موثوقة + `usedforsecurity` لا يكفي هنا. JSON هو الأنظف مع مفتاح إصدار مخطط.

### P2-3 · الربط على `0.0.0.0` في الافتراضيات — منخفض (بحاجة قرار صريح)
- **المواقع:** `nimna/config.py:221,309`، `nimna/tools/builtin/web.py:160`.
- **التقييم:** مقبول داخل حاويات/k8s، ومرفوض للتطوير المحلي على حاسوب شخصي. البوابة الأمنية (رفض الإقلاع بلا `NIMNA_API_KEY` في production) موجودة وسليمة — تحققت منها.
- **الحل:** قرار موثق: ربط `127.0.0.1` افتراضيًا في `NIMNA_ENV=development`، والإبقاء على `0.0.0.0` في production قصد الحاويات، مع سطر تحذير في سجل الإقلاع.

> **فجوة حوكمة:** لا يوجد **أي** تحليل ساكن أمني في CI (لا bandit/ruff/mypy — grep في `.github/workflows/` فارغ). الموجود: Trivy + ZAP + pip-audit + بوابة سلوكية — ممتاز، لكن bandit ببساطة سيكتشف P2-1/P2-2 تلقائيًا ولن يُكتشفان اليوم إلا يدويًا.

---

## 4) فجوات النشر والتشغيل (P3)

### P3-1 · مقياس HPA المخصص `active_websockets` **غير منفَّذ في الكود إطلاقًا** — عالي تشغيليًا
- **الإثبات:** `requirements.txt` يثبّت `prometheus-client` «HPA custom metric active_websockets»، و`k8s/hpa.yaml:15` يوسّع بناءً على المقياس، و`k8s/hpa.yaml:35` (ConfigMap) يقول «Expose `active_websockets` via /metrics (prometheus_client)» — لكن **لا يوجد** أي `import prometheus` أو endpoint `/metrics` في `nimna/` كله (grep فارغ)، ولا كود عدّاد WebSocket نشط.
- **الأثر:** قاعدة المقياس في HPA بلا مصدر — التوسّع المخصص لن يعمل أبدًا (يبقى CPU/الذاكرة فقط)، وتبعية مثبتة بلا مستفيد.
- **الحل:** تنفيذ `/metrics` مع `Gauge` باسم `active_websockets` (زيادة في `ws_dashboard` accept وأخيراً finally للإنقاص) — عبء صغير والتبعية جاهزة — أو حذف المقياس من hpa.yaml + حذف التبعية. **التوصية: التنفيذ.**

### P3-2 · توثيق Redis PubSub (`nimna:sessions`) بلا أي كود — متوسط
- **الإثبات:** `k8s/hpa.yaml` (التعليق العلوي + ConfigMap): «code in nimna/api/app.py publishes to Redis… Each pod subscribes to `nimna:sessions`» — grep في `nimna/` عن `publish|subscribe|nimna:sessions` **فارغ**. الاستخدام الوحيد لـ Redis هو كاش الرؤية (`vision/cache.py`).
- **الأثر:** بـ `replicas: 2`، إبطال كاش الموافقات المعلّقة بين النسخ موصوف ولا يعمل (قاعدة البيانات تبقى مصدر الحقيقة، لكن الوثيقة تعد بسلوك غير موجود).
- **الحل:** إما تنفيذ PubSub (نشر عند إنشاء/حل موافقة، اشتراك عند الإقلاع) أو تصحيح الوثائق لتصف «مزامنة عبر DB فقط».

### P3-3 · k8s: SQLite محلي بلا PVC مع `replicas: 2` + وسم صورة متحرك — متوسط
- **الإثبات:** `k8s/deployment.yaml`: لا `DB_PATH` خارجي ولا `PersistentVolumeClaim`، ولا `volumeMounts` → كل نسخة بقاعدة SQLite منفصلة على emptyDir **تُمسح عند إعادة التشغيل**، والموافقات المعلّقة/الذاكرة تتفرق بين النسختين. كذلك `image: nimna-agent:latest`.
- **الحل (بترتيب تفضيل):** (أ) `DB_PATH=postgresql://…` + Postgres مُدار — الكود يدعمه؛ (ب) PVC + `replicas: 1` مع توضيح السبب؛ وتثبيت وسم الصورة على SHA قصير للمستودع.

### P3-4 · Dockerfile يثبّت الحزمة بلا الميزات الاختيارية — منخفض
- **الإثبات:** `Dockerfile` ينفّذ `pip install .` (تبعيات pyproject فقط) — `redis/qdrant-client/numpy/prometheus-client` خارجها (في requirements.txt حصرًا) → داخل الحاوية تتراجع ميزات الكاش/الذاكرة الشعاعية/المقاييس صامتة إلى fallback.
- **الحل:** إضافة extras اختيارية في pyproject (`[project.optional-dependencies] infra = [...]`) وتثبيتها في Dockerfile صراحة، أو توثيق التدهور المقصود.

### P3-5 · `pytest` في `requirements.txt` (بيئة الإنتاج) — منخفض
- إطار اختبار يُثبَّت مع إنتاجية الخدمة في كل النشر (Render/k8s/ZAP job). **الحل:** نقله إلى `[project.optional-dependencies].dev` حصرًا (وهو موجود هناك أصلًا).

### P3-6 · تعارض إصدارات `actions/checkout` — منخفض
- `fastapi-cloud-deploy.yml:35` يستخدم `@v5` وبقية الـ workflows (8 مواضع) `@v4`. **الحل:** توحيد (سواء v4 أو v5) — أساسًا لقراءة سلوك متسق وتجنب انزلاق صمت.

---

## 5) ديون الجودة (P4) والوثائق (P5)

### P4 · التحليل الساكن — لا يكسر التشغيل لكنه يحجب الأخطاء المستقبلية
| الفاحص | العدد | أبرز الفئات |
|---|---|---|
| **ruff** على كود الإنتاج (`nimna/`, `scripts/`, `security/`) | **651** | UP045 ×260 (`Optional[X]`→`X \| None`) · BLE001 ×131 (except عريض) · S110 ×49 (try/except-pass يبتلع الأخطاء) · I001 ×42 · **F401 ×26 واردة ميتة** · UP035 ×18 (استيرادات deprecated) · F841 ×6 متغيرات ميتة · F811 ×3 تعريفات حاجبة |
| **ruff** على `tests/` | 147 | أغلبها I001/F401/C408 أسلوبية |
| **mypy** على `nimna/` | **88 خطأ في 23 ملفًا** | union-attr ×18 · arg-type ×10 · operator ×9 · attr-defined ×9 (غالبها حمايات ناقصة لقيم `None` — لم أثبت انهيار تشغيليًا لها في المسارات المختبَرة) |

- **467 ملاحظة ruff قابلة للإصلاح التلقائي** (`ruff check --fix`) — تُنفَّذ في PR واحد مع مراجعة الـ diff.
- أمثلة إنذارات كاذبة في mypy: `api/app.py:541` (نمط `__getattr__` الكسول لـ `app` — يعمل فعليًا)، و`providers/*` ToolCall بقيم افتراضية.
- أخطر ما في الموضوع: **CI لا يشغّل أيًّا من الفاحصين** — أي تراجع مستقبلي يمرّ صامتًا.

### P5 · انحراف وثائقي
1. أرقام الاختبارات القديمة (P1-6 أعلاه).
2. مراجع مسارات غير موجودة: أغلبها **مقصود** كخطط مستقبلية في `docs/architecture/agent-platform-audit.md` (12 مسارًا blueprint)، و`workspace/.screenshots/` في README/SECURITY يُنشأ وقت التشغيل. منخفض — يُوصى بوسم صريح «(مخطط)» بدل الحذف.
3. تلميح مضلل في `nimna doctor`: طباعة «run: chmod 600 …» حتى **عند نجاح** فحص الأذونات (منطق `check()` في `cli.py` يطبع hint دائمًا بغضّ النظر عن النتيجة).

---

## 6) إنذارات كاذبة — لا تُصلح (توثيق لمصداقية التقرير)

| الإنذار | لماذا هو كاذب (تحقق يدوي) |
|---|---|
| bandit B608 «SQL injection» في `memory/store.py:273,300` | الـ f-string يبني `WHERE … AND …` من عبارات ثابتة فقط؛ **القيم كلها parameterized** (`?`) |
| bandit S105 «hardcoded password» في `verification.py:65` و`governance/policy.py:19` | `Verdict.PASS` واسم مفتاح قاموس `"SECRET"` — أسماء رمزية لا بيانات سرية |
| «VNC_PASSWORD ميت» في `.env.example` | يقرؤه `infra/desktop/vnc-guard.sh` و`docker-compose.yml:107` — ليس كود Python عمدًا |
| إنذار الواردات الميتة للفحص `run_command` في مهارة `shell_execution` | انظر P1-4 — الأداة حقيقية opt-in |

---

## 7) ما فُحص وثبتت سلامته

- **الاختبارات:** 510/510 ناجحة (ببيئة CI المطابقة) · `python -m compileall` نظيف · استيراد كل وحدات `nimna` (68 وحدة) ينجح (باستثناء P1-1).
- **تبعيات:** pip-audit — لا ثغرات معروفة في القيود المثبتة.
- **سلامة الأدلة:** `docs/evidence/T7.1/MANIFEST.sha256` — كل الهاشات مطابقة · مجموعة Arena: 7 تنفيذ / 4 تخطي / **0 أخطاء / 0 انحدارات**.
- **سكربتات التحقق الثلاثة:** `verify_capabilities` · `check_render_blueprint` (30 متغيرًا مقروءًا) · `check_fastapi_cloud_link` — كلها PASS.
- **البنية:** كل ملفات YAML صالحة (k8s متعدد المستندات سليم) · 8 سكربتات bash تمرّ `bash -n` · `.gitignore` يغطي `.env` وقواعد البيانات.
- **تشغيل فعلي:** `nimna serve` على وضع production — `/api/health` سليم، `/api/tools` بلا مفتاح = 401 وبمفتاح = 200، رؤوس `X-Frame-Options: DENY` و`nosniff` و`Referrer-Policy` كلها حاضرة، UI يخدم 200، و`nimna doctor` يفشل بأمان (exit 1) عند غياب المفاتيح.
- **أسرار:** لا مفاتيح صلبة في الملفات المتعقبة؛ المفاتيح كلها عبر بيئة/أسرار CI.

---

## 8) الخطة التنفيذية

> مبدأ التنفيذ: كل مرحلة تسلّم قيمة قابلة للتحقق ولا تخلط الإصلاح التلقائي باليدوي؛ كل خطأ P1 يقابله اختبار regression يمنع عودته.

### المرحلة 0 — إصلاحات فورية (≤ نصف يوم) 🔴
| # | المهمة | الملفات | الجهد | معيار القبول |
|---|---|---|---|---|
| T0.1 | حارس `__main__` + اختبار regression للاستيراد البرمجي | `nimna/__main__.py`, `tests/` | 15 د | `walk_packages` كامل لا يرمي `SystemExit` |
| T0.2 | فك تعارض `PolicyDecision`: استيراد مؤهَّل (`RegistryPolicyDecision`) وإزالة ازدواج `__all__` | `nimna/execution/__init__.py` | 30 د | `from nimna.execution import PolicyDecision` تُرجع فئة policy حصرًا + ruff F811=0 في الملف |
| T0.3 | حسم دعم 3.10: رفع `requires-python>=3.11` **أو** fallback `tomli` | `pyproject.toml` أو `scripts/check_fastapi_cloud_link.py` | 15 د | السكربت يعمل على الحد الأدنى المعلن |
| T0.4 | مسار fallback لـ sys.path في سكربت الـ arena | `scripts/run_arena_suite.py` | 15 د | المجموعة تعمل بـ requirements فقط: 0 صف ERROR |
| T0.5 | توحيد `actions/checkout` (v4 في الجميع) | `fastapi-cloud-deploy.yml` | 5 د | grep يظهر إصدارًا واحدًا |
| T0.6 | حذف/تصحيح أرقام الاختبارات في README وCONTRIBUTING | وثائق | 15 د | لا رقم ثابت بلا مصدر آلي |

### المرحلة 1 — الأمن (أسبوع) 🟠
| # | المهمة | الملفات | معيار القبول |
|---|---|---|---|
| T1.1 | `usedforsecurity=False` للـ MD5 الستة (أو sha256) | `agent.py`, `computer.py`, `vision/cache.py` | bandit: صفر نتائج HIGH |
| T1.2 | استبدال `pickle` بـ JSON في كاش الرؤية + مفتاح إصدار مخطط + اختبار | `vision/cache.py` | لا `pickle.loads` على مدخلات خارجية |
| T1.3 | **إضافة bandit بوابة CI** (`bandit -r nimna security scripts -lll`) + قرار موثق لـ 0.0.0.0 | `.github/workflows/ci.yml` | الفحص أحمر عند أي HIGH جديد |
| T1.4 | رفع S110/BLE001 الأعلى خطرًا: logging بالسياق بدل الابتلاع الصامت (أولوية: `execution/`, `api/`) | دفعات | انخفاض BLE001 ≥ 50% دون كسر الاختبارات |

### المرحلة 2 — الجودة المستمرة (أسبوعان) 🟡
| # | المهمة | التفاصيل | معيار القبول |
|---|---|---|---|
| T2.1 | PR واحد `ruff --fix` للفئات الآمنة (UP045/I001/F401/RUF022/FURB167 — 467 بندًا) | مراجعة بشرية للـ diff | الاختبارات خضراء + الإنتاج ruff < 200 |
| T2.2 | تفعيل ruff في CI: حاجز على `F,E9,B,S` · استشاري على `UP,I,C4` | إعداد `pyproject.toml` | CI يمنع أي F401 جديد |
| T2.3 | mypy تدريجي: تكوين + قائمة ملفات نظيفة تنمو، ومنع الانحدار | `mypy.ini` + CI (استشاري ثم حاجز) | 0 أخطاء على الملفات المرصودة |
| T2.4 | مدقق المهارات يعرف الأدوات opt-in (تصنيف «متاحة عند التمكين») | `skills validate`, `doctor` | صفر تحذير كاذب في الوضع الافتراضي |
| T2.5 | تلميح `doctor` يظهر عند الفشل فقط | `cli.py:222` | مخرجات doctor بلا تناقض |

### المرحلة 3 — النشر (أسبوعان–شهر) 🟢
| # | المهمة | التفاصيل | معيار القبول |
|---|---|---|---|
| T3.1 | **تنفيذ `/metrics` + Gauge `active_websockets`** (التبعية مثبتة أصلاً؛ زيادة عند accept وإنقاص في finally) | `api/app.py` | `/metrics` يعرض المقياس، ومسار e2e يثبت العدّ |
| T3.2 | تنفيذ PubSub `nimna:sessions` أو تصحيح وثائق hpa.yaml لصالح «DB مصدر الحقيقة» | `api/app.py` أو `k8s/hpa.yaml` | الوثيقة تطابق الكود حرفيًا |
| T3.3 | k8s: قاعدة بيانات خارجية (Postgres) أو PVC + `replicas: 1` · تثبيت وسم الصورة على SHA | `k8s/deployment.yaml` | لا فقدان بيانات عند إعادة تشغيل pod |
| T3.4 | extras اختيارية (`infra`) وتثبيتها في Dockerfile صراحة | `pyproject.toml`, `Dockerfile` | الحاوية تُظهر الكاش/الذاكرة مفعّلين أو موثّقين كتدهور مقصود |
| T3.5 | إخراج `pytest` من requirements.txt | `requirements.txt` | بيئة إنتاج بلا أدوات اختبار |

### المرحلة 4 — حوكمة (مستمرة)
- **T4.1** فحص انحراف وثائقي آلي: أرقام/مسارات مذكورة في README يجب أن تُشتق آليًا أو تُحذف.
- **T4.2** تفعيل Dependabot لإصدارات `actions/*` وpip.
- **T4.3** مراجعة ربع سنوية: إعادة تشغيل هذا التدقيق كاملًا (السكربتات جاهزة).

### مؤشرات نجاح الخطة
1. pytest أخضر 100% + اختبار regression لكل خطأ P1.
2. bandit HIGH = 0 · ruff على كود الإنتاج < 200 · mypy = 0 على الملفات المرصودة · ثلاثة فاحصين داخل CI كبوابات.
3. `/metrics` حي ومقياس HPA قابل للتحقق · كل وثيقة نشر تطابق الكود.
4. صفر تحذيرات كاذبة من `skills validate` في الوضع الافتراضي.

### تقدير الجهد الكلي
| المرحلة | الجهد | الأولوية |
|---|---|---|
| 0 — إصلاحات فورية | ~1.5 ساعة | 🔴 ابدأ اليوم |
| 1 — أمن | 3–5 أيام | 🟠 هذا الأسبوع |
| 2 — جودة | 1.5 أسبوع | 🟡 الأسبوعان 2–3 |
| 3 — نشر | 2–4 أسابيع | 🟢 بالتوازي بعد المرحلة 1 |
| 4 — حوكمة | أيام موزعة | مستمرة |

---

## 9) سجل التنفيذ (يُحدَّث مع كل دفعة إصلاحات)

### الدفعة 1 — 2026-09-25: توحيد مصدر الحقيقة (P1-3 + P3-4 + P3-5)
| الحالة | البند | ما نُفِّذ |
|---|---|---|
| ✅ | P1-3 | `requires-python = ">=3.11"` (توحيد صريح بدل 3.10 المعلنة) — CI وDockerfile كانا على 3.11 أصلًا، لا job متبقٍ على 3.10 |
| ✅ | P3-4 | extra جديدة `infra = [redis, qdrant-client, numpy, prometheus-client]` + Dockerfile صار `pip install ".[infra]"` (يغطي Render تلقائيًا — `runtime: docker`) |
| ✅ | P3-5 | `pytest` خرج من `requirements.txt` إلى `[dev]` (مع ruff/mypy/bandit/types-PyYAML) |
| ✅ | P4 (تهيئة) | `[tool.ruff]` + `[tool.mypy]` داخل pyproject (target py311، select E/F/W/I/B/UP/S) |
| ✅ | تكيف CI | مهمتا `test` و`production-gate` → `pip install -e ".[dev,infra]"` (production_gate.sh يشغّل pytest)؛ `security-blocklist`/`zap`/`sandbox-escape` بقيت على requirements.txt (لا تحتاج pytest) |
| ✅ | وثائق | تحديث `docs/DEPLOY-RENDER-FREE.md` (وصف سلوك Dockerfile القديم) |
| ✅ | تحقق | 510/510 اختبارًا ينجح في الحالتين: `.[dev]` وحدها (إثبات مسارات fallback) و`.[dev,infra]` · falsifiability check على ci.yml · سكربتات التحقق الثلاثة PASS · `test_auth` (القارئ لـci.yml) أخضر |

**خط الأساس بعد الدفعة 1:** ruff بالتكوين الجديد = 618 ملاحظة (441 قابلة للإصلاح التلقائي) · mypy = 109 خطأ — كلاهما استشاري حتى الآن (لا بوابة CI عليهما بعد).

### الدفعة 2 — 2026-09-25: P1-1 + P1-2 + مسار أ (دفعة الـfix)
| الحالة | البند | ما نُفِّذ |
|---|---|---|
| ✅ | P1-1 | حارس `__main__` + `tests/test_main_no_side_effects.py` — دُوِّنت دورة أحمر (فشل الاستيراد بـSystemExit) → أخضر (2/2) |
| ✅ | P1-2 | إعادة تسمية بالدور: `PolicyVerdict` (حوكمة) · `PolicyOutcome` (نتيجة تقييم execution.policy) · `PolicyGateDecision` (بوابة tool_registry) — استيراد مؤهل في `execution/__init__`، `__all__` بلا ازدواج، صفر `import *`، وجسر `t5_policy_adapter` صُحح يدويًا (كان سيدمج الفئتين لولا القراءة البشرية) |
| ✅ | F811 | الصفر (كانت 6: التعارض + planner_swarm json/re + 3 ملفات اختبار) |
| ✅ | مسار أ | `ruff check --fix`: **502 إصلاحًا آليًا** عبر 77 ملفًا · المتبقي **174 في 23 فئة** غير قابلة للإصلاح الآلي (S-rules ×94، B904 ×22، F841 ×17، UP042 ×13…) |
| ✅ | حماية | `extend-exclude = ["docs"]` — أدلة T7.1 الموقعة بـMANIFEST.sha256 خارج نطاق اللايتر (تحقق بعد الدفعة: 0 اختلاف هاش) |
| ✅ | تحقق | **512/512** · arena-suite: 7 ran/0 error/0 regressions · re-exports مطابقة لـ`__all__` · سلامة `nimna serve` كما هي |

**بعد الدفعة 2:** ruff = 174 (كلها مرحلة 1/2: أمن S-rules + دليلية) · mypy = 109 (لم يُمس).

### الدفعة 3 — 2026-09-25: P2-1 منجَزة + بوابة Ruff Gate حية
| الحالة | البند | ما نُفِّذ |
|---|---|---|
| ✅ | P2-1 (جزء المرحلة 1) | **sha256 بدل md5 في 6 مواضع** (agent.py، computer.py، vision/cache.py + سطر التوثيق) — لا عقد خارجي مع md5 (تحقق: grep = 0، لا اختبارات تتحقق من قيمه). bandit: **High 6→0** |
| ✅ | P4 (بوابة T2.2) | `ruff check .` أخضر بالكامل (صفر) عبر قائمة ignore موسومة `TODO(phase1/phase2)` + إنذارات كاذبة موثقة بلا وسم (S105/S608). S324 **أُصلح** فلا وسم له |
| ✅ | بوابة CI | `.github/workflows/ruff-gate.yml`: ruff حاجب + bandit-advisory غير حاجب (`continue-on-error` — استثناء موثق من فاحص falsifiability داخل الملف نفسه، يُلغى عند إغلاق S301) |
| 🔍 | F841 (T2 مفتوح) | الـ17 مرصودة بترياج أولي: 6 «إسقاط إسناد مع إبقاء النداء» (منها validate وget_detector — حذف النداء كان سيعطّل التحقق ويفعّل `enabled:true` كاذبة)، 8 «تحويل إلى assert يحفظ النية» (legacy/ctx)، 2 «حذف نظيف»، 1 «قرار ملكية» (policy_stats — سلك ناقص: تمرير للـadapter أو حذف) |

**بعد الدفعة 3:** ruff gate = أخضر (صفر مرئي؛ الدين موثق بالوسوم) · bandit High = 0 · pytest = 512/512.

### الدفعة 4 — 2026-09-25: F841 أ+ب+ج (16 تنظيفًا) + تجربة د موقوفة بنتائج حرفية
| الحالة | البند | ما نُفِّذ / النتيجة |
|---|---|---|
| ✅ | فئة أ (6) | إسقاط الإسناد مع حفظ النداء: `tool.validate` (agents/base:156) · `get_detector()` (api/app:94) + **فحص سلوكي**: `/api/health` بعد التعديل يظهر `anomaly: {'enabled': True, 'detector': 'heuristics-v1'}` و`get_detector()` ينجح فعلًا (المصدر النداء لا قيمة افتراضية) · 4 نداءات اختبار عارية |
| ✅ | فئة ب (8) | 7 × `assert agent[s].tools.get("shell_execute") is not None` + `assert _ctx(agent) is not None` — بشرط PYTHONOPTIMIZE: grep = 0 ✓ |
| ✅ | فئة ج (2) | حذف `last_text` + حذف `has_retry` مع تعليق NOTE مانع للعودة |
| ⚠️ | حادثة أثناء التنفيذ | sed نمطي أصاب مواضع تستخدم المتغيرات فعلًا (42 × F821) — رُجِّع وأُعيد التنفيذ **بعنوان مزدوج (رقم سطر + نمط)**. الدرس: أنماط F841 ليست كل الاستخدامات |
| 🔬 | تجربة د — TEST-A | بوابة T5 + المحول **حيّان**: `invoke` مرفوض (`resource: system/etc`) → `POLICY_DENIED` و`stats = {'DENY': 1, 'ALLOW': 1}` |
| 🔬 | تجربة د — TEST-B | السويت **لا يحتوي أي استدعاء لـ`t5_policy_adapter`** (grep=0) — يعبر حصرًا عبر gateway (تعليق P1-T7). مع `SHELL_TOOL_ENABLED=1`: code-05 عبر فعلًا و**row["policy"] أظهر `allow=1`** (إحصاءات gateway حية ومعروضة). بلا الراية: المهام الحاملة لـverify (code-01/05) كلها SKIPPED → لا صف يحمل policy أصلًا |
| ⏸️ | قرار د | **موقوف بقاعدة التوقف** — النتيجة لا تطابق الفرعين المتوقعين: ليس «ميتًا في المصدر» (TEST-A أثبت العكس) ولا «قابلًا للتمرير» (لا استدعاء لتمريره إليه). `policy_stats` **يتيم هجرة T5→T7**. الخياران: (1) حذفه + توثيق أن إحصاءات البوابة الحية من gateway حصرًا — (2) إنشاء مسار T5 مباشر جديد في السويت (تغيير معماري). بانتظار القرار؛ السطر يحمل `# noqa: F841 # TODO(decision-D)` |

**بعد الدفعة 4:** ruff = أخضر · F841 خرج من ignore الشامل إلى noqa واحد موسوم · pytest = 512/512 · suite (مع الراية): 9 ran / 0 error / 0 regressions.
