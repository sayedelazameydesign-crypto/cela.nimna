# T7.1-B — بوابة الربط البرمجية: الأدلة (B1–B5 + ع2–ع4)

**الالتزامات:** B الأساس + B3 core-change = `b50783e4d611ceb82d9ca3558466c106d1181a5a`
(ملاحظة: خالط وثيقة A — المُستهجَن؛ A أُعيدت مستقلة في `af667ef`).
**متابعة B (ع2/ع3/ع4) = `dee9cfa`.** الاختبارات العشرين لملف
`tests/test_binding_migration.py` (sha256 للمحتوى النهائي في MANIFEST.sha256).

## المخرج الحي
```
collected 20 items
tests/test_binding_migration.py ....................                     [100%]
20 passed in 1.83s
```

## البنود → الاختبار القاتل → الدليل
| بند | ما ثبت | الاختبار/الدليل |
|---|---|---|
| **B1** | `auto_approve` افتراضي False (config+env+runtime)؛ تشغيله **يتخطى البوابة فعلاً** — القلب الخطِر مُسمّى لا مُخفى: `approved=True` + `EXECUTED!` في record + **صفر** `approval_requested` | `test_auto_approve_bypasses_gate_DOCUMENTED` |
| **B2** | نطاق ALWAYS سلوكياً: **بقية عمر RunState المخزَّن بما فيه عبر الاستئناف** — asked ONCE ran TWICE بالاسم | `test_always_scope_is_rest_of_run_by_name_DOCUMENTED` |
| **B2** | denylist يسود على ALWAYS: `rm -rf /` محجوب رغم منح ALWAYS | `test_always_never_beats_the_denylist_DOCUMENTED` |
| **B2/ع3** | النطاق الدقيق عبر الاستئناف: وكيل **ثانٍ** (حالة من مخزن مشترك) يستأنف والاستدعاء التالي بنفس الاسم يُنفَّذ **بلا سؤال جديد** (approval_requested=2 إجمالاً) | `test_always_scope_spans_resume_via_persisted_run_state` |
| **B3** | تغيير core مُعلن: `PendingApproval.call_digest` = sha256(json{tool,arguments sorted})؛ الفحص في `_apply_decision` على **الاستدعاء الذي سيُنفَّذ فعلاً** (`assistant.tool_calls[pending_call_index]`)؛ mismatch ⇒ `approval_binding_mismatch` + رفض | `test_resume_executes_exactly_the_approved_call`، `test_resume_with_modified_call_is_refused` + اختبار عبث |
| **B3/ع2** | سجل pending **قديم** (بلا digest، `""` من pydantic) ⇒ **fail-closed**: رفض + `approval_binding_missing` + صفر spawn — لا fail-open مموَّه | `test_legacy_pending_approval_without_digest_is_refused` |
| **B3/ع4** | **لا flag تعطيل** لربط الهوية — إبطاله = الثغرة (مكتوب في threat_model.md §7) | threat_model.md |
| **B4** | جواب بكود: القياس من **سلسلة الإنتاج** (`agent.run→_drive→_execute_calls`)، mock وحيد `_run_with_limits`، أحداث audit من MemoryStore حقيقي ⇒ `executions: 1` | اختبار B4 في الملف |
| **B5** | `threat_model.md`: «نطاقها X، تُبطِلها Y، تُستأنف بـ Z» + سطح الهجوم المتبقي مُسمّى | threat_model.md |

## عقد fail-closed (شجرة `_run_tool` الحية — مذكورة في X03/X05)
- gateway مُربوط + أداة غير مُسجَّلة ⇒ `NOT_IN_GATEWAY` بلا معالج.
- فشل gateway ⇒ `GATEWAY_ERROR` **بلا** fallback.
- gateway=None ⇒ legacy **مُعلَن** (`gateway_compat`-style audit حيث ينطبق).
