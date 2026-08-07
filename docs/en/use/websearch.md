
# Web Search

The web search feature gives large language models internet retrieval capability for recent information, which can improve response accuracy and reduce hallucinations to some extent.

AstrBot's built-in web search functionality relies on the large language model's `function calling` capability. If you're not familiar with function calling, please refer to: [Function Calling](/en/use/function-calling.html).

When using a large language model that supports function calling with the web search feature enabled, you can try saying:

- `Help me search for xxx`
- `Help me summarize this link: https://soulter.top`
- `Look up xxx`
- `Recent xxxx`

And other prompts with search intent to trigger the model to invoke the search tool.

AstrBot currently supports 6 web search providers: `Tavily`, `BoCha`, `Baidu AI Search`, `Brave`, `Firecrawl`, and `Exa`.

![image](https://files.astrbot.app/docs/source/images/websearch/image.png)

Go to `Configuration`, scroll down to find Web Search, where you can select `Tavily`, `BoCha`, `Baidu AI Search`, `Brave`, `Firecrawl`, or `Exa`.

### Custom API Base URL

Each provider supports an optional custom API Base URL for routing requests through a proxy or gateway, for example using Tavily Hikari to manage multiple Tavily API keys. When left empty, the official endpoint is used. When set, requests append the original paths (such as `/search` and `/extract`) to that base URL; the calling logic is unchanged.

For example, with Tavily Hikari, create an access token `th-...` in the Hikari console, then configure AstrBot's web search:

- Set `Tavily API Base URL` to the Hikari HTTP endpoint, e.g. `http://127.0.0.1:58087/api/tavily`.
- Set `Tavily API Key` to the Hikari-issued access token `th-<id>-<secret>` instead of an official Tavily key.

Hikari handles upstream rotation and quota management across multiple Tavily API keys.

### Tavily

Go to [Tavily](https://app.tavily.com/home) to get an API Key, then fill it in the corresponding configuration item.

### BoCha

Get an API Key from the BoCha platform, then fill it in the corresponding configuration item.

### Baidu AI Search

Get an API Key from Baidu Qianfan APP Builder, then fill it in the corresponding configuration item.

### Brave

Get an API Key from Brave Search, then fill it in the corresponding configuration item.

### Firecrawl

Go to [Firecrawl](https://firecrawl.dev) to get an API Key, then fill it in the corresponding configuration item.

### Exa

Go to [Exa](https://dashboard.exa.ai) to get an API Key, then fill it in the corresponding configuration item. Exa is an AI-native search engine that supports keyword and semantic search with category filters, domain restrictions, and date ranges.

If you use Tavily as your web search source, you will get a better experience optimization on AstrBot ChatUI, including citation source display and more:

![](https://files.astrbot.app/docs/source/images/websearch/image1.png)
