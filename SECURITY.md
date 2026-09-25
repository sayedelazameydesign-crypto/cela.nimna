# Security policy

> **حالة المشروع: نسخة مرشحة للإصدار (Release Candidate) — محصّن وفق نطاق الاختبارات الحالية (77 اختبارًا)**
> الاختبارات لا تثبت الأمان المطلق. الحاويات تشترك في **نواة المضيف**؛ يجب إبقاء المضيف وDocker محدثين واستخدام **seccomp/AppArmor أو SELinux** عند النشر. راجع `README.md` و`docker-compose.yml`.

## Reporting a vulnerability
Please email the maintainers or open a private security advisory on GitHub.
Do **not** file a public issue for sensitive reports. We aim to acknowledge
within 72 hours and to ship a fix within 14 days.

## API access control (HTTP + WebSocket)

Implemented in `nimna/api/security.py`, wired by `create_app()`; covered by `tests/test_auth.py`.
Operator runbook (key generation, rotation, per-platform setup): [`docs/API-SECURITY.md`](docs/API-SECURITY.md).

| Control | Behaviour | Env var (default) |
|---|---|---|
| Mode | `production` or `development`. **Default is `production`** — a forgotten variable fails *closed*. Unknown values refuse to boot. | `NIMNA_ENV` (`production`) |
| Authentication | Every path requires the `X-Nimna-Key` header **except** `/` (static UI), `/api/health` and `/static/*`. Default-deny: `/docs`, `/openapi.json` and unknown paths also return **401**. Constant-time comparison (`hmac.compare_digest`). Dot-segments (`/static/../api/…`) never qualify as public. | `NIMNA_API_KEY` (—) |
| Boot refusal | In production the app **refuses to build** (`SecurityConfigError`) when `NIMNA_API_KEY` is missing, shorter than 32 chars, a placeholder/low-entropy value, or uses characters outside `[A-Za-z0-9._~+-]`. `nimna serve` exits with code 2; `uvicorn nimna.api.app:app` / FastAPI Cloud fail at import. Error messages never contain key values. | — |
| Key rotation | `NIMNA_API_KEY` accepts a comma-separated list: add the new key, roll clients, remove the old key. | `NIMNA_API_KEY` |
| Development | With a key: enforced exactly as in production. **Without a key: only loopback clients** (`127.0.0.1`, `::1`) are served — never expose a development instance or put it behind a same-host reverse proxy. | `NIMNA_ENV=development` |
| WebSocket | `/ws/*` always needs a key: the `X-Nimna-Key` header, or — because browsers cannot set headers on a WebSocket — the sub-protocol pair `["nimna.v1", "nimna.key.<key>"]`. The server only ever negotiates `nimna.v1`; the key protocol is never echoed. Rejections close with **1008** before accept (HTTP 403). | — |
| CORS | Explicit allow-list only (`scheme://host[:port]`); `*`, wildcards, paths and credentials are refused at boot. Unset ⇒ **production: every cross-origin request denied**; development: `http(s)://localhost|127.0.0.1|[::1]` on any port. `allow_credentials=false`; allowed headers `Content-Type`, `X-Nimna-Key`. The built-in UI is same-origin and needs no entry. | `NIMNA_ALLOWED_ORIGINS` (—) |
| Rate limit | `POST /api/chat`: **30 requests / minute per API key** (sliding window) ⇒ `429` + `Retry-After`. Cannot be disabled (must be ≥ 1). Unauthenticated requests are rejected before they reach the limiter. | `NIMNA_CHAT_RATE_LIMIT` (30) |
| WS connection cap | **10 concurrent `/ws/*` connections per API key**; the 11th is closed with 1008 until one disconnects. | `NIMNA_WS_MAX_CONNECTIONS` (10) |
| Security headers | On **every** HTTP response (incl. 401/404/429 and CORS preflights), overriding weaker app values: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`. | — |

Web UI: on the first `401` the page on `/` asks for the key (password field) and keeps it in
**`sessionStorage`** (this tab only), sends it as `X-Nimna-Key` on every `/api` call and via the
sub-protocol on the WebSocket. The 🔑 button changes or clears it.

**Known limits (by design, documented rather than hidden):**
- Rate-limit and connection counters are **in-process**: each uvicorn worker / pod counts on its own
  (the k8s HPA multiplies the effective ceiling by the replica count). Use a shared store (Redis) or an
  edge limiter if you need a global ceiling.
- One key = one trust level. There are no per-user identities or scopes yet; anyone holding a key can
  approve tools. Treat the key like an admin credential.
- `/api/health` stays public (platform health checks need it) and still reports provider/model/limits.
- No `Content-Security-Policy` yet: the UI uses inline scripts and CDN assets (Tailwind, marked, DOMPurify).

## Keys and secrets
- Never commit `.env`.  It is git-ignored. Copy `.env.example` instead. Run `chmod 600 .env`.
- Do **not** bake API keys into the Docker image — pass them at runtime via `env_file: .env` or environment variables.
- Keys are read only from environment variables (`GEMINI_API_KEY` / `GOOGLE_API_KEY` fallback, `OPENAI_API_KEY` / `NVIDIA_API_KEY` fallback). They are never written to logs,
  audit payloads, or the model history – `nimna/tools/base.py: redact_payload`
  and `nimna/memory/store.py` scrub `api_key`, `secret`, `password`, `token`
  and tokens like `sk-*`/`nvapi-*` before persistence or `log.exception`.
- `nimna doctor --offline` verifies the setup without pinging the provider and checks `.env`/`DB` permissions (0600).

## Filesystem isolation
- Every tool that touches a path goes through `ToolContext.resolve_path`
  (see `nimna/tools/base.py`). Absolute paths are rejected; `..` traversals,
  symlink escapes, and null bytes are blocked – the resolved target must stay
  inside `WORKSPACE_DIR`.
- `list_files` skips dot-files and drops entries whose symlink target would
  escape the workspace.
- `read_file` refuses files larger than `AGENT_MAX_FILE_BYTES` (default 5 MB);
  `write_file` / `write_report` enforce `AGENT_MAX_WRITE_BYTES` (2 MB).
- The workspace (`0700`) and SQLite directory (`0700`, DB `0600`) are created with restrictive defaults;
  run `nimna doctor` to check writability and permissions. The `.env` file is also tightened to `0600` by `ensure_dirs()`.

## Network isolation
- `fetch_url` / `web_search` call `_assert_public_url` (see
  `nimna/tools/builtin/web.py`): only `http`/`https`, literal private IPs and
  hostnames that resolve to private / loopback / link-local / reserved /
  multicast / unspecified addresses are rejected. IPv4-mapped IPv6 (`::ffff:127.0.0.1`) is handled by inspecting the mapped IPv4. Local suffixes (`.local`,
  `.internal`, `.localhost`) and `localhost` / `0.0.0.0` / `::1` are blocked.
  Redirects are followed hop-by-hop with fresh DNS per hop and the same checks (max 5 hops) — no connection pooling to avoid DNS rebinding.
- The search tool never contacts the user's private network directly; it only
  queries `html.duckduckgo.com`.

## Code execution
- **Default (safe):** `SANDBOX_BACKEND=subprocess` (no socket) — scrubbed env, `cwd=workspace`, POSIX `RLIMIT_AS` / `RLIMIT_CPU` / `RLIMIT_FSIZE` / `RLIMIT_NPROC`, but it is **not a security boundary** and is `confirm` (requires approval).
- **Docker isolation (opt-in):** `SANDBOX_BACKEND=docker` runs `run_python` in `python:3.11-slim` with `--network none`, `--cap-drop ALL`, `--security-opt no-new-privileges`, memory/cpu/pids limits, and only the workspace mounted (`nimna/tools/sandbox.py`). Even then containers **share the host kernel** — keep the host and Docker updated and apply a seccomp/AppArmor or SELinux profile in production.
- No tool ever executes code without going through the sandbox module.
- Image `nimna-agent:latest` runs as non-root `nimna` (uid 1000) — do **not** run as root. For production, prefer **Docker rootless** or **Podman**; it significantly reduces the impact of a container breakout versus a root daemon.

## Docker socket — local-sandbox profile
- The default `docker-compose.yml` service `nimna` **does not mount** `/var/run/docker.sock`. Verification: `docker compose config | grep -F docker.sock` must return nothing.
- An optional service `nimna-sandbox` (marked **`DEV ONLY — لا تنشر`** in `docker-compose.yml`) with `profiles: ["local-sandbox"]` mounts the socket and sets `SANDBOX_BACKEND=docker`. **Mounting the socket grants the container near-host control (can create arbitrary privileged containers) and is equivalent to very broad host privileges — even read-only mount is not sufficient isolation.** It is a **development-only** option and **must not** be available in a normal deployment or untrusted CI. Never use `--profile local-sandbox` on a machine with sensitive data.
- Alternatives: run Nimna outside Docker and let `run_python` use the host's Docker Engine, or use a least-privilege socket proxy / Podman / separate sandbox service.

## Computer control — isolated desktop (VNC)
- **Isolation:** `docker compose --profile computer up -d desktop` runs an Ubuntu LXDE desktop (`dorowu/ubuntu-desktop-lxde-vnc:focal`, 1280x800, noVNC on `:6901`) isolated from the host. Only `workspace` is shared (`/home/ubuntu/workspace`). Never runs on the host directly.
- **Tools:** `take_screenshot` is `safe` (read-only, saves to `workspace/.screenshots/` and feeds Vision Gateway); `mouse_click`, `type_text`, `shell_execute` are `confirm` and `restricted` — each suspends the run and requires visual approval with a red dot on the Mirror View.
- **Vision Gateway:** After each `take_screenshot`, the agent injects the image as `Message.user_with_image` (base64, `image/png`) so Gemini (`Part.from_bytes`) or OpenAI (`image_url`) can see the desktop. Images are capped at ~1.5 MB and never logged in full in audit (only path + preview).
- **Network:** Desktop has no access to host secrets. The noVNC port is bound to `127.0.0.1:6901` only.
- **VNC_PASSWORD:** read from `.env` — there is no default. `infra/desktop/vnc-guard.sh` wraps the image's
  `/startup.sh` and **refuses to start** the desktop when the password is empty, a known weak value (including
  the old hard-coded `nimna`) or shorter than 12 characters. Classic VNC auth only uses the first 8 characters,
  which is why the port stays on loopback. (`${VNC_PASSWORD:?}` is not used: compose interpolates every service,
  so it would break `docker compose up nimna` for everyone not using the `computer` profile.) If `COMPUTER_ENABLED` is not set, tools run in simulated mode (Pillow-generated placeholder) so the loop can be tested without the container.
- **Anti-abuse:** `shell_execute` blocks `rm -rf /`, `mkfs`, fork-bombs, etc., even inside the container; every click/typing/command is audit-logged with `approved` and `purpose`.

## Approvals
- Risk `confirm` tools (`delete_file`, overwriting `write_file`/`write_report`,
  `run_python` in subprocess mode, etc.) suspend the run and require an
  explicit decision. The CLI asks `[y/n/a]`; the API returns
  `status=awaiting_approval` with `{approval_id, tool_name, arguments, description}`
  and `POST /api/approvals/{id}` resolves it. Overwrites render as
  `هذه العملية ستكتب فوق: workspace/...` in both CLI and web UI.
- `ALWAYS` is scoped to `tool + skill + version` (see `nimna/core/agent.py: _approval_key`). A permanent approval does not carry to a different skill-set/version.
- Denied calls feed `{"error":"denied"}` back to the model; the agent is
  instructed never to retry a denied tool.
- Resume is immutable: the stored `tool_call` is executed; client cannot mutate it. Duplicate resume is rejected via atomic `WHERE resolved_at IS NULL`.

## Limits that stop runaway loops
`nimna/config.py` (all env-overridable):

```
AGENT_MAX_STEPS=12
AGENT_MAX_TOOL_CALLS=30
AGENT_MAX_RUNTIME_SECONDS=300
AGENT_MAX_RESPONSE_TOKENS=4096
AGENT_MAX_CONSECUTIVE_FAILURES=5
```

The agent also stops on: repeated identical tool calls (≥3), repeated
identical model text (≥3), unknown/forbidden tool, or validation failures.

## Skill loading
- Adding a folder under `skills/` does not auto-execute. The planner only sees
  the catalog (`name`/`description`/`triggers`). The body is loaded via
  `load_skill` (or at request start) and widened tools stay scoped to that run.
- `nimna skills validate` (and `SKILL.md`'s `risk_level`) flag skills that
  reference unknown tools or are marked `restricted`.

## Dependency pinning and scanning
- Direct dependencies are pinned in `pyproject.toml` / `requirements.txt` (e.g. `pydantic==2.13.5`, `fastapi==0.141.1`, `google-genai==2.25.0`). Re-pin after `pip freeze` and test.
- Run `pip-audit -r requirements.txt` regularly and before every release. For container images, run `trivy image nimna:latest`.
- Provider and network code is outside the mock test scope — a passing mock suite does **not** prove real provider connectivity.

## Mandatory operational rules
```
لا تستخدم --profile local-sandbox على جهاز يحتوي بيانات حساسة
لا تنشر بدون NIMNA_API_KEY — والخادم يرفض الإقلاع في الإنتاج بدونه
لا تضع NIMNA_ENV=development على خادم عام
لا تشغّل الخدمة كـ root
لا تضع مفاتيح API داخل صورة Docker
لا تعتبر subprocess عزلًا أمنيًا
الـ mock والاختبارات لا يثبتان نجاح الاتصال بمزود حقيقي
```

## Updates
Use `docker compose pull && docker compose up -d` or `pip install -U .`.
Pin `SANDBOX_IMAGE` if you need reproducibility. Security fixes for the host/Docker/kernel are the operator's responsibility.
