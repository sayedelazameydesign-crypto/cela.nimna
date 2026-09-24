# T7.1-F — تعداد مواقع ربط gateway (حتمي + توجيه ثلاثي إلزامي)

**السكربت:** `scripts/audit/enumerate_bindings.py` · **المخرج المُخزَّن كدليل:**
`F_bindings.json` — **sha256:** `af2a4a183c86dcad9d8f6a9dac54a910bb1c63e8a33a0a961da7868b0f77f8ac`

## التوجيه الثلاثي عند الربط (إلزامي — آلي لا كلامي)
كل سطر في كود الإنتاج يلمس `execution_gateway` / `gateway.has` /
`gateway.invoke_for_agent` / `compat_tools` / أحداث التوجيه الأربعة إما:
1. **مُدرَج** في القاعدة الذهبية **10 ربط + 8 تنفيذ** (B01–B10, X01–X08)، أو
2. **استثناء موثَّق بسبب** (EXCEPTIONS في السكربت — سبب مكتوب إلزامي)،
3. وإلا فهو **UNTRIAGED ⇒ السكربت يفشل (exit 2)** — التعداد بلا توجيه ناقص.

عكسياً: موقع ذهبي اختفى من الكود ⇒ **exit 3** (انحدار ربط يُكشف).

## الحصيلة الحية
```
binding: 10  execution: 8  untriaged: 0  missing: 0  exceptions: 2   exit=0
```
- الاستثناءان الموثَّقان (كلاهما في `nimna/execution/gateway.py`): إعلان حقل
  `compat_tools` وإسناده في المُنشئ — **نقطة إعلان القائمة الصريحة** لا موقع
  ربط ولا مسار تنفيذ؛ تعديلها يغيّر توجيه X04/X08 ويُراجع معهما (السبب مكتوب
  داخل المخرج نفسه).
- المخرج **حتمي**: تشغيلان متتاليان متطابقان بايت-ببايت (`diff` فارغ)، بلا
  طوابع زمن، بلا بيئة؛ `generated_from_commit` يربط الدليل بالتزام.

## إثبات أن البوابة تنطق (طفرة محكومة ثم استرجاع — مخرج حي)
```
mutation-untriaged exit=2   # سطر gateway جديد بلا توجيه
ENUMERATION INCOMPLETE: untriaged=1 missing=0
mutation-missing   exit=3   # موقع ذهبي حُرّف
ENUMERATION INCOMPLETE: untriaged=1 missing=1
DETERMINISTIC: identical    # diff بين تشغيلين فارغ
```

## القائمة (من F_bindings.json)
| ID | kind | الملف | الأسطر الحية |
|---|---|---|---|
| B01 | binding | core/agent.py | ctor param |
| B02 | binding | core/agent.py | ctor assign |
| B03 | binding | core/agent.py | `_invoke_via_gateway` read |
| B04 | binding | core/agent.py | `_run_tool` read |
| B05 | binding | core/agent.py | `gateway is None` → legacy مُعلَن |
| B06–B08 | binding | agents/base.py | ctor param/assign + swarm dispatch read |
| B09 | binding | bootstrap.py | param + التمرير (سطران) |
| B10 | binding | core/planner_swarm.py | انتشار parent→child |
| X01 | execution | core/agent.py | `invoke_for_agent` (المسار الحاكم الوحيد) |
| X02 | execution | core/agent.py | `gateway.has` → fabric |
| X03 | execution | core/agent.py | GATEWAY_ERROR fail-closed (audit+payload) |
| X04 | execution | core/agent.py | compat مُعلَن (audit `gateway_compat`+legacy) |
| X05 | execution | core/agent.py | NOT_IN_GATEWAY refusal (audit+payload) |
| X06–X08 | execution | agents/base.py | swarm: has→invoke + refusal |

## نسخة البوابة
`F_bindings.py` في هذا المجلد **نسخة حرفية** (byte-for-byte) من الأصل الكنسي
`scripts/audit/enumerate_bindings.py` — تجزئة الاثنين في MANIFEST.sha256 تكشف
أي انحراف بينهما.
