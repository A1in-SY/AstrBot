# 网页搜索

网页搜索功能旨在为大模型提供联网检索能力，以获取最近信息，一定程度上能够提高回复准确度，减少幻觉。

AstrBot 内置的网页搜索功能依赖大模型提供 `函数调用` 能力。如果你不了解函数调用，请参考：[函数调用](/use/function-calling.html)。

在使用支持函数调用的大模型且开启了网页搜索功能的情况下，您可以试着说：

- `帮我搜索一下 xxx`
- `帮我总结一下这个链接：https://soulter.top`
- `查一下 xxx`
- `最近 xxxx`

等等带有搜索意味的提示让大模型触发调用搜索工具。

AstrBot 当前支持 6 种网页搜索源接入方式：`Tavily`、`BoCha`、`百度 AI 搜索`、`Brave`、`Firecrawl`、`Exa`。

![image](https://files.astrbot.app/docs/source/images/websearch/image.png)

进入 `配置`，下拉找到网页搜索，您可选择 `Tavily`、`BoCha`、`百度 AI 搜索`、`Brave`、`Firecrawl` 或 `Exa`。

### 自定义 API 地址（Base URL）

每个提供商都支持可选的自定义 API Base URL，用于通过代理或网关转发请求，例如使用 Tavily Hikari 管理多个 Tavily API Key。留空时使用官方地址；填写后，请求会在该地址后拼接原有路径（如 `/search`、`/extract`），调用逻辑不变。

以 Tavily Hikari 为例：在 Hikari 控制台创建访问令牌 `th-...`，然后在 AstrBot 的网页搜索配置中：

- `Tavily API Base URL` 填写 Hikari 的 HTTP 地址，例如 `http://127.0.0.1:58087/api/tavily`；
- `Tavily API Key` 填写 Hikari 签发的访问令牌 `th-<id>-<secret>`，而不是官方 Tavily Key。

上游的多个 Tavily API Key 轮换与配额管理由 Hikari 负责。

### Tavily

前往 [Tavily](https://app.tavily.com/home) 得到 API Key，然后填写在相应的配置项。

### BoCha

前往 BoCha 平台获取 API Key，然后填写在相应的配置项。

### 百度 AI 搜索

前往百度千帆 APP Builder 获取 API Key，然后填写在相应的配置项。

### Brave

前往 Brave Search 获取 API Key，然后填写在相应的配置项。

### Firecrawl

前往 [Firecrawl](https://firecrawl.dev) 获取 API Key，然后填写在相应的配置项。

### Exa

前往 [Exa](https://dashboard.exa.ai) 获取 API Key，然后填写在相应的配置项。Exa 是一个 AI 原生搜索引擎，支持关键词和语义搜索，提供分类过滤、域名限制和日期范围等高级搜索功能。

如果您使用 Tavily 作为网页搜索源，在 AstrBot ChatUI 上将会获得更好的体验优化，包括引用来源展示等：

![](https://files.astrbot.app/docs/source/images/websearch/image1.png)
