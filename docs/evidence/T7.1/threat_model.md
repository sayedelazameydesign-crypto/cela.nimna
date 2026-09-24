# T7.1-B5 — نموذج تهديد الموافقة البصرية لـ `shell_execute` (بعد B1–B4)

> **الموافقة برمجية-بالحارس.** نطاقها: **لكل استدعاء افتراضياً**؛ وبمومس
> `ALWAYS` تمتد إلى **بقية الـ run الواحد بالاسم + مفتاح skill:version**
> (`state.approved_tools`، `agent.py:542-546` + `_approval_key:853` — لا تمتد
> لجلسة ولا run آخر). تُبطِلها: رفض المستخدم، قائمة الحجب المحتوى
> (**تسود على أي منح** — `rm -rf /` محجوب حتى مع ALWAYS، مُثبت
> `test_always_never_beats_the_denylist_DOCUMENTED`)، وتعطّل `DEFER`. تُستأنف بـ
> `resume(run_id, approved)` مع **ربط الهوية**: `_apply_decision` يعيد حساب
> digest الاستدعاء الذي سيُنفَّذ فعلاً ويقارنه بالمخزَّن عند التأجيل —
> عدم تطابق ⇒ `approval_binding_mismatch` + رفض **حتى لو وافق المستخدم**
> (`test_resume_with_modified_call_is_refused`).

## 1. أين يُفرض الحارس (كود لا نص)

- **إعلان الخطر:** `computer.py:620` — `risk="confirm"`.
- **الحارس:** `core/agent.py:521-526` — `PolicyEngine → APPROVAL_REQUIRED` ⇒
  `approval_policy.decide(state, tool, call)` لكل استدعاء؛ حدث
  `approval_requested` مدقَّط؛ `DEFER` يعلّق الدورة (`AWAITING_APPROVAL`) ويُخزَّن
  الـ pending في SQLite.
- **نص SKILL.md** (`code_execution:15,27,35`) = **توثيق** للحارس لا تنفيذه —
  الإنفاذ في الكود أعلاه. turn-scoping (عرض الأداة في الدورة) = طبقة
  **شرطية-بالسلوك** (قائمة `allowed_tools` في SKILL.md، تُفعَّل بتحميل المهارة).

## 2. B1 — `auto_approve`: مُبطِل كامل موثَّق لا مخفي

- الافتراضي: `config.py:138` `auto_approve: bool = False` + `:235`
  `AGENT_AUTO_APPROVE` default False + **قيمة وقت التشغيل**: `False` (أمر حي).
- `auto_approve=True` ⇒ الحارس يُتجاوز كلياً (المعالج يركض بلا أي
  `approval_requested`). الاختبار `test_auto_approve_bypasses_gate_DOCUMENTED`
  يثبت الخطر: `approved=True` + `EXECUTED!` من جساء المعالج + **صفر**
  `approval_requested`. **التسمية:** مفتاح مُشغِّل-الخطر؛ يظل مكشوفاً في
  نموذج التهديد دائماً.

## 3. B2 — `Decision.ALWAYS`: النطاق بالسلوك

- مُمنوح مرة ⇒ الاستدعاءات التالية **بنفس الاسم في نفس الـ run** بلا سؤال
  (`asked ONCE, ran TWICE` — `test_always_scope_is_rest_of_run_by_name_DOCUMENTED`).
- **لا يسود على قائمة الحجب** (الاختبار أعلاه).
- ينجو مع تعليق/استئناف نفس الـ run (RunState يُخزَّن كاملاً) — **تصميم مُسمّى**،
  لا ثغرة استئناف: النطاق run-محصور والاستئناف نفسه فعل مستخدم.

## 4. B3 — ربط الاستئناف بالهوية (تغيير core مُعلن)

- **قبل B3:** `_apply_decision` كان يقرأ الاستدعاء بالفهرس من رسائل الحالة
  **بلا مقارنة هوية** — استدعاء معدَّل بنفس الفهرس يرث الموافقة (ثغرة أعترف بها
  الكود، كشفها سيناريو المالك).
- **الإصلاح (ملتزم):** `PendingApproval.call_digest` (حقل إضافي، افتراضي ""
  — `core/state.py`) + `_call_digest()` + فحص في `_apply_decision` عند
  APPROVE/ALWAYS: عدم تطابق ⇒ `approval_binding_mismatch` + رفض + المعالج لا
  يُلمس. بلا flag تعطيل: ربطُ الهوية قابلٌ للتعطيل = ثغرة.
- الاختبارات: `test_resume_executes_exactly_the_approved_call` (نفس الوسائط
  بايتياً) و`test_resume_with_modified_call_is_refused` (عبث الحالة ⇒ رفض، جساء
  المعالج صفر).

## 5. B4 — صدق الـ harness: المِحرَض من الإنتاج

الجواب بالكود: **من `_execute_calls` الحقيقي.** المِحرَض استدعى `agent.run()`
(مدخل الإنتاج) فمرّ بـ `_drive → _execute_calls` الحقيقيتين؛ الحدثان
`approval_requested` و`tool_denied` يكتبهما **حصرياً** كود الإنتاج
(`agent.py:525-527` و`:538-539`) وقرأناهما من `agent.memory` الحقيقي
(MemoryStore فعلي). الشيء الوحيد المُعاقَب: دالة حد النظام
`nimna.tools.builtin.computer._run_with_limits` (spawn العمليات) — لا استنساخ
لأي منطق حارس. `handler executions: 1` إذن = نداء حقيقي من سلسلة الإنتاج،
مُثبت بأن الـ sabotage اشتعل داخل معالج الأداة الحقيقي عبر `_run_tool` الحقيقية.

## 6. سطح الهجوم المتبقي (مُسمّى لا مخفي)

1. `auto_approve=True` (تشغيلي-بيئي): يُبطل الحارس — افتراضياً False.
2. `ALWAYS`: يخفف «لكل استدعاء» إلى «لبقية الـ run» — تصميم مُسمّى.
3. **مسار `shell_execute` (حزمة computer/VNC) — سطح هجوم متبقٍّ بلا unit test،
   والحماية الآلية له argv/استضافة-المستوى فقط:**
   - النقل الحي (`computer.py:638-646`):
     `docker exec desktop bash -lc 'ulimit -t 30; ulimit -v 524288; …; timeout
     N bash -lc ''<command>'''` — أي أن التعزيز «داخل الحاوية» هو **سلسلة
     نصية داخل bash** (ulimits + timeout)، وليس إضافة في argv الـ `docker
     exec` نفسه؛ وحاوية desktop تُنشأ مسبقاً بـ `docker compose --profile
     computer` خارج كل ما يختبره هذا المستودع.
   - **بلا سقف انحدار آلي:** لا unit test يمسك سلسلة ulimit، ولا تعزيزاً في
     argv الـ exec، ولا شيئاً داخل الحاوية — أي انحدار هناك لا يُمسك اختباراً.
   - الحماية الآلية المثبتة لهذا المسار **قبل/حول** التنفيذ فقط: denylist
     قبل عرض الموافقة + turn-scoping + بوابة الموافقة (T7.1-B) + killpg من
     المضيف بعد `timeout+3` (`_run_with_limits`).
   - **دخان E لا يغطي هذا المسار إطلاقاً** — E لحاوية python-sandbox في
     `tools/sandbox.py` (`docker run --network none --cap-drop ALL …`)؛
     الخلط بين الحاويتين قراءة خاطئة لـ E (ترويسة E النطاقية تصرّح بذلك).
4. قائمة الحجب regex/نصية = **تخفيف لا بديل** — الحماية البنيوية = عزل الحاوية
   + Fabric عند الربط.
5. سلامة مخزن الحالة (SQLite) تصير **ضمن سطح الهجوم** بعد B3: ربط الهوية يكشف
   العبث لكنه لا يمنع كتابة الحالة أصلاً — حماية المخزن نفسه خارج نطاق T7.1
   (تُرفع لتذكرة لاحقة).

## 7. إضافات ع2–ع4 (جولة المالك)

- **ع2 — fail-closed للسجل القديم:** سجل `pending` مخزَّن قبل B3 (بدون
  `call_digest`) **لا يثبت ماذا وافق عليه** ⇒ يُرفض عند الاستئناف مهما وافق
  المستخدم (حدث `approval_binding_missing`) — `test_legacy_pending_approval_
  without_digest_is_refused`. لا مسار fail-open.
- **ع3 — الاسم الدقيق لنطاق ALWAYS:** «**بقية عمر RunState المخزَّن، بما في
  ذلك عبر الاستئناف**» — مثبت: مُنح مرة ⇒ استئناف قام به **وكيل آخر** (حالة
  مُحمَّلة من المخزن) ⇒ استدعاء لاحق بنفس الاسم نُفِّذ **بلا سؤال جديد**
  (`test_always_scope_spans_resume_via_persisted_run_state`).
- **ع4 — القرار المكتوب:** «**لا flag تعطيل لربط الهوية؛ إبطاله = الثغرة**».
  (يرقى سطر الشجرة `if decision in (APPROVE, ALWAYS):` — لا شرطdigest فارغ.)
