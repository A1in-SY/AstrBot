# Script Tasks

Script Tasks are the third cron task type in AstrBot. They freeze mechanical, deterministic workflows (for example "check the gold price every 5 minutes and alert me only when it drops below 3900") into a restricted Python script executed by an AST interpreter in an isolated one-shot worker. **Scheduled execution never goes through the LLM**, consuming no tokens and no agent tools.

> If the task needs future reasoning, natural-language understanding, arbitrary tool calls, or context-dependent answers, keep using [Proactive Agent Tasks](./proactive-agent.html).

## Enabling and authorization

Script tasks are disabled by default (fail-closed). To enable them:

1. Open **Settings → Security → Script Tasks**.
2. Turn on **Enable**.
3. Add the target session exactly to the **Allowed sessions (UMO)** list, e.g. `aiocqhttp:GroupMessage:123456`.
4. Optionally tune the hard per-run timeout and the source/AST limits.

The allowlist uses exact UMOs; wildcards are not supported and an empty list denies everything. The `script_task` tool only appears in chat when the current session is allowlisted.

## Creating via chat

Just describe the requirement in a normal conversation. AstrBot decides whether the task can be frozen:

- Needs LLM reasoning → `future_task`.
- Mechanical and deterministic → `script_task`, created and enabled in one step with no extra confirmation.

Example:

```text
Check the gold price every 5 minutes and alert me only when it drops below 3900
```

The LLM generates a script similar to:

```python
import decimal

response = await ctx.http.request(
    "GET", "https://example.com/gold", use_proxy=False
)
price = decimal.Decimal(str(response.json()["price"]))

if price < decimal.Decimal("3900"):
    last = ctx.state.get("last_alert_price")
    if last != str(price):
        await ctx.send_text(f"Gold below 3900: {price}")
        ctx.state["last_alert_price"] = str(price)
```

Creation runs full static validation. On failure, every diagnostic is returned to the LLM so it can fix and retry in the same turn; nothing is persisted.

## Language and runtime

`astrbot-python-subset/v1` is a restricted Python dialect executed by an AST interpreter, not CPython `exec`:

- The script body is the task itself; `ctx` is pre-bound at module top level and top-level `await` is allowed.
- Top-level `def`/`async def` helpers are allowed; async helpers must be awaited directly.
- Forbidden: non-allowlisted imports, `exec/eval/compile`, file access, AstrBot tools, arbitrary send targets, `lambda/class/yield`, decorators, annotations.
- Every run has a hard timeout; timeout, kill, or protocol failure fails the run.
- No memory limit; pathological scripts can trigger OOM, which the administrator must investigate.

### Allowed imports

`datetime`, `time`, `zoneinfo`, `math`, `decimal`, `json`, `re`, `base64`, `hashlib`, `hmac`, `urllib.parse`. Dotted modules need an explicit alias, e.g. `import urllib.parse as urlparse`. Members such as `hashlib.file_digest` are explicitly unavailable.

### ctx API

| Member | Description |
| --- | --- |
| `ctx.run.job_id / run_id / started_at / timezone` | Read-only metadata for this run |
| `ctx.state` | Persistent JSON state (string keys only; committed only on success) |
| `ctx.send_text(text)` | Send plain text to the task's bound session |
| `ctx.http.request(...)` | HTTP request (see below) |

`ctx.http.request(method, url, params=None, headers=None, content=None, json=None, data=None, timeout_seconds=None, follow_redirects=False, use_proxy=True)`:

- Any method/URL is allowed, including localhost, LAN and internal services.
- 4xx/5xx are returned as normal responses; no automatic exceptions.
- Redirects are not followed by default (`follow_redirects=False`).
- `use_proxy=True` uses the AstrBot global `http_proxy/no_proxy` snapshot; `False` forces direct connection (ignores environment variables).
- The response only exposes `status`, `headers`, `text`, `url`, and `json()`.

## State, failures, and notifications

- State is committed only when the script finishes successfully; exceptions, timeouts, and crashes never commit it.
- Sent messages and completed HTTP requests are not transactional, so delivery is at-least-once, not exactly-once.
- No automatic retries and no new failure notifications; existing cron failure recording is reused.
- A job never runs concurrently in one process; when a manual "run now" conflicts with an active run it returns 409.

## Management

The **Cron** page in the Dashboard supports creating/editing, enabling/disabling, deleting, running now, source validation with inline Monaco diagnostics, viewing and resetting state, and explicit language-version migration. Choose **Script Task** when creating.

Language versions are explicit. Future versions require an explicit migration from the Dashboard; migration preserves state but never rewrites the source automatically.
