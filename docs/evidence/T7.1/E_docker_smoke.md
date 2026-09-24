# T7.1-E — دخان Docker argv (جهة المضيف)

> **ترويسة النطاق (إلزامية):** هذا الدخان **لا يغطي ما يحدث داخل الحاوية**.
> يثبت أن **أمر التشغيل من جهة المضيف** يحمل التثبيتات الأربعة وtimeout.
> الحد الأمني الفعلي داخل الحاوية (kernel namespaces / cgroups / dockerd)
> **بلا سقف انحدار آلي** في هذا المستودع: من يعدّل argv يُمسك، ومن يعدّل
> الصورة أو الـ daemon لا يُمسك آلياً.

**الاختبار:** `tests/test_docker_argv_smoke.py` — mock الوحيد = `subprocess.run`
(تقاطيع في `nimna.tools.sandbox`)؛ **المسار إنتاجي حقيقي**:
`run_python_code(backend=docker) → run_in_docker → subprocess.run(cmd, **kw)`.

## أين يُفرض كل عنصر (من مخرج حي)
| العنصر | القيمة | مكان الفرض | كيف يقرؤه الاختبار |
|---|---|---|---|
| شبكة | `none` | **argv** (بعد `--network`) | `cmd[i+1] == "none"` |
| موارد | `--memory {N}m`, `--cpus 1`, `--pids-limit 128` | **argv** | قراءة القيم بجوار المفتاح |
| امتيازات | `--cap-drop ALL`, `--security-opt no-new-privileges` | **argv** | كذلك |
| جذر FS | `-v {ws}:/work`, `-w /work` | **argv** | كذلك |
| timeout | `sandbox_timeout + 15` | **kwarg للـ subprocess** (ليس في argv) | الاختبار يقرأ `kwargs["timeout"] == 45` |

إثبات إيجابي إضافي داخل الاختبار: `"--network" in cmd and "timeout" not in cmd`
⇒ timeout ليس قيمة argv.

## السلبيات ×4 (إزالة العنصر ⇒ فشل متوقع)
| سلبي | محذوف | النتيجة |
|---|---|---|
| 1 | `--network none` | `AssertionError: missing from argv` ✔ |
| 2 | `--cap-drop ALL` | `AssertionError` ✔ |
| 3 | `--memory 256m` | `AssertionError` ✔ |
| 4 | `-w /work` | `AssertionError` ✔ |
| (بونص) | `timeout` من الـ kwargs | `AssertionError: kwarg` ✔ |

## المخرج الحي
```
tests/test_docker_argv_smoke.py ......  [100%]
6 passed in 0.03s
```
