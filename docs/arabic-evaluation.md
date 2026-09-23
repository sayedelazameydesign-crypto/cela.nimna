# تقييم عربي لحوكمة الأدوات — Arabic Evaluation Suite

> **ما الذي يقيسه هذا التقرير وما لا يقيسه.**
> يقيس **مسارات الحوكمة** (النطاق، السياسة، الموافقة، التدقيق، والتعامل مع الفشل)
> عندما يكون المستخدم والأدوات وسجل التدقيق بالعربية. **لا يقيس جودة نموذج
> لغوي** ولا قدرته على الاستدلال بالعربية: كل اختبار هنا يعمل على مزوّد وهمي
> (`MockProvider`) بلا شبكة، فلا شيء في هذا الملف يقول شيئاً عن أداء نموذج حقيقي.

| البند | القيمة |
|---|---|
| ملفات الاختبار | `tests/test_arabic_evaluation.py` (25)، `tests/test_mcp_security_invariants.py` (18) |
| الإجمالي | **239 اختباراً في المستودع** (196 قبل هذه الحزمة + 43 جديداً) |
| الشبكة | لا شيء يخرج إلى الإنترنت؛ خادم MCP حقيقي على `127.0.0.1` عبر مقبس فعلي |
| مزوّد النموذج | `MockProvider` — لا استدعاء لنموذج خارجي |

## أوامر التحقق

```bash
.venv/bin/python -m pytest                                        # 239 اختباراً
.venv/bin/python -m pytest tests/test_arabic_evaluation.py        # 25 اختباراً
.venv/bin/python -m pytest tests/test_mcp_security_invariants.py  # 18 اختباراً
.venv/bin/python scripts/verify_capabilities.py                   # integrity PASS
.venv/bin/python -m compileall -q nimna security                  # PASS
```

بوابات `security-blocklist` و`sandbox-escape` تعمل في CI ضمن `.github/workflows`،
وتُنفَّذ على نفس الالتزام عبر `gh pr checks`.

## أولاً: تغطية المسارات الثمانية

كل صف يربط المسار المطلوب باختبار مُسمّى وبالبوابة (Gate) أو صف مصفوفة القدرات
الذي يدعمه. الأسماء الإنجليزية مقصودة: التقرير يجب أن يكون قابلاً للبحث في مخرجات CI.

| # | المسار | الاختبار | البوابة / الصف |
|---|---|---|---|
| A | أداة محلية آمنة | `test_a_arabic_safe_local_tool_runs_without_approval` | `G1` حلقة الوكيل — لا طلب موافقة |
| A | اسم ملف عربي عبر الأداة | `test_a_arabic_filename_survives_the_tool_round_trip` | `G1` + صف «Typed tool arguments» |
| B | أداة تتطلب موافقة | `test_b_arabic_approval_required_tool_defers_then_runs` | `G2` الموافقة — إيقاف ثم استئناف |
| C | أداة MCP بعيدة | `test_c_arabic_remote_mcp_tool_full_governed_path` | **`G19` / `G20`** + `G17` |
| C | اسم أداة عربي على السلك | `test_c_arabic_tool_name_is_escaped_locally_but_intact_on_the_wire` | `G15` ترويسات + `G19` |
| C | تعليق `x-mcp-header` غير صالح | `test_c_invalid_arabic_header_annotation_excludes_only_that_tool` | `G15` — يُستبعد ولا يصل للنموذج |
| C | قيمة عربية وترويسة ASCII | `test_c_arabic_x_mcp_header_annotation_uses_ascii_header_name` | `G15` + `G19` |
| D | رفض بسبب النطاق (المهارة) | `test_d_arabic_scope_denial_without_an_mcp_skill` | `G17` الحوكمة |
| D | نمط glob عربي | `test_d_arabic_glob_pattern_matches_an_arabic_tool` | `G20` توسيع `allowed_tools` |
| D | نطاق مُعلن من المشغّل | `test_d_scope_denial_at_the_server_level_is_reported` | `G17` |
| E | رفض بسبب السياسة | `test_e_arabic_policy_denial_stops_a_remote_tool` | `G17` — لا يصل إلى الموافقة |
| F | رفض المستخدم | `test_f_arabic_user_denial_blocks_the_tool` | `G2` — الأداة لا تُنفَّذ |
| G | فشل الاتصال | `test_g_connection_failure_is_reported_not_raised` | `G19` — رفض مُبلَّغ لا استثناء |
| G | استجابة MCP غير صالحة (6 حالات) | `test_g_invalid_mcp_responses_are_refused` | `G16` / `G19` |
| G | بث SSE مقطوع | `test_g_truncated_sse_stream_is_refused_not_retried` | `G16` — لا استئناف |
| H | إشعارات مرتبطة بـ `run_id` | `test_h_arabic_run_notifications_are_tied_to_the_run_id` | `G17` + `G19` |
| — | اسم خادم ASCII (قرار تصميم) | `test_server_name_is_ascii_operator_configuration` | توثيق الحدّ |

### الحالات الست غير الصالحة في `test_g_invalid_mcp_responses_are_refused`

| الحالة | لماذا يجب أن تُرفض |
|---|---|
| جسم ليس JSON | لا يمكن تأكيد أنه استجابة صالحة |
| معرّف الاستجابة لا يطابق الطلب | استجابة لطلب آخر — خطر إسناد خاطئ |
| لا `result` ولا `error` | استجابة بلا معنى يجب ألا تُقرأ كنجاح |
| `resultType` غير معروف | عقد الإصدار `2026-07-28` يوجب `resultType` معروفاً |
| `Content-Type` غير مدعوم | ربط النقل يفرض `application/json` |
| `202` لطلب | `202` مخصص للإشعارات فقط |

## ثانياً: الواقعية العربية

| المتطلب | أين تحقّق |
|---|---|
| أوامر ووصف أدوات بالعربية | `test_arabic_tool_description_and_parameter_names_reach_the_model` — الوصف «الطقس» وأسماء المعاملات «مدينة/أيام» تصل للنموذج |
| أسماء أدوات وأنماط glob بـ Unicode | `nimna/mcp/naming.py` + `test_c_arabic_tool_name_is_escaped_locally_but_intact_on_the_wire` و`test_d_arabic_glob_pattern_matches_an_arabic_tool` |
| رسائل الموافقة والرفض بالعربية | `test_b_…` (ملخص الطلب يذكر اسم التقرير العربي) و`test_f_…` (`approval_resolved.decision == "deny"`) |
| سجلات تدقيق مقروءة وقابلة للتحقق | `test_arabic_audit_log_is_readable_and_verifiable` — الطلب العربي مخزّن حرفياً + `verify_audit_chain` + JSON صالح |
| خلط عربي/إنجليزي/أرقام | `test_arabic_mixed_script_request_and_arguments` — «القاهرة الجديدة 5» و3 أيام |

### قرار تصميم: أسماء الأدوات مقابل اسم الخادم

**أسماء الأدوات** تأتي من الخادم، وهي حرة تماماً في MCP، فتُدعم بالكامل. لكن
`mcp__{server}__{tool}` يُعرض على النموذج، وواجهات استدعاء الدوال تقبل عادةً
`[A-Za-z0-9_-]{1,64}`. لذلك يُرمَّز الاسم البعيد إلى عنصر محلي آمن
(`طقس` → `~00c7~00d3`، بترميز base36 بعرض ثابت)، ويبقى **الاسم الحقيقي هو ما
يُرسل على السلك**؛ الترميز لا يُستخدم أبداً لإعادة بناء الاسم عند الاستدعاء،
بل يُغلق عليه في `build_tool`. النتيجة: النموذج لا يرى اسماً ترفضه واجهته،
والخادم لا يرى اسماً لم يعلنه.

**اسم الخادم** إعداد مشغّل لا مدخل بعيد، فيبقى ASCII قصيراً (`^[a-z0-9][a-z0-9_-]{0,23}$`)
لأن ترميزه سيجعل كل نطاق وسطر سجل غير مقروء. هذا حدّ مقصود ومُختبَر في
`test_server_name_is_ascii_operator_configuration`.

الأرقام العربية الهندية مدعومة: `أداة٢٠٢٦` يُرمَّز ويُستعاد كما هو.

## ثالثاً: الثوابت الأمنية المقفلة

كل ثابت له اختبار يفشل عند إزالة الضمان — وقد أُثبت ذلك بطفرة (mutation) فعلية.

| الثابت | الاختبار | إثبات الفشل |
|---|---|---|
| الأدوات البعيدة `risk="confirm"` دائماً | `test_remote_tool_registration_is_forced_to_confirm` | — |
| لا تجاوز عبر `explicit_consent` | `test_handler_called_directly_cannot_grant_itself_consent` + `test_agent_marks_the_tool_approved_only_at_execution` | استدعاء الـhandler مباشرةً ⇒ رفض ولا طلب على الشبكة (مُتحقَّق بالطفرة) |
| إجبار `confirm` عند نقطة التنفيذ | `test_agent_refuses_a_remote_tool_that_is_registered_safe` | حذف `MCP_TAG in tool.tags` من `agent.py` ⇒ **فشل الاختبار** (مُتحقَّق) |
| كل استدعاء يمر بالنطاق ثم السياسة ثم الموافقة ثم التدقيق | `test_scope_policy_and_approval_each_precede_execution` | في كل مرحلة: `tools/call` غائب عن الخادم |
| كل إشعار يحمل `run_id` | `test_sse_notifications_are_recorded_with_their_run_id` | إزالة تمرير `run_id` من `gateway.py` ⇒ **فشل الاختبار** (مُتحقَّق) |
| لا مسار CLI/API يتجاوز `build_agent` | `test_api_routes_through_build_agent`, `test_no_transport_module_constructs_an_agent_directly`, `test_cli_uses_build_agent` | فحص المصدر + تجسّس على `build_agent` |
| فشل الترويسة ⇒ رفض حقيقي من الخادم | `test_the_wire_never_carries_the_escaped_local_name` + `G15` | خادم المقبس الحقيقي يفحص تطابق الترويسة مع الجسم |

**لماذا الإجبار عند نقطة التنفيذ مهم:** المعالج المحكوم يمرر
`explicit_consent=True` لأنه لا يُستدعى إلا بعد موافقة الوكيل. لو سُجّلت أداة
بعيدة كـ `safe`، لتحوّل ذلك التأكيد إلى باب خلفي — لذلك يُعاد رفع أي أداة تحمل
`MCP_TAG` إلى `confirm` في `agent.py` عند قرار التنفيذ، وليس في التسجيل فقط.

**والموافقة مُثبتة لا مُفترضة:** الوكيل يسجّل الأداة في سجلّ الموافقة داخل
`_run_tool` (القمع الوحيد لكل تنفيذ، ومسار الاستئناف يمر منه)، والـhandler
يرفض أي استدعاء لا يجد فيه الأداة — فلا يفيد استدعاء الـhandler مباشرةً ولا
تمرير `explicit_consent` كوسيط. نصفا الآلية مقفلان باختبارين يفشل كل منهما عند
إزالة النصف المقابل.

## رابعاً: القيود المعلنة (ما لا يُدّعى)

* **`G18` يبقى `BLOCKED`.** لم يُجرَّب أي خادم MCP من طرف ثالث. الخادم المستخدم
  في الاختبارات (`tests/mcp_mock_server.py`) **خادم محلي داخل المستودع** يعمل
  على مقبس loopback، وليس خدمة خارجية ولا يُدَّعى أنه كذلك.
* التقييم على مزوّد وهمي: لا استدلال نموذج حقيقي بالعربية، ولا قياس لأي جودة لغوية.
* `validate_arguments` المحلي جزئي عن قصد (وجود المطلوب، ورفض المعاملات غير
  المعروفة فقط عند `additionalProperties: false`)؛ **الخادم هو المرجع** في التحقق.
* أمن الشبكة: طلب loopback مرفوض افتراضياً بحراسة SSRF، والاختبارات تفتحه صراحةً
  كما يفعل المشغّل لخادم محلي.

## سجل البوابات

| البوابة | الأمر | الحالة |
|---|---|---|
| `G19` تكامل MCP | `pytest -q tests/test_mcp_integration.py` | PASS — 23 اختباراً |
| `G20` حلقة الوكيل | `pytest -q tests/test_mcp_integration.py -k "agent"` | PASS — 4 اختبارات |
| `G21` التقييم العربي | `pytest -q tests/test_arabic_evaluation.py` | PASS — 25 اختباراً |
| `G22` الثوابت الأمنية | `pytest -q tests/test_mcp_security_invariants.py` | PASS — 18 اختباراً |
| `G18` خادم طرف ثالث حيّ | — | **BLOCKED** (يتطلب `MCP_ENABLED=true` + خادم + اعتماد) |
