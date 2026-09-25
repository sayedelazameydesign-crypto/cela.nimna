# FREE-STACK — بدائل مجانية/مفتوحة مُتحقق منها (verified 2026-09-25)

> كل أداة في القائمة الأصلية فُحصت فعليًا (وجود + ترخيص + ملاءمة).
> المرفوض موثق **مع السبب** — لا توصية عمياء.
> `tests/test_foss_stack.py` (9) يثبّت أن الكلام المكتوب هنا مطبّق فعلًا.

## القاعدة الذهبية: أعد التحقق (re-verify) من الترخيص يوم الاعتماد

Redis وTerraform وGrafana وDragonfly كلها **غيّرت تراخيصها بعد أن كانت
BSD/MIT/Apache**. أي جدول تراخيص — بما فيه هذا — يصدأ. قبل اعتماد أي أداة:
افتح ملف `LICENSE` في المستودع الرسمي وتأكد. ما ورد أدناه صحيح بتاريخ
التحقق (2026-09-25) ومثبت بالمصادر.

## سجل التحقق (adopted / recommended / rejected / experimental / not-a-tool)

| الأداة | الحكم | الترخيص (وقت التحقق) | الدليل / السبب |
|---|---|---|---|
| Valkey | **adopted** (بدل Redis) | BSD-3، Linux Foundation | fork من Redis 7.2.4، متوافق RESP/Lua بالكامل، الصورة `valkey/valkey:8`. Redis ≥ 7.4 ثنائي SSPL/RSAL (غير OSI) — التبديل إصلاح امتثال حقيقي |
| KeyDB | recommended (بديل ثانٍ) | permissive (Snap) — تحقق عند الاعتماد | متوافق RESP؛ Valkey أولًا لأنه fork مباشر بلا فروق أوامر |
| Dragonfly | ⚠️ بشروط فقط | BSL 1.1 (source-available، **ليس** OSI) — يتحول لـ Apache 2.0 في 2029 | مجاني للاستضافة الذاتية الداخلية، ممنوع تقديمه كخدمة مدارة منافسة. لا تعتمده إن كان شرطك "FOSS صارم" |
| HAProxy / NGINX / Traefik | **adopted (مثال HAProxy)** | GPLv2 / BSD-2 / MIT | `infra/haproxy/haproxy.cfg` مثال يعمل أمام نسختين |
| ModSecurity / Coraza / Certbot | recommended | Apache-2.0 | WAF + TLS؛ تُركّب عند الـ LB لا داخل التطبيق |
| LucidMQ | **rejected (كوسيط موزع)** | مفتوحة (Rust) | مؤكد: **مكتبة brokerless تُضمَّن في التطبيق** لا خدمة مستقلة ([lib.rs/crates/lucidmq](https://lib.rs/crates/lucidmq): "no external processes running"). مشروع صغير (~41★) وإصدارات المكتبة 0.1.x (2022). يصلح للتضمين داخل تطبيق Rust، لا كبديل Kafka موزع |
| StrikeMQ | **rejected** | — | موجود ([awneesht/Strike-mq](https://github.com/awneesht/Strike-mq)) لكن المؤلف نفسه يقول: "للتطوير والاختبار، **ليس للإنتاج** — يضحّي بالمتانة" |
| Rafka | **rejected** | MIT/Apache (مشروعان بنفس الاسم) | [LYZJU2019/rafka](https://github.com/LYZJU2019/rafka): "تجريبي، **ليس جاهزًا للإنتاج**"؛ والثاني مشروع تعليمي. لا queue تجريبي في مسار المتانة |
| NATS JetStream | recommended (بدل الثلاثة) | Apache-2.0، CNCF | البديل الجاد: ثنائية واحدة، ثبات مثبت، de-dup وreplay. الثاني: RabbitMQ (MPL-2.0) |
| Postgres + pgBackRest | documented (مثال جاهز) | Postgres: PostgreSQL License؛ pgBackRest: **MIT** ([pgbackrest.org](https://pgbackrest.org)) | المعيار الفعلي (full/diff/incr + WAL + PITR). المثال: `infra/postgres/pgbackrest.conf.example` |
| Barman | optional | GPL (copyleft — لاحظ الفرق عن MIT) | بديل pgBackRest إن ناسبتك GPL |
| SigNoz | recommended | نواة permissive مفتوحة قابلة للاستضافة الذاتية بلا سقف بيانات (open-core: المزايا المتقدمة في السحابة) | OTel-native: traces+metrics+logs في ClickHouse واحدة. البديل: Prometheus+Grafana+Loki+Tempo |
| Prometheus / Alertmanager | **adopted (مثال)** | Apache-2.0 | `infra/monitoring/prometheus.yml` + `alerts.yml` على أسماء مقاييس حقيقية من `/api/metrics` |
| Grafana / Loki / Tempo | recommended مع ملاحظة | **AGPLv3** (copyleft شبكي) | مجاني للاستضافة الذاتية؛ القيد يظهر فقط إن عدّلت وقدّمت كخدمة. UIs تُشير لـ Prometheus أعلاه |
| Uptime Kuma | recommended | MIT | أسهل مراقبة uptime + صفحات حالة |
| Coroot | recommended | Apache-2.0 (تحقق من المستودع) | APM صفر-إعداد عبر eBPF |
| Tech Sentinel Monitor | listed (تحقق بنفسك) | **غير مؤكدة** — افتح المستودع أولًا | موجود ([ronaldgoodchild/tech-sentinel-monitor](https://github.com/ronaldgoodchild/tech-sentinel-monitor)) لكن الترخيص لم يُرصد في البحث |
| OpenTofu | recommended | **MPL-2.0**، Linux Foundation + CNCF | البديل المباشر لـ Terraform (ثنائية مكان ثنائية لمعظم الإعدادات) |
| Orion | **experimental — للتجربة فقط** (تصحيح: كنا مخطئين) | **MIT** | موجود فعلًا: [bit2swaz/orion](https://github.com/bit2swaz/orion) — منسّق حاويات موزع أحادي-الثنائية (Go 100%): raft+boltdb مضمّن (بلا etcd) + gossip ‏(memberlist/SWIM+Lifeguard). بحثنا فشل لأن المشروع صغير جدًا (نجمتان، كاتب واحد، 11 كوميت في ديسمبر 2025 فقط، إصدار واحد v1.0.0) فلم يظهر في النتائج. بهدف تعليمي معلن ("i wanted to understand how kubernetes actually works"). الأرقام المنشورة ذاتيًا: failover ‏~2-4s وscheduling ‏<10ms — **غير متحقق منها مستقلًا**. للإنتاج الحرج: k0s/MicroShift |
| k0s | recommended | Apache-2.0 (Mirantis) | كوبيرنيتس بثنائية واحدة؛ `k8s/*.yaml` الحالية تعمل عليه كما هي |
| MicroShift | recommended | Apache-2.0 (Red Hat) | OpenShift مصغّر للحافة/الأجهزة الصغيرة |
| OpenSVC | recommended | Apache-2.0 (من v3) ([book.opensvc.com](https://book.opensvc.com)) | إنتاج منذ 2009: agent + clustering + orchestration |
| Keyflare | recommended (بشروط) | **MIT** (مؤكد رباعيًا: ملف LICENSE + package.json + README + شارة GitHub) | [keyflare-labs/keyflare](https://github.com/keyflare-labs/keyflare): مدير أسرار Worker+D1، v0.1.0، 28 نجمة، هادئ منذ أبريل 2026، مشرفان (84+63 كوميت) + كوميتات bot ظاهرة (Copilot/agent). المخرج موجود (`kfl secrets download` إلى env/JSON/YAML). المفتاح الرئيسي SPOF يُعرض مرة واحدة بلا تدوير موثق. زر النشر يشير لمنظمة قديمة (keyflare بدون labs) — علامة صيانة. لا CLA. الأخطر من الترخيص: الاعتماد على سقوف Cloudflare المجانية — انظر الأولوية 7. **اختبار فقدان المفتاح: راسب** — الـCLI (6 ملفات أوامر مفحوصة) بلا rotate/backup/restore، والـREADME يقر: الفقدان غير قابل للاسترجاع. المعوَّض: تنزيلات دورية + نسخة المفتاح |
| Sigillo | recommended | **MIT** ([remorses/sigillo](https://github.com/remorses/sigillo)) — معلن في README وحقل cli/package.json، **لكن بلا ملف LICENSE جذري** (**فجوة توثيق ترخيص لا شكلية**: حقل package.json يتغير بسطر بلا مراجعة، وبعض الفرق القانونية لا تعتمده وحده؛ مسودة قضية جاهزة وتنتظر إذن النشر) | Doppler مفتوح على Cloudflare، v0.13.0 نشط (أغسطس 2026)، 58 نجمة، تصدير (json/env/yaml/docker)، ونشر ذاتي بأمر واحد — **لكن** يعتمد على `auth.sigillo.dev` مركزي (OAuth) ومشرف واحد. بينه وبين Keyflare: Sigillo أنضج (نشط مقابل هادئ). الاعتماد على سقوف Cloudflare — انظر الأولوية 7. **اختبار فقدان المفتاح: راسب** — لا مسارات backup/rotate/restore في الشجرة كاملة، والتدوير المدمِّر موثق. المعوَّض: التصدير الدوري |
| Infisical | recommended (**خط الأساس المؤسسي**) | جذر LICENSE = نص MIT Expat + فقرة استثناء («All content that resides under any "ee/" directory ... licensed under ... "ee/LICENSE"» — MIT معدّل كملف، ومنح MIT قياسي لنطاق النواة)؛ `ee/` تحت `backend/src/ee` محكومة بـ`LICENSE.md` = **Infisical Enterprise License** (ملكية: الإنتاج باشتراك، التطوير/الاختبار بلا اشتراك، التوزيع ممنوع) | [Infisical/infisical](https://github.com/Infisical/infisical): ‏27.5k نجمة، E2E حقيقي (التشفير بالعميل والخادم لا يقرأ)، استضافة ذاتية بـcompose بلا حساب خارجي، سحابة مجانية حتى 5 أعضاء ثم ~$6–9/user. **التدقيق المستقل**: SOC 2 Type II + HIPAA + FIPS 140-3 + اختراق مستمر — موثق رسميًا ومؤكد من منافس (Akeyless)، الوحيد في الفئة — لكن الشهادات تغطي سحابة Infisical وعمليات الشركة لا نشرتك الذاتية. ادعاء الاستخدام الحكومي من مدونتهم (غير متحقق مستقلًا). **تحفظ ee/**: ميزات المؤسسات (SSO/RBAC/تدقيق/تدوير/dynamic/external-KMS/FIPS) كلها داخل `ee/` (قائمة الخدمات + أعلام TFeatureSet كلها false للمجانية، وملفات ee بلا ترويسة — الرخصة على مستوى المجلد) — النواة MIT = خزنة أساسية + E2E + versioning فقط (keystore خارج ee والواجهة بلا ee أصلًا، والـREADME صامت عن الترخيص: صفر سطر). ف«الخط الأساس المؤسسي» = النواة للأساسيات، والتجارية/السحابة للقطاع المنظم — لا Keyflare ولا Sigillo في الحالتين |
| SOPS + age | recommended | MPL-2.0 / BSD | أسرار مشفّرة في git — الأبسط لفريق صغير |
| LitmusChaos / ChaosMesh | recommended | Apache-2.0، CNCF | chaos ناضج لـ k8s؛ الجدول الحالي في `docs/RESILIENCE.md` §8 يبقى للتمارين اليدوية |
| Krkn | recommended | مفتوح 100%، **CNCF Sandbox** (قُبِل ديسمبر 2023)، Red Hat | chaos + اختبار مرونة وأداء تحت الحمل لـ k8s/OpenShift ([redhat-chaos/krkn](https://github.com/redhat-chaos/krkn)) — الأنسب إن أردت chaos مع قياس أداء |
| Pumba / Toxiproxy / chaoskube | recommended | Apache/MIT (تحقق من المستودع) | chaos خفيف للحاويات/الشبكة (Toxiproxy أنسب لاختبار القاطع) |
| PikoCI | recommended | **Apache-2.0** ([pikoci/pikoci](https://github.com/pikoci/pikoci)) | CI أحادي-الثنائية (ذاكرة→SQLite→Postgres) بنموذج Concourse. حديث — راجع النضج قبل الاعتماد الكامل |
| Woodpecker CI | recommended | Apache-2.0 | fork ناضج من Drone، YAML مألوف |
| Forgejo | recommended (البديل الصارم) | **GPL-3.0** (تصحيح: كنا نقول MIT خطأً) | git + CI مجتمعي بالكامل على [codeberg.org/forgejo/forgejo](https://codeberg.org/forgejo/forgejo) — لا وجود له على GitHub أصلًا، لا CLA (موثق في CONTRIBUTING.md)، الجذر GPL-3.0 وملفات موروثة بترويسات MIT (متوافقة، عيّنة: main.go). copyleft بلا قيد شبكي: الاستضافة كخدمة حتى تجارية لا تُلزم بكشف المصدر — القيد فقط عند توزيع ثنائيات معدّلة. الفحص الكامل تم عبر mirror غير رسمي حديث (qundao/mirror-forgejo، HEAD سبتمبر 2026 — والتحقق بسلسلة git: الـmirror سلف مباشر للرسمي بفارق 4 كوميتات chore فقط (ملاحظات إصدار/renovate/CI)، صفر ملفات مصدرية — فالتعداد ينطبق حرفيًا على الرسمي): **صفر ترويسات proprietary في 8255 ملفًا** (5 إصابات كلها قوالب منتقي التراخيص في options/license)، والتعداد: MIT موروث + GPL-3.0-or-later للجديد. الإصدارات: stable ربع سنوي + LTS كل سنتين — الحالي LTS ‏v15.0 (مدعوم حتى يوليو 2027) — للإنتاج ثبّت على LTS. القاعدة: **مُشغّل خدمة ← Forgejo (لا CLA وGPL يحمي من الاستيلاء المغلق)، مُوزّع منتج ← Gitness (نواة permissive مع خطر CLA) |
| Gitness (الآن Harness Open Source) | recommended مع asterisk موثق | Apache-2.0 للشجرة + **27 إصابة (26 ترويسة + 1 مرجع) | المستودع انتقل إلى [harness/harness](https://github.com/harness/harness) (38k★، نشط يوميًا). الجذر والـREADME والـbackend (Go) وأغلب الواجهة Apache-2.0؛ لكن فحصًا كاملًا (clone ضحل 50MB + grep لكل الشجرة) وجد **23 ملفًا بـ PolyForm Shield 1.0.0** (يمنع الاستخدام المنافِس لـ Harness — 6 منها في الواجهة المشحونة) و**3 ملفات بـ PolyForm Free Trial** (واحد مشحون: `ar/strings/types.ts` المولّدة). لا `LICENSE.enterprise` ولا مجلد `licenses/`، وروابط النصوص في الترويسات ميتة (404 — النصوص انتقلت لـ polyformproject.org/licenses). **CLA مطلوب** (cla-assistant) — أي مرونة إعادة الترخيص مستقبلًا بيد الشركة. **الخطر الأكبر هو الـCLA لا PolyForm**: الترخيص الحالي لا يُسحب بأثر رجعي، لكن الإصدارات القادمة قد تتغير (مسار HashiCorp) — التخفيف: **pin-to-tag** (اربط `v2.x.x` لا `main`). ملف الـFree Trial المشحون (`types.ts`، 807 سطور) **مقصود ومُتجدد ذاتيًا**: المولّد نفسه (Apache-2.0) يختمه بالترويسة عند كل توليد. الملفات الستة المشحونة تجميلية (15–105 سطور: زر نسخ، زر نجمة، getters جلسة). الروابط الميتة (404) لا تُبطل الترخيص (الاسم والإصدار محددان) لكنها غموض إضافي. السيناريوهات: فريق داخلي مع fork مثبّت ✅ آمن؛ استضافة Git كخدمة تجارية ⚠️ (Shield قد يعضّ) والأصلح Forgejo؛ قطاع OSI-only ❌ والأصلح Forgejo/Woodpecker؛ تتبع main ⚠️ والتخفيف pin-to-tag. تاريخ PolyForm (git log): تلوث **متكرر بالنقل** لا زحف تدريجي — ملف بترويسة 2020 هبط أغسطس 2024 (دمج Artifact Registry) وآخر بترويسة 2021 هبط يوليو 2025 (لوحات CDE) — فنقل وحدات جديدة من واجهة Harness المغلقة سيضيف المزيد على الأرجح. إجراء الترقية: أعد `check_upstream_licenses.py` وقارن القائمة (الحارس الشهري `upstream-license-watch` يفعل ذلك آليًا، baseline 27). مبدأ الحارس: **الفشل الصاخب على الفشل الصامت** — عدّ التواجد قد يُنذر زائدًا لكنه مستحيل أن يفوّت متغيرًا جديدًا بصمت (عكس تعداد الأسماء الذي يفشل بصمت ويقتل قيمة الحراسة) |
| "ResticBorgBackup" | **not-a-tool — تصحيح** | — | لا توجد أداة بهذا الاسم؛ هما مشروعان منفصلان: **Restic** (BSD-2) و**BorgBackup** (BSD-3). أيهما يصلح لنسخ الملفات/الأقراص |

## ما الذي تغيّر في المستودع فعلًا

1. **Valkey بدل Redis** (إصلاح امتثال، بلا تغيير كود — RESP متوافق):
   `docker-compose.yml` و`k8s/redis.yaml` صارا `valkey/valkey:8` مع
   `valkey-server`/`valkey-cli`. أسماء الخدمة/المجلد/المتغير (`redis`،
   `REDIS_URL`، مخطط `redis://`) بقيت عمدًا — طبقة بروتوكول لا مورّد.
2. **مثال LB**: `infra/haproxy/haproxy.cfg` — فحص `/api/health` + leastconn
   أمام نسختين. NGINX/Traefik في §الأولويات أدناه (مقتطفات).
3. **حزمة مراقبة**: `infra/monitoring/prometheus.yml` (كشط `/api/metrics`
   بتوكن Bearer من ملف — النقطة خلف المصادقة) + `alerts.yml` (أربع قواعد
   على أسماء مقاييس حقيقية، العتبات من SLO في `docs/RESILIENCE.md`).
4. **مثال pgBackRest**: `infra/postgres/pgbackrest.conf.example` + تمرين
   استرجاع شهري (غير موصول بشيء — SQLite هو المخزن الحالي).

## الأولويات التسع — أين تقف كل واحدة

### 1) ذاكرة + LB
Valkey معتمد أعلاه (KeyDB احتياطي). LB: مثال HAProxy جاهز؛
مكافئ NGINX (مقتطف):
```nginx
upstream nimna { least_conn; server nimna1:8000 max_fails=2 fail_timeout=5s; server nimna2:8000 max_fails=2 fail_timeout=5s; }
server { listen 80; location / { proxy_pass http://nimna; proxy_set_header X-Request-ID $request_id; } }
```
ومكافئ Traefik (labels على خدمة compose):
```yaml
labels:
  - "traefik.http.routers.nimna.rule=Host(`api.example.com`)"
  - "traefik.http.services.nimna.loadbalancer.healthcheck.path=/api/health"
```
TLS بالحافة (Certbot) + WAF (Coraza) عند الحاجة — لا شيء منهما داخل التطبيق.

### 2) Postgres + نسخ + اختبار استرجاع
اليوم SQLite (`scripts/backup_sqlite.py` + RPO/RTO في `docs/RESILIENCE.md`
§5). عند الكتابة الأفقية: Postgres (96–256MB لنسخة صغيرة) + استرجاع مُختبَر
شهريًا من `pgbackrest.conf.example`. لا ترحيل استباقيًا — الحدود موثقة.

### 3) Queue + idempotency
**لا queue اليوم** (لا workers — YAGNI كما في `docs/RESILIENCE.md` §9).
Idempotency موجودة في الكود (`Idempotency-Key`، 10 اختبارات). مع أول worker
حقيقي: NATS JetStream (أو RabbitMQ) + مستهلك واحد لكل queue + KEDA للتوسع.

### 4) مراقبة
Prometheus pack أعلاه يعمل اليوم ضد `/api/metrics`. SigNoz هو البديل
الأحادي متى أردت traces (يحتاج OTel SDK — خارج النطاق حاليًا). Uptime Kuma
لصفحات الحالة. **ملاحظة صدق**: قاعدة `NimnaLatencyAvgHigh` تراقب المتوسط
لا p95 (المقياس summary بلا buckets — موثق في الملف نفسه).

### 5) OpenTofu
لا ملفات tf في المستودع (النشر الحالي Render + k8s manifests). عند أول
بنية سحابية مُدارة: OpenTofu (MPL-2.0) لا Terraform (BSL) — نفس اللغة.

### 6) استضافة ذاتية
`k8s/*.yaml` الحالية (anti-affinity + PDB + HPA) تعمل على أي عنقود متوافق
بما فيه k0s. Orion ([bit2swaz/orion](https://github.com/bit2swaz/orion)، MIT) موجود لكنه تجريبي (ديسمبر 2025، مطور واحد، نجمتان) — للتجربة والتعلم لا للإنتاج الحرج. MicroShift/OpenSVC للحافة
والأجهزة الصغيرة.

### 7) أسرار
اليوم: env + عدم تسريب في السجلات/الصحة (مُختبَر). الترقية التدريجية:
SOPS+age (الأبسط) ← Infisical ذاتي-الاستضافة (الأنضج) ← Sigillo/Keyflare
(مشروطة بالملاحظات أعلاه). لا Vault (BSL) ولا أسرار في git مكشوفة.

**رياضيات Cloudflare المجانية** (لـ Keyflare/Sigillo — سؤال ماذا لو تغيّرت الشروط؟): Workers Free 100K req/day مع 10ms CPU (تجاوزها = Error 1027) وD1 5M قراءة/100K كتابة يوميًا مع 5GB (تجاوزها = **توقف صلب** للاستعلامات، لا فوترة تلقائية). لأحمال الأسرار (قراءة عند النشر، كتابة نادرة) الهامش ضخم — ادعاء المجانية كافية صادق **اليوم**. المخاطر الحقيقية: (1) **توقف صلب** عند أي تجاوز (CI مزدحم، poller سيئ) — خفّف بالحقن وقت النشر لا القراءة الحية؛ (2) **تغيّر الشروط** مستقبلًا — خفّفه بتصدير ربع سنوي مشفّر (كلتاهما قابلتان للتصدير، والخروج يوم عمل)؛ (3) **صمام $5**: خطة Workers المدفوعة تزيل كل السقوف — أرخص تأمين في البنية، وتحوّل طبيعة الخطر من توقف إلى فاتورة. البيانات صغيرة (KBs) فالجاذبية منخفضة والخروج سهل.

**اختبار القبول (فقدان المفتاح الرئيسي)**: Keyflare وSigillo **راسبان** — لا استرجاع ولا تدوير آمن في أي منهما (الأول بلا أوامر استرجاع في CLI، الثاني بلا أي مسار استرجاع وتدويره مدمِّر موثق). المعوَّض الإجرائي: تصدير دوري مشفّر + نسخة المفتاح في مكانين. إجراء المحاكاة الحية (لمن يملك حساب Cloudflare — غير ممكن من هذا الصندوق): انشر نسخة تجريبية → احذف Worker Secret → أعد التشغيل → تحقق أن البيانات غير قابلة للفك وأن آخر تصدير يعيد البناء. **خط الأساس المؤسسي**: Infisical — بتحفظ: النواة MIT (خزنة + E2E) والميزات المؤسسية (SSO/تدقيق/تدوير) في `ee/` الملكية، والشهادات (SOC 2/HIPAA/FIPS) تغطي سحابة الشركة لا نشرتك — للقطاع المنظم: التجارية أو السحابة، لا بديل عنهما في هذه القائمة.

### 8) Chaos
جدول `docs/RESILIENCE.md` §8 (يدوي) + Toxiproxy لاختبار القاطع محليًا.
Litmus/ChaosMesh عند عنقود staging دائم، وKrkn (CNCF Sandbox من Red Hat)
إن أردت chaos مع قياس أداء تحت الحمل. لا chaos على الإنتاج بلا ميزانية
خطأ مكتوبة.

### 9) CI
اليوم GitHub Actions (يعمل). PikoCI/Woodpecker + Forgejo (أو Gitness/Harness Open Source: ‏Apache-2.0 مع 27 إصابة PolyForm موثقة في الجدول) هي مسار الخروج من السحابة متى أردت CI ذاتي-الاستضافة
بالكامل — لا هجرة قبل الحاجة. (Forgejo ‏GPL-3.0 بلا CLA، والفحص الكامل (8255 ملفًا) خرج نظيفًا — التفاصيل في الجدول. أعد الفحص بنفسك: `git clone --depth 1 https://codeberg.org/forgejo/forgejo` ثم `grep -ril polyform . --exclude-dir=.git`.)

## خريطة الملفات

| الملف | الغرض |
|---|---|
| `docker-compose.yml` (خدمة `redis`) | صورة Valkey + فحص صحة `valkey-cli` |
| `k8s/redis.yaml` | Deployment/ConfigMap/Service على Valkey |
| `infra/redis/{redis.conf,README.md}` | إعداد LRU + شرح التسمية |
| `infra/haproxy/haproxy.cfg` | مثال LB (أولوية 1) |
| `infra/monitoring/{prometheus.yml,alerts.yml}` | حزمة المراقبة (أولوية 4) |
| `infra/postgres/pgbackrest.conf.example` | مثال النسخ (أولوية 2) |
| `tests/test_foss_stack.py` | 9 اختبارات تثبّت كل ما سبق |
| `docs/RESILIENCE.md` | SLO/runbooks/chaos (المرجع التشغيلي) |
