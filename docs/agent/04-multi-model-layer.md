# 04 · 多模型层：30+ Provider 的统一抽象

> 如何用一套接口归一化 OpenAI / Anthropic / Google / Bedrock 等 10 种协议、30+ 模型提供商？

源码：`packages/ai/src/`（`providers/`、`api/`、`types.ts`、`models.ts`、`utils/event-stream.ts`）。

## 4.1 三层架构

```
┌────────────────────────────────────────────────────────────────┐
│  Provider 层 (30+ 业务方)                                       │
│  openai / anthropic / google / bedrock / azure / ...            │
│  每个 provider = 一个轻量工厂: id + name + baseUrl + auth       │
│                 + models 目录 + api 协议实现                    │
├────────────────────────────────────────────────────────────────┤
│  API 协议层 (10 种协议)                                          │
│  openai-responses / openai-completions / anthropic-messages    │
│  google-generative-ai / google-vertex / bedrock-converse-stream│
│  mistral-conversations / azure-openai-responses / ...          │
│  负责 SSE/流式解析，把原生 toolCall 归一化到统一 ToolCall 类型    │
├────────────────────────────────────────────────────────────────┤
│  统一契约层                                                      │
│  Model<TApi> + StreamFunction + AssistantMessageEventStream    │
│  统一 Message / ContentBlock / ToolCall / AssistantMessage 类型  │
└────────────────────────────────────────────────────────────────┘
```

**关键点**：30+ provider 映射到仅 10 种协议。很多 provider 复用同一协议（如 OpenAI 兼容接口可被 deepseek、openrouter、groq 等共享 `openai-responses` 协议）。

## 4.2 Provider 工厂范式

```typescript
// providers/openai.ts (全文 16 行)
export const openaiProvider = createProvider({
    id: "openai",
    name: "OpenAI",
    baseUrl: "...",
    auth: { apiKey: envApiKeyAuth("OPENAI_API_KEY") },
    models: OPENAI_MODELS,
    api: openAIResponsesApi(),
});
```

`createProvider<TApi>`（`models.ts` L784）是统一的装配工厂，组合：
- `id` / `name`：标识
- `baseUrl`：服务地址
- `auth`：认证（API Key / OAuth）
- `models`：模型目录（来自 `models.generated.ts` 生成）
- `api`：协议实现（懒加载）

`providers/all.ts` 的 `builtinProviders()` 汇总所有内置 provider。

## 4.3 懒加载（lazyApi）

```typescript
// api/openai-responses.lazy.ts (全文 5 行)
export const openAIResponsesApi = lazyApi(() => import("./openai-responses.ts"));
```

**动机**：避免所有 provider 在冷启动时全部载入。只有真正用到某协议时才动态 import 其实现模块，降低启动内存与时间开销。

## 4.4 统一流式事件模型

### AssistantMessageEventStream

```typescript
// utils/event-stream.ts
class EventStream<T, R> {
    push(event: T)   // 推送事件；若 isComplete 则 resolve 最终结果
    end()            // 结束流
    [Symbol.asyncIterator]()  // 支持 for-await 迭代
    result()         // 取最终结果 Promise
}
```

基于 FIFO 队列 + async iterator，实现 `AsyncIterable<AssistantMessageEvent>`。

### AssistantMessageEvent

```typescript
// types.ts L654
type AssistantMessageEvent =
    | { type: "start" }
    | { type: "text_start" | "text_delta" | "text_end" }
    | { type: "thinking_start" | "thinking_delta" | "thinking_end" }
    | { type: "toolcall_start" | "toolcall_delta" | "toolcall_end" }
    | { type: "done" }
    | { type: "error" }
    // 每个事件都携带 partial: AssistantMessage 实时快照
```

**统一性**：无论底层是 OpenAI 的 `function_call`、Anthropic 的 `tool_use`，还是 Google 的 `functionResponse`，最终都归一化为这套事件流，由 agent-loop 的 `streamAssistantResponse` 统一消费。

## 4.5 toolCall 跨 Provider 归一化

```typescript
// api/openai-responses-shared.ts (processResponsesStream)
// 解析 OpenAI SSE 流，根据 item.type 创建统一内容块：
//   function_call     → StreamingToolCall { type:"toolCall", id, name, arguments, partialJson }
//   custom_tool_call  → 带 customInput 的 toolCall
// 归一化目标（types.ts L381）:
//   ToolCall = { type:"toolCall", id, name, arguments: JsonObject, thoughtSignature?, namespace? }
```

`AssistantMessage.content` 是统一的内容块联合类型：

```typescript
// types.ts L491
content: (TextContent | ThinkingContent | ToolCall)[]
```

**核心思想**：协议差异被封闭在 `api/` 各实现里，向上层只暴露统一的 `ToolCall` / `TextContent` / `ThinkingContent`。agent-loop 完全不感知 provider 差异。

## 4.6 Compat 能力位

不同 provider 能力不同（是否支持 system role、mid-conversation system message、thinking 格式、缓存控制等）。`ModelCompat`（`types.ts` L695）用能力位开关归一化这些差异：

```typescript
supportsDeveloperRole      // 是否支持 developer 角色
supportsMidConvoSystemMessages  // 是否支持会话中插入 system 消息
thinkingFormat             // thinking 内容格式
cacheControlFormat         // 缓存控制格式
```

**作用**：同一份请求构造逻辑，根据模型的能力位动态适配参数，避免为每个 provider 写特殊分支。

## 4.7 已知协议与 Provider

### 10 种协议（KnownApi）

```
openai-completions / openai-responses / openai-codex-responses
anthropic-messages / google-generative-ai / google-vertex
bedrock-converse-stream / mistral-conversations
azure-openai-responses / pi-messages
```

### 30+ Provider（KnownProvider 节选）

```
openai / openai-codex / anthropic / google / google-vertex
amazon-bedrock / github-copilot / deepseek / mistral / xai / groq
cerebras / openrouter / vercel-ai-gateway / zai / qwen-token-plan
xiaomi / minimax / moonshotai / fireworks / together / baseten
huggingface / nvidia / meta / ant-ling / cloudflare-workers-ai
cloudflare-ai-gateway / opencode / ...
```

## 4.8 核心设计总结

| 设计决策 | 原因 |
|---------|------|
| provider → api 两层 | 30+ provider 复用 10 种协议，降低实现数量 |
| createProvider 工厂 | 统一装配，新增 provider 只需一个轻量对象 |
| lazyApi 懒加载 | 降低冷启动开销 |
| 统一事件流 | agent-loop 不感知 provider，单一消费逻辑 |
| toolCall 归一化 | 协议差异封闭在 api 层 |
| compat 能力位 | 一份请求逻辑适配不同模型能力 |

## 延伸阅读

- 02 文：agent-loop 如何消费 AssistantMessageEventStream（`for await` + partialMessage 快照）
- 03 文：归一化后的 ToolCall 如何被工具系统执行
