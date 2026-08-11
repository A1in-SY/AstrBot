# 脚本定时任务

脚本定时任务（Script Task）是 AstrBot 的第三种定时任务类型。它把机械化、可确定的流程（例如“每 5 分钟查一次金价，低于 3900 时提醒我”）固化为一段受限 Python 脚本，由一次性隔离 Worker 用 AST 解释器执行。**定时触发过程完全不经过 LLM**，不消耗 Token，也不依赖 Agent 工具。

> 需要未来继续推理、理解自然语言、调用任意工具或根据上下文作答的任务，请继续使用 [主动型 Agent 能力](./proactive-agent.html)。

## 启用与授权

脚本任务默认关闭（fail-closed）。要启用：

1. 打开 **设置 → 安全 → 脚本定时任务**。
2. 打开“启用”。
3. 在“允许的会话（UMO）”中精确加入目标会话，例如 `aiocqhttp:GroupMessage:123456`。
4. 按需调整单次执行硬超时与源码大小/AST 限制。

白名单使用精确 UMO，不支持通配符；留空表示全部拒绝。只有当前会话在白名单中时，对话里才会出现 `script_task` 工具。

## 通过对话创建

在普通对话中向 AstrBot 描述需求即可。AstrBot 会判断该任务是否可固化：

- 需要 LLM 推理 → 使用 `future_task`。
- 机械且可确定 → 使用 `script_task`，一次完成创建并启用，不需要额外确认。

示例对话：

```text
帮我每 5 分钟查一次金价，只在低于 3900 时提醒我
```

LLM 会生成类似下面的脚本并创建定时任务：

```python
import decimal

response = await ctx.http.request(
    "GET", "https://example.com/gold", use_proxy=False
)
price = decimal.Decimal(str(response.json()["price"]))

if price < decimal.Decimal("3900"):
    last = ctx.state.get("last_alert_price")
    if last != str(price):
        await ctx.send_text(f"金价已低于 3900：{price}")
        ctx.state["last_alert_price"] = str(price)
```

创建时会做完整静态校验；校验失败会把全部诊断返回给 LLM，LLM 可当场修正后重试，不会写入数据库。

## 语言与运行时

`astrbot-python-subset/v1` 是受限 Python 方言，由 AST 解释器执行，不是 CPython `exec`：

- 脚本正文即任务本体，模块顶层预绑定 `ctx`，支持顶层 `await`。
- 允许顶层 `def` / `async def` 辅助函数；async 辅助函数必须直接 `await`。
- 禁止：原生模块导入（白名单外）、`exec/eval/compile`、文件读写、AstrBot Tool、任意发送目标、`lambda/class/yield`、装饰器、注解。
- 每次执行都有硬超时；超时、进程被杀、协议错误时直接判定失败。
- 不做内存限制；极端脚本可能触发 OOM，需要管理员事后排查。

### 允许的导入

`datetime`、`time`、`zoneinfo`、`math`、`decimal`、`json`、`re`、`base64`、`hashlib`、`hmac`、`urllib.parse`。点分模块必须显式别名，例如 `import urllib.parse as urlparse`。`hashlib.file_digest` 等成员明确不可用。

### ctx API

| 成员 | 说明 |
| --- | --- |
| `ctx.run.job_id / run_id / started_at / timezone` | 本次运行只读元数据 |
| `ctx.state` | 持久化 JSON 状态（仅字符串键；成功结束才提交） |
| `ctx.send_text(text)` | 向任务绑定的会话发送纯文本 |
| `ctx.http.request(...)` | HTTP 请求（见下） |

`ctx.http.request(method, url, params=None, headers=None, content=None, json=None, data=None, timeout_seconds=None, follow_redirects=False, use_proxy=True)`：

- 任意 method/URL 均允许，包括 localhost、局域网与内网服务。
- 4xx/5xx 作为普通响应返回，不自动抛错。
- 重定向默认不跟随（`follow_redirects=False`）。
- `use_proxy=True` 时使用 AstrBot 全局 `http_proxy/no_proxy` 快照；`False` 时强制直连（忽略环境变量）。
- 响应只暴露 `status`、`headers`、`text`、`url` 和 `json()`。

## 状态、失败与通知

- 脚本成功结束时状态才提交；异常、超时、崩溃都不提交。
- 已发送的消息与已完成的 HTTP 不参与事务，因此语义是 at-least-once，不是 exactly-once。
- 不自动重试，不新增失败通知；沿用现有 Cron 失败记录逻辑。
- 同一任务单进程内不会并发执行；手动“立即执行”与定时触发冲突时，手动执行返回 409。

## 管理

Dashboard 的 **定时任务** 页支持创建/编辑/启停/删除/立即执行、源码校验、查看与重置状态、语言版本迁移。创建时选择“脚本任务”，编辑源码后可点击“校验”查看 Monaco 内联诊断。

语言版本是显式字段，未来版本变化需在 Dashboard 显式迁移；迁移会保留状态，但不会自动改写源码。
