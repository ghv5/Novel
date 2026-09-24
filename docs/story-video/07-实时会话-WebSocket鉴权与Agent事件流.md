# 07 实时会话：WebSocket 鉴权与 Agent 事件流

> 标签：WebSocket / STOMP / 握手鉴权 / 事件推送 / 会话隔离
>
> 证据根：`story-video-agent/src/main/java/io/binghe/framework/ai/agent/{websocket,core,emitter}/**`

---

## 1. 场景与边界

Agent 的生成是**长过程 + 流式输出**：一句话进去，模型可能边想边吐字、中间还要调用多个工具、再派子 Agent 干活。用 HTTP 轮询体验差，所以采用 **WebSocket + STOMP**：

1. **鉴权**：WebSocket 没有 HTTP Header 那么自然，token 怎么传？握手阶段如何校验？
2. **路由**：一条连接如何区分「哪个项目、哪种 Agent（小说/大纲/分镜/流水线）」？
3. **事件模型**：服务端要推流式文本、工具调用、子 Agent 流转、结束/错误、进度等；前端如何订阅？
4. **会话生命周期**：连接建立时加载历史、断开时保存、空闲时回收；
5. **隔离**：多个用户/多个项目互不串台。

边界：
- 本文聚焦「连接 → 鉴权 → 会话 → 事件推送」。
- Agent 的工具循环与子 Agent 编排见第 05 篇。
- 流水线自身的阶段事件也走同一套主题（见第 06 篇）。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| 握手期鉴权 | STOMP 的 `CONNECT` 前有 HTTP Upgrade，必须在握手拦截器里校验 token 并写入 session attributes |
| 路径即路由 | 端点用 path variable 携带 agentType/projectId，拦截器需解析并存 attributes |
| 事件分类 | 流式文本、工具调用、流转、进度、结束、错误要有统一的 `type` |
| 单实例 vs 多实例 | 简单内存 broker 无法跨实例广播 |
| 会话隔离 | 会话键必须能区分用户/项目 |
| 历史持久化 | 连接/断开时把对话历史落库/加载 |
| 内存泄漏 | 长时间不活动的 Agent 要及时回收 |

---

## 3. 实现链路

### 3.1 端点与订阅模型

```
客户端连接（带 token）：/ws/agent/novel/{projectId}?token=xxx
                       /ws/agent/outline/{projectId}?token=xxx
                       /ws/agent/storyboard/{projectId}?token=xxx&scriptId=yyy
                       /ws/pipeline/{projectId}?token=xxx

发送消息（SEND，前缀 /app）：/app/agent/{agentType}/{projectId}/msg
清空历史（SEND）：          /app/agent/{agentType}/{projectId}/cleanHistory
订阅事件（SUBSCRIBE）：     /topic/agent/{agentType}/{projectId}/event
```

消息只接受 `payload.data` 一个字段（非空才处理）。

### 3.2 握手拦截：鉴权 + 路由解析

`JwtHandshakeInterceptor.beforeHandshake`：

1. 取 query 参数 `token`；
2. `jwtTokenProvider.validateToken(token)`（含 Redis 单会话校验，见第 01 篇）；
3. 通过则 `attributes.put("userId", ...)`；
4. `parsePathAttributes(path, attributes)`：`/ws/agent/{type}/{projectId}` → `agentType` + `projectId`；`/ws/pipeline/{projectId}` → `agentType="pipeline"`；
5. 可选 `scriptId`（分镜 Agent 需要）；
6. 返回 `true` 放行，否则 `return false` 拒绝握手。

### 3.3 会话生命周期

```
SessionConnectedEvent
  └─ 读 attributes（agentType/projectId/scriptId）
     └─ 按 agentType 选工厂：outline/novel/storyboard → getOrCreate(projectId, agentType, factory)
        └─ 其它 → get(projectId, agentType)（如 pipeline，通常为 null）
           └─ agent.onConnect()  → loadHistory()（t_chat_history）

SessionDisconnectEvent
  └─ agent.onDisconnect() → saveHistory()
     └─ agentSession.remove(projectId, agentType)  ← 内部还会再调一次 onDisconnect

@Scheduled(fixedDelay=300_000) evictIdle()
  └─ 清理「空闲 > 30 分钟且 IDLE」的 Agent
```

### 3.4 事件类型（`AgentEmitter`）

主题固定 `/topic/agent/{agentType}/{projectId}/event`，消息体 `{type, data}`：

| type | 方法 | 含义 |
| --- | --- | --- |
| `stream` | `stream(text)` | 主 Agent 流式文本 |
| `response_end` | `responseEnd(text)` | 主 Agent 结束 |
| `sub_agent_stream` | `subAgentStream(type,text)` | 子 Agent 流式文本 |
| `sub_agent_end` | `subAgentEnd(type)` | 子 Agent 结束 |
| `tool_call` | `toolCall(type,name,args)` | 工具调用 |
| `transfer` | `transfer(name)` | 流转到子 Agent |
| `layer_start` / `chapter_start` / `progress` | 同名方法 | 进度类事件 |
| `error` | `error(msg)` | 错误 |

### 3.5 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| STOMP 配置 | `.../agent/websocket/WebSocketConfig.java:33-55` | `enableSimpleBroker("/topic")`、app 前缀 `/app`、4 个端点 |
| 握手鉴权 | `WebSocketConfig.java:57-100` | `JwtHandshakeInterceptor`、取 token、`validateToken` |
| 路径解析 | `WebSocketConfig.java:90-100` | `parsePathAttributes` |
| 跨域 | `WebSocketConfig.java:45,48,51,54` | `setAllowedOriginPatterns("*")` |
| 消息处理 | `.../websocket/AgentWebSocketHandler.java:29-45` | `/agent/{agentType}/{projectId}/msg` |
| 清历史 | `AgentWebSocketHandler.java:47-55` | `/cleanHistory` |
| 连接事件 | `.../websocket/WebSocketEventListener.java:34-70` | 按 agentType 选工厂，`onConnect` |
| 断开事件 | `WebSocketEventListener.java:72-89` | `onDisconnect` + `remove` |
| 会话容器 | `.../agent/core/AgentSession.java:22-92` | 键 `agentType:projectId`、30 分钟空闲回收 |
| 会话键 | `AgentSession.java:76-78` | `key(projectId, agentType) = agentType + ":" + projectId` |
| 回收 | `AgentSession.java:57-74` | `@Scheduled(fixedDelay=300000)` `evictIdle` |
| 事件发送 | `.../agent/emitter/AgentEmitter.java:21-86` | `topic()` + 各类事件 |
| Agent 历史 | `.../agent/base/BaseAgent.java:236-247,284-...` | `onConnect`/`onDisconnect`/`loadHistory` |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| STOMP over WebSocket | 有「订阅/发布」语义，前端 `@stomp/stompjs` 易用 | 比裸 WS 重 |
| 握手期校验 token | 复用第 01 篇 JWT + Redis 单会话，登出即断 | token 走 query 有泄露风险（见 §6.2） |
| 路径变量携带 agentType/projectId | 路由信息随连接一次确定 | 拦截器需解析 path |
| `enableSimpleBroker` | 零依赖、开箱即用 | **单实例**，不能水平扩展 |
| 会话键 `agentType:projectId` | 同项目同类型 Agent 可复用 | **未含 userId**，存在跨用户串台（见 §6.1） |
| 30 分钟空闲回收 | 防内存泄漏 | 回收期间对话上下文丢失（但历史已落库） |

---

## 5. 关键工程实现

**（1）握手鉴权（`WebSocketConfig.java:59-82`）**

```java
String token = servletRequest.getServletRequest().getParameter("token");
if (token != null && jwtTokenProvider.validateToken(token)) {
    Long userId = jwtTokenProvider.getUserIdFromToken(token);
    attributes.put("userId", userId);
    parsePathAttributes(request.getURI().getPath(), attributes);   // agentType/projectId
    String scriptIdStr = servletRequest.getServletRequest().getParameter("scriptId");
    if (scriptIdStr != null) { try { attributes.put("scriptId", Long.parseLong(scriptIdStr)); } catch (NumberFormatException ignored) {} }
    return true;
}
log.warn("WebSocket handshake rejected: invalid or missing token");
return false;
```

**（2）消息入口（`AgentWebSocketHandler.java:29-45`）**

```java
@MessageMapping("/agent/{agentType}/{projectId}/msg")
public void handleMessage(@DestinationVariable String agentType, @DestinationVariable Long projectId,
                          @Payload Map<String, Object> payload) {
    BaseAgent agent = agentSession.get(projectId, agentType);
    if (agent == null) {
        messagingTemplate.convertAndSend("/topic/agent/" + agentType + "/" + projectId + "/event",
                Map.of("type", "error", "data", "Agent实例不存在，请刷新页面重新连接"));
        return;
    }
    String message = (String) payload.get("data");
    if (message != null && !message.isBlank()) agent.onMessage(message);
}
```

**（3）会话键与回收（`AgentSession.java:28-78`）**

```java
public BaseAgent getOrCreate(Long projectId, String agentType, Supplier<BaseAgent> factory) {
    AgentEntry entry = agents.computeIfAbsent(key(projectId, agentType), k -> new AgentEntry(factory.get()));
    entry.touch();
    return entry.agent;
}
private String key(Long projectId, String agentType) { return agentType + ":" + projectId; }
```

**（4）事件构造（`AgentEmitter.java:21-23`）**：`"/topic/agent/" + agentType + "/" + projectId + "/event"`。

---

## 6. 踩坑根因排障

### 6.1 【代码事实】会话键不含 userId → 跨用户串台

```java
private String key(Long projectId, String agentType) { return agentType + ":" + projectId; }
```
键里**只有 agentType + projectId，没有 userId**。而 projectId 是自增主键，不同用户完全可能访问同一个 projectId（或恶意构造）。后果：

- 用户 A 与用户 B 同时连 `/ws/agent/novel/100`，`getOrCreate` 命中**同一个 `BaseAgent` 实例**；
- 该实例对话历史、`state`、`MessageQueue` 全部共享 → **B 能看到 A 的对话、B 的消息会打断 A 的执行**（`onMessage` 的 `synchronized` + 队列）。

**修复方向**：会话键加入 userId（`userId:agentType:projectId`），并在 `handleMessage`/订阅时校验 attributes 里的 userId 与项目归属。

### 6.2 【代码事实】token 走 URL query，存在泄露风险

`?token=xxx` 会出现在浏览器历史、代理/网关访问日志、Referer 里。更稳妥的是用 STOMP `CONNECT` 帧的 header 或一次性 ticket。当前实现可接受，但需注意日志脱敏。

### 6.3 【代码事实】`enableSimpleBroker` 只能单实例

`registry.enableSimpleBroker("/topic")` 是进程内 broker，无法把消息广播到其它实例。**多实例部署时，订阅到另一实例的用户收不到事件**。需要换 `enableStompBrokerRelay`（外部 broker：RabbitMQ/ActiveMQ/Redis relay）。

### 6.4 【代码事实】`setAllowedOriginPatterns("*")` 放开所有来源

四个端点都允许任意 Origin 建立连接（配合 §6.2 的 token-in-query，风险叠加）。生产应白名单化。

### 6.5 【代码事实】断开时历史保存两次

`handleSessionDisconnect` 先 `agent.onDisconnect()`，随后 `agentSession.remove(...)` 内部**又调一次** `entry.agent.onDisconnect()`（`AgentSession.java:44-49`）。即每次断开写两次历史库。功能上幂等尚可，但产生冗余写。

### 6.6 【代码事实】未配 `scriptId` 的分镜连接不会建 Agent

`storyboardAgentFactory.create(projectId, scriptId)` 需要 scriptId；缺失时只 `log.warn` 且 `agent = null`，随后 `agent.onConnect()` 被跳过。客户端会收到「Agent实例不存在」，但错误原因（缺 scriptId）不会显式回传。

### 6.7 【代码事实】`evictIdle` 只回收 `IDLE` 的 Agent

若某 Agent 因异常卡在 `RUNNING`（例如工具循环未回到 IDLE、或 §05 的 pause 问题），则**永远不会被回收**，长期占用内存。

### 6.8 常见现象对照表

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| 别的用户的消息串进来 | 会话键无 userId（§6.1） | 会话键加 userId + 校验项目归属 |
| 多实例下部分用户收不到事件 | `enableSimpleBroker`（§6.3） | 换外部 broker relay |
| 连上就报「Agent实例不存在」 | 缺 scriptId 或未建立会话（§6.6） | 带上 scriptId；先连端点 |
| 内存持续上涨 | 卡在 RUNNING 不被回收（§6.7） | 加 RUNNING 超时强制回收 |
| 发消息无反应 | 未按 `/app/agent/{type}/{projectId}/msg` 发送或 `data` 字段名不对 | 对齐目的地址与 payload 结构 |
| 握手被拒 | token 失效/未带/Redis 单会话已被顶 | 见第 01 篇单会话失效 |

---

## 7. 可迁移落地方案

`【落地建议】`

1. **握手期完成鉴权**：把用户身份写进 session attributes，后续所有消息都从 attributes 取身份，**不要信任客户端 payload 里的 userId**。
2. **会话键包含租户/用户维度**：`userId:agentType:projectId`，并在每次消息处理时校验「该会话的 userId == 当前连接 userId」。
3. **每类事件有明确 `type`**：前端按 type 分发渲染；错误也要走同一主题（`type=error`），避免另开通道。
4. **单实例用内存 broker，多实例必须换 relay**；同时事件主题命名带项目维度，天然支持按项目订阅。
5. **历史持久化放在连接/断开钩子**，并保证幂等（避免重复写）。
6. **空闲 + 异常双回收**：既回收 IDLE 超时，也要对长期 RUNNING 加熔断。
7. **token 不要放 URL**：优先 STOMP CONNECT header 或短时 ticket；若必须放 query，务必日志脱敏 + 限制 Origin。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| STOMP + 内存 broker（本方案） | 单实例、需求标准 | 多实例、超高并发 |
| STOMP + 外部 relay | 多实例、需要广播 | 运维成本上升 |
| 原生 WebSocket + 自研协议 | 极致轻量/定制 | 需自建订阅模型 |
| SSE | 只需服务端单向推送 | 需要双向交互 |
| gRPC streaming | 内部服务间 | 浏览器不友好 |

---

## 9. 验收清单

- [ ] 无 token/无效 token 握手被拒。
- [ ] 有效 token 能建立连接并写入 userId/agentType/projectId/scriptId。
- [ ] 按 agentType 正确创建 novel/outline/storyboard Agent。
- [ ] 消息经 `/app/agent/{type}/{projectId}/msg` 到达 `onMessage`。
- [ ] 事件按 `{type,data}` 推送到 `/topic/agent/{type}/{projectId}/event`。
- [ ] 连接加载历史、断开保存历史（且幂等）。
- [ ] 空闲 30 分钟被回收；卡住的 RUNNING 有超时兜底。
- [ ] **跨用户/跨项目不会串台**（会话键含 userId 并校验）。
- [ ] 多实例部署时事件可达（用 relay）。

---

## 10. 待确认项

- 会话键不含 userId 是否有上游网关做了项目级隔离来兜底。【待确认】
- 是否真的存在多实例部署场景（决定 §6.3 的严重度）。【待确认】
- `setAllowedOriginPatterns("*")` 是否计划收紧。【待确认】
- 前端对 `sub_agent_*` / `progress` / `layer_start` 等事件的消费情况。【待确认】
- `pipeline` 类连接是否需要 Agent（当前 `get` 返回 null）。【待确认】
