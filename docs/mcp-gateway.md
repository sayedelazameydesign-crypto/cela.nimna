# MCP Gateway في Nimna

> **حالة التكامل:** طبقة عقود + حدود + سياسة في `nimna/mcp/` مع اختبارات offline
> حتمية (`tests/test_mcp_gateway.py`). **غير موصولة بحلقة الوكيل بعد**، ومعطّلة
> افتراضياً (`MCP_ENABLED=false`). الصف في `docs/CAPABILITY-MATRIX.md` يبقى
> `partial` ولا يدّعي `implemented`.
>
> **المراجعة:** 2026-09-23 مقابل مواصفة MCP إصدار `2026-07-28`.

## الإصدار المستهدف والقرار المهم

الإصدار الحالي من المواصفة هو **`2026-07-28`**، وهو **stateless**. هذا يخالف
معظم الدروس والمقالات المنتشرة التي تفترض `initialize` و`Mcp-Session-Id`:

| آلية | 2025-03-26 … 2025-11-25 | 2026-07-28 (المستهدف) |
|---|---|---|
| `initialize` + `notifications/initialized` | مطلوب | **محذوف** — كل طلب يحمل نسخته وقدراته في `_meta` |
| `Mcp-Session-Id` | جلسة على مستوى الاتصال | **محذوف** |
| `server/discover` | غير موجود | **إلزامي** على الخادم |
| `Last-Event-ID` / إعادة الإرسال | مدعوم | **محذوف** — stream مقطوع يعني طلباً مفقوداً |
| `resources/subscribe` | موجود | مُستبدل بـ `subscriptions/listen` |
| `ping`, `logging/setLevel` | موجود | **محذوف** (مستوى السجل عبر `_meta`) |
| `resultType` في النتائج | غير موجود | **مطلوب** (`complete` / `input_required`) |
| طلبات من الخادم (sampling/roots/elicitation) | عبر stream | مُستبدلة بـ MRTR داخل النتيجة |

لذلك لا يوجد في `nimna/mcp/contract.py` أي ثابت لـ`initialize` أو
`Mcp-Session-Id`، و`REMOVED_METHODS` يسمّي المحذوفات ليصبح سبب الرفض واضحاً بدل
«method غير معروف».

`MCP_SUPPORTED_PROTOCOL_VERSIONS` يساوي `("2026-07-28",)` فقط — لا يُتفاوض للأسفل
إلى إصدار يملك handshake غير مُنفَّذ. خادم يتحدث إصدارات أقدم فقط يُصنَّف
`unsupported` صراحةً.

## العناوين المطلوبة على كل POST

| الترويسة | المصدر | متى |
|---|---|---|
| `MCP-Protocol-Version` | `_meta.io.modelcontextprotocol/protocolVersion` | كل طلب |
| `Mcp-Method` | `method` | كل طلب |
| `Mcp-Name` | `params.name` أو `params.uri` | `tools/call`, `resources/read`, `prompts/get` |
| `Mcp-Param-{Name}` | معامل مُعلَّم بـ`x-mcp-header` | عند وجود قيمة |
| `Accept` | — | `application/json, text/event-stream` |

**قاعدة الأمان المركزية:** الترويسات مرآة للـbody، والاتفاق بينهما شرط لا رفاهية.
اختلاف الترويسة عن الـbody يعني أن وسيطاً قد يوجّه بناءً على قيمة بينما ينفّذ
الخادم قيمة أخرى. لذلك:

- `check_header_body_agreement()` ترفض إرسال طلب تختلف ترويساته عن body‑ه
  **محلياً**، بدل ترك الخادم يكتشفها.
- `assert_routable_version()` ترفض تطبيق أي سياسة مبنية على الترويسات إذا كان
  الإصدار أقدم من `2025-06-18` أو غائباً — لأن تلك الإصدارات لم تكن تُلزم أصلاً
  بمطابقة الترويسة للـbody، فالترويسة فيها ليست مصدراً موثوقاً.

## ترميز القيم

القيمة التي تصلح كنص ASCII عادي تُرسل كما هي. غير ذلك — non-ASCII، محارف تحكم،
مسافات في الطرفين، أو قيمة تشبه `=?base64?...?=` نفسها — تُرمَّز Base64 بعلامة
`=?base64?{value}?=`. هذا يجعل حقن `CRLF` عبر ترويسة مستحيلاً بالبناء: النص
`"evil\r\nX-Injected: 1"` يُرمَّز ولا يصل أبداً كسطر ترويسة.

> اختيار موثّق: النص الفارغ `""` يُرمَّز أيضاً، ليظل «القيمة فارغة» مميزاً عن
> «الترويسة غائبة» بعد أي وسيط يحذف الترويسات الفارغة.

## x-mcp-header: متى يُرفض تعريف الأداة

المواصفة تلزم العميل **برفض** تعريف الأداة (أي استبعادها من `tools/list`) إذا خالف
وسم `x-mcp-header` أي قيد. `header_params_from_schema()` يفرض:

- النوع `string` أو `integer` أو `boolean` فقط — `number` ممنوع.
- الاسم غير فارغ ومطابق لصيغة `1*tchar` (RFC 9110).
- الاسم فريد بغض النظر عن حالة الأحرف.
- المسار من الجذر عبر مفاتيح `properties` فقط — لا عبر `items` أو `oneOf`/
  `anyOf`/`allOf`/`not` أو `if`/`then`/`else` أو `$ref`.
- الأعداد الصحيحة داخل نطاق JavaScript الآمن (`±(2^53 − 1)`).

الفحص يمشي على **كل** الشجرة لا على المسارات القابلة للوصول فقط: وسم مدفون تحت
`items` مخالفة، لا أمر يُتجاهل. أداة واحدة فاسدة لا تُسقط بقية الأدوات —
`filter_tool_definitions()` يستبعدها ويُسجّل السبب في التدقيق.

## التحقق من الأمان

- **SSRF:** كل نقطة نهاية تُفحص عبر `assert_public_url()` قبل أول طلب، وتُعاد
  المراجعة في **كل** نداء — اسم حلّ لعنوان عام عند التسجيل قد يحلّ لعنوان خاص
  لاحقاً (DNS rebinding). الاختبار `test_reachability_is_rechecked_on_every_call`
  يثبّت هذا السلوك.
- **المصادقة:** خادم يعلن حاجته لمفتاح دون وجود متغير بيئته يفشل **عند البناء**،
  لا عند أول أداة. `MCPCredential` لا يُظهر سره في `repr`/`str`، و`fingerprint()`
  يعطي معرّفاً غير عكوس للتدقيق.
- **الأسرار:** لا تُسجَّل الترويسات الخام؛ `describe_credential()` و
  `redact_headers()` هما المساران الوحيدان، والمخزن نفسه يعيد التنقيح.
- **الحجم:** سقف على جسم الاستجابة وstream (`MCP_MAX_RESPONSE_BYTES`).
- **stdio غير مدعوم:** `ALLOWED_TRANSPORTS = {"streamable_http"}` فقط. نقل stdio
  يعني تشغيل subprocess، وهذا المستودع صريح أن subprocess ليس حدّاً أمنياً —
  فاستُبعد بدل توريثه.

## الإعدادات

```text
MCP_ENABLED=false                 # لا اتصال بأي خادم قبل التفعيل
MCP_SERVERS=                      # أسماء خوادم من البيئة
MCP_MAX_RESPONSE_BYTES=8388608
MCP_ALLOW_PRIVATE_NETWORKS=false  # يخالف الافتراضي فقط عند اختبار محلي مقصود
```

## سلوك الفشل

`call_tool()` لا يرفع استثناءً لفشل متوقع؛ يعيد `ToolCallOutcome` بحالة واحدة من
`ok` / `denied` / `approval_required` / `error`، مثل `ToolRegistry.execute`.
كل قرار — بما فيه الرفض — يُسجَّل كحدث تدقيق (`mcp.tool_call`, `mcp.tool_result`,
`mcp.denied`, `mcp.error`, `mcp.tool_definition_rejected`).

حالات صريحة بدل سلوك مرجَّح:

| الحالة | النتيجة |
|---|---|
| `202` على طلب (لا إشعار) | رفض — `202` مشروع للإشعارات فقط |
| `400` مع خطأ JSON-RPC معروف | يُعاد كخطأ — الخادم حديث، لا تراجع إلى `initialize` |
| `400` بجسم فارغ/غير معروف | `MCPLegacyServerError` — إصدارات `initialize` غير منفَّذة |
| `404` مع `-32601` | خطأ JSON-RPC عادي — الطريقة غير مدعومة |
| `404` بلا جسم | `MCPLegacyServerError` — الرابط ليس نقطة MCP حديثة |
| stream ينتهي بلا رد | `MCPStreamTruncatedError` — الطلب مفقود، أعد إرساله بمعرّف جديد |
| نتيجة `input_required` | `approval_required` — البوابة **لا تجيب نيابة عن المستخدم** |

## ما لم يُنفَّذ بعد (صراحة)

- **الربط بحلقة الوكيل**: الأدوات البعيدة لا تُسجَّل في `default_registry()`،
  فالنموذج لا يراها ولا يستدعيها اليوم. `capability_scope()` تنتج الأسماء
  المؤهَّلة (`mcp__{server}__{tool}`) الجاهزة لذلك الربط.
- **إصدارات `initialize`**: غير منفَّذة، وتُرفض صراحةً.
- **`subscriptions/listen`**: الثابت موجود، ولم تُنفَّذ إدارة stream طويل الأمد.
- **اختبار تكامل مع خادم MCP حقيقي**: غير موجود؛ كل الاختبارات عبر
  `httpx.MockTransport`. الحالة الصحيحة لذلك `MOCKED` لا `PASS production`.

## Definition of Done مقابل هذه الحالة

| الشرط | الحالة |
|---|---|
| code exists | ✅ `nimna/mcp/` |
| scoped boundary | ✅ `headers.py` (اتفاق الترويسة/الـbody) + `gateway.py` (نطاق وسياسة ومصادقة) |
| deterministic offline test | ✅ 85 اختباراً بلا شبكة |
| integration/contract test where external | ❌ لا خادم حقيقي — `MOCKED` |
| policy and failure behavior | ✅ `PolicyEngine` + جدول الفشل أعلاه |
| audit/evidence event | ✅ أحداث `mcp.*` عبر `EvidenceJournal` |
| capability row and verification row | ✅ `G14` في `docs/VERIFICATION-MATRIX.md` |

لذلك الصف `partial` لا `implemented`.
