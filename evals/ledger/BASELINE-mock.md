### 🧪 Arena Task Suite — mode: `mock`

> ⚠️ **MOCKED SUITE — ليست نتيجة جودة حقيقية.** كل المهام شُغِّلت على `MockProvider`؛ النتيجة تثبت أن خط القياس يعمل فقط. الحكم الحقيقي يأتي في P5 (live + judge). لا يُطبع PASS في هذا الوضع أبداً.

**Repo:** `876eb19` · **Tasks:** `11` · **Generated:** `2026-09-23T17:38:36+00:00` · **run_command:** `enabled`

| Task | Category | Verdict | Check score | Checks | Notes |
| :--- | :--- | :--- | ---: | ---: | :--- |
| `arabic-01-market-brief` | arabic_long | 🧪 **MOCKED** | 0.0 | 0/1 | mock provider run — deterministic checks only · failed: artifact:briefs/market_brief.md |
| `arabic-02-lesson-plan` | arabic_long | 🧪 **MOCKED** | 0.0 | 0/1 | mock provider run — deterministic checks only · failed: artifact:lessons/plan.md |
| `code-01-fizzbuzz-module` | coding | 🧪 **MOCKED** | 100.0 | 5/5 | mock provider run — deterministic checks only |
| `code-02-fix-buggy-mean` | coding | 🧪 **MOCKED** | 100.0 | 2/2 | mock provider run — deterministic checks only |
| `code-03-write-tests` | coding | 🧪 **MOCKED** | 50.0 | 1/2 | mock provider run — deterministic checks only · failed: artifact:tests/test_calc.py |
| `code-04-refactor-split` | coding | 🧪 **MOCKED** | 100.0 | 1/1 | mock provider run — deterministic checks only |
| `code-05-shell-fix-retry` | coding | 🧪 **MOCKED** | 100.0 | 4/4 | mock provider run — deterministic checks only |
| `data-01-csv-summary` | data | 🧪 **MOCKED** | 50.0 | 1/2 | mock provider run — deterministic checks only · failed: artifact:reports/sales_summary.md |
| `data-02-json-transform` | data | 🧪 **MOCKED** | 0.0 | 0/1 | mock provider run — deterministic checks only · failed: artifact:data/orders_sorted.json |
| `web-01-summarize-article` | web | ⏭️ **SKIPPED** | — | — | missing capabilities: network |
| `web-02-extract-links` | web | ⏭️ **SKIPPED** | — | — | missing capabilities: network |

**Summary:** ran `9` · skipped `2` · error `0` · mean check-score `55.6` · secret hits `0` · regressions `0`

**Metrics:** verified `4/9` · tool calls `5` (failed `0`) · shell executions `3` (failed commands `1`) · security denials `0` · evidence completeness `100.0`% · recovered `1` · mean wall `16.2`ms

**Delta evidence:** `2` artifact hash(es) re-verified against execution evidence — Delta + Re-observation = Evidence (P1-T2).

> قاعدة الأمانة: `MOCKED` لا يعني نجاحاً و`SKIPPED` لا يعني فشلاً — والحكم الحقيقي على الجودة يُقاس في وضع live مع الحَكَم (P5).
