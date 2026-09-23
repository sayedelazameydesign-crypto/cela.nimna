# Browser Use Cloud — API V4 (مرجع التكامل)

> **ملاحظة الدمج:** هذا المرجع مدموج من توثيق Browser Use الرسمي بصيغته المعدّة للوكلاء. النسخة الكاملة المُدارة على `https://docs.browser-use.com/.well-known/llms-full.txt` (تُخزَّن مؤقتًا حتى 24 ساعة). داخل مهارة Nimna: انظر `skills/browser_use/SKILL.md` والمرجع المختصر في `skills/browser_use/references/`.

> ## Documentation Index
> Fetch the complete documentation index at: https://docs.browser-use.com/llms.txt
> Use this file to discover all available pages before exploring further.

> ## Agent Instructions
> Use https://docs.browser-use.com/llms.txt and its linked .md pages for current documentation. The managed full bundle is https://docs.browser-use.com/.well-known/llms-full.txt and can be cached for up to 24 hours. Do not use the obsolete /cloud/llms*.txt or /open-source/llms*.txt static exports.
> Choose Cloud API V4 for new agent integrations; V2 is the lower-cost option for simple tasks. Keep V3 examples explicitly versioned. The open-source browser-use library and hosted browser-use-sdk have different APIs.
> Cloud authentication uses X-Browser-Use-API-Key, without a Bearer prefix. Install or upgrade browser-use-sdk and use its explicit v4 import for V4. Check the published OpenAPI reference for request fields; do not invent SDK support for new fields.
> Cloud concurrency and HTTP request rate are separate. Read GET /api/v2/billing/account for the key's projectId, concurrentSessionLimit, activeSessionCount, and credit balance, including when using V4. Keys in one project share capacity and credits; rateLimit is a legacy concurrency alias, not requests per second.
> Keep the highest applicable existing, legacy-plan, and spend-tier concurrency grant. Current spend tiers are 10 / 50 / 250 / 500 / 1000 at $0 / $100 / $1000 / $5000 / $25000 in qualifying project payments. Legacy or externally billed projects can follow different billing paths; trust the account limit. See https://docs.browser-use.com/cloud/guides/concurrency.md.
> HTTP rate limits have two layers. Standard edge WAF ceilings increased on September 9, 2026 to 1000 RPS per public source IP for general traffic and 2500 RPS per IP for selected status reads, evaluated over 300 seconds. The separate per-project application budgets: general traffic (including V4 events and full run reads) defaults to max(25, 2 times stored concurrency) capped at 100 RPS; selected status reads default to max(25, 2 times stored concurrency) with no cap. The two are counted independently. Project overrides and account-specific edge rules can differ. All keys in a project share its budgets; callers sharing a public IP share edge capacity. See https://docs.browser-use.com/cloud/guides/concurrency.md.
> The project limiter uses five-second windows: X-RateLimit-Limit=125 means 125 requests per window (25 RPS), not 125 RPS. Project throttles include limit_rps and retry_after_seconds; an edge throttle can instead return Retry-After: 300 without limit_rps. Honor the returned Retry-After. Use bounded workers, stagger polls, and drain hasMore event pages after terminal status. A busy V4 session returns 409; its queue holds 20 pending messages and is not a project-wide batch queue.
> A completed run or closed CDP connection does not immediately stop its cloud browser. Stop unneeded owned browsers with PATCH /api/v4/browsers/{id} and {"action":"stop"}. A client wait timeout does not cancel the server-side run.
> Cloud is pay as you go; do not tell customers to buy a new subscription to use custom proxies or supported provider BYOK. Usage funding and model eligibility still apply. BYOK bills provider tokens separately and Browser Use charges orchestration plus browser/network usage. See https://docs.browser-use.com/cloud/guides/billing.md.
> Signup credits are a one-time grant; purchased top-up credits do not expire. Check the API key's project before diagnosing missing credits. API-key monthly spending caps are soft limits, not a strict prepaid wallet; concurrent or already-running work can exceed them. Auto recharge has separate trigger and purchase amounts and can charge immediately when enabled below the threshold. Use https://browser-use.com/pricing for current rates.
> Box and Bux are retired. Do not recommend their SDKs, sandbox quotas, or subscription plans. Use the Cloud Agent or Browser Infrastructure guides.
> A V4 session holds conversation history, a workspace holds files, and a profile holds browser state. These IDs and V3/V4 workspace namespaces are not interchangeable. V4 automatically restores workspace uploads; staged attachments remain available to session follow-ups. Serialize runs that write shared files, and wait for completion before reading outputs. See https://docs.browser-use.com/cloud/agent/workspaces.md.
> API browser recording defaults to off. Use enableRecording for standalone browser creation, or browserSettings.record for an agent run. Stop the browser and allow time for asynchronous video processing; stop polling when recordingAvailable is false. Live preview is for an active browser. Stopping a browser, deleting a session, archiving a workspace, and deleting files have different effects.
> Use model-specific reasoning values. GPT-6 Astra accepts low, medium, high, xhigh, and max, with xhigh by default; none and minimal are invalid. Use the public REST schema when installed SDK types lag new fields. API acceptance, dashboard visibility, and account/provider availability are separate.
> For open-source browser-use, is_done only reports a terminal done action. is_successful is the agent-reported outcome; verify important external actions independently. Cloud timeout, API client timeout, model timeout, and task completion are separate concepts.
> For failed requests, use https://docs.browser-use.com/cloud/guides/troubleshooting.md. Inspect the full error and project before retrying or adding credits. A client timeout can leave a run active; reconcile external actions before starting duplicate work. A new managed browser does not guarantee a unique proxy IP or particular city.

# Quick start

> Run a hosted agent or launch a cloud browser.

| المسار | الوصف |
|---|---|
| [Browser Use Agents](https://docs.browser-use.com/cloud/agent/quickstart) | أعطِ الوكيل مهمة واحصل على النتيجة. |
| [Browser Infrastructure](https://docs.browser-use.com/cloud/browser/quickstart) | شغّل متصفحًا سحابيًا وتصل إليه من كودك. |

Both Cloud paths use API V4. Choose whether Browser Use drives the browser or
your Playwright/Puppeteer code connects directly over CDP. For a local agent,
use the [open-source library](https://docs.browser-use.com/open-source/quickstart).

Get an [API key](https://cloud.browser-use.com/settings?tab=api-keys&new=1) and
export it:

```bash
export BROWSER_USE_API_KEY=your_key
```

## Install the SDK

Use Python 3.10 or newer for the Python SDK. Skip installation if you use curl.

```bash
pip install browser-use-sdk --upgrade
```

```bash
npm install browser-use-sdk@latest puppeteer-core
```

## Run Browser Use Agents

```python
from browser_use_sdk.v4 import BrowserUse

with BrowserUse() as client:
    run = client.runs.create("Find the top Hacker News story")
    result = client.runs.wait_for_completion(run.id)
    print(result.result)
```

```typescript
import { BrowserUse } from "browser-use-sdk/v4";

const client = new BrowserUse();
const run = await client.runs.create({
  task: "Find the top Hacker News story",
});
const result = await client.runs.waitForCompletion(run.id);
console.log(result.result);
```

```bash
curl https://api.browser-use.com/api/v4/runs \
  -H "X-Browser-Use-API-Key: $BROWSER_USE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"task":"Find the top Hacker News story"}'
```

## Use Browser Infrastructure

These examples connect to the managed browser's existing context and stop it
in `finally`, including when connecting or navigating fails.

```python
from browser_use_sdk.v4 import BrowserUse
from playwright.sync_api import sync_playwright

with BrowserUse() as client:
    session = client.browsers.create(proxy_country_code="us")
    try:
        if not session.cdp_url:
            raise RuntimeError("The browser did not return a CDP URL")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(session.cdp_url)
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://example.com")
            print(page.title())
    finally:
        client.browsers.stop(session.id)
```

```typescript
import { BrowserUse } from "browser-use-sdk/v4";
import puppeteer from "puppeteer-core";

const client = new BrowserUse();
const session = await client.browsers.create({ proxyCountryCode: "us" });
try {
  if (!session.cdpUrl) throw new Error("The browser did not return a CDP URL");
  const browser = await puppeteer.connect({ browserWSEndpoint: session.cdpUrl });
  try {
    const context = browser.defaultBrowserContext();
    const page = (await context.pages())[0] ?? await context.newPage();
    await page.goto("https://example.com");
    console.log(await page.title());
  } finally {
    await browser.disconnect();
  }
} finally {
  await client.browsers.stop(session.id);
}
```

For curl and additional connection options, follow the
[Browser Infrastructure quickstart](https://docs.browser-use.com/cloud/browser/quickstart#launch-and-connect).
