# triage-sync — عميل مزامنة اللوحة

عميل واحد (Node ≥ 18، بلا اعتماديات) يدفع بنود `S110/S112` الحية من ruff إلى لوحة
الترياج عبر `POST /api/triage/sync`، ويُغلق تلقائيًا ما أُصلح.

## التشغيل

```bash
# 1) مفتاح اللوحة (نفس TRIAGE_API_KEY المهيأ على الخادم)
export TRIAGE_API_KEY=...

# 2) من جذر المستودع — اللوحة المحلية افتراضًا
node tools/triage-sync/sync-push.mjs

# لوحة أخرى تتبع العقد نفسه (مثال: نسخة منشورة)
TRIAGE_BOARD_URL=https://my-board.example.com node tools/triage-sync/sync-push.mjs

# عرض الحِمل بلا إرسال
DRY_RUN=1 node tools/triage-sync/sync-push.mjs
```

| متغير | الافتراضي | الوظيفة |
|---|---|---|
| `TRIAGE_API_KEY` | — (إلزامي) | مفتاح اللوحة |
| `TRIAGE_BOARD_URL` | `http://127.0.0.1:3000` | عنوان اللوحة |
| `RUFF_BIN` | `<repo>/.venv/bin/ruff` | مسار ruff |
| `RUFF_SELECT` | `S110,S112` | القواعد المدفوعة |

## العقد المتوقع من اللوحة

| البند | القيمة |
|---|---|
| المزامنة | `POST /api/triage/sync` — `{items: [...]}`، حد 200/طلب |
| اللقطة | `GET /api/triage/snapshot` — `totals` + `hygieneByFile` + `refs` + `generatedAt` |
| صيغة Arena | `GET /api/triage/snapshot?format=arena` — `items[]: {ref, state, category, title, description}` |
| المصادقة | `Authorization: Bearer <TRIAGE_API_KEY>` أو `x-triage-api-key` |

**Mapping:** `externalRef = ruff:<rule>:<path>:<line>` · `state ⟶ status` · `category ⟶ issueType` · `title ⟶ summary` · `description ⟶ details`.

## القواعد السلوكية (مختبرة حيًا)

1. **upsert**: (externalRef + issueType) موجود ⇒ تحديث؛ غير موجود ⇒ إدراج.
2. **لا status في الحِمل** ⇒ حالة اللوحة لا تُمسّ (قرار بشري يبقى).
3. **الإغلاق التلقائي**: مرجع كان `open` واختفى من ruff ⇒ `resolved`.
   ما راجعته يدويًا (`reviewing`) **لا يُغلق** — يبقى للمراجعة البشرية.
4. `filePath` مطلوب للإدراج الجديد فقط؛ التحديث/الإغلاق يكفيه المرجع.

## تحقق حي (2026-09-25)

- دفع الحالة القائمة: `inserted=0 updated=50`.
- بند جديد ظهر في ruff ⇒ `inserted=1` (وبلا تدخل يدوي).
- مراجعة يدوية `reviewing` ثم اختفاء البند ⇒ **لم يُغلق** (القاعدة 3).
- إعادته `open` ثم الدفع ⇒ `أُغلق=1` و`resolved`.

## ملاحظات دقيقة

- `S110` لا يصطاد `except <Type>: pass` المُنمَّط (`KeyError` مثلًا) — يصطاد
  `except:` و`except Exception: pass`. أُثبت ذلك حيًا أثناء الاختبار.
- العميل لا يحذف صفوفًا أبدًا: الإغلاق تغيير حالة فقط (سجل تاريخي محفوظ).
