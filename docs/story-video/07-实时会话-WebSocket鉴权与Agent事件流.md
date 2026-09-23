# 07 实时会话：WebSocket（STOMP）鉴权与 Agent 事件流

> 标签：WebSocket / STOMP / 握手鉴权 / 事件推送 / 会话生命周期

---

## 1. 场景与边界

Agent 生成是**流式、长时**过程，前端需要实时看到：流式文本、工具调用、子 Agent 过程、章节进度、分层加载等。为此平台使用 **WebSocket + STOMP**：

- 握手阶段完成 token 鉴权与路径参数解析；
- 连接建立后，由 `SessionConnectedEvent` 懒创建对应 Agent；
- Agent 通过 `AgentEmitter` 把事件推送到 `/topic/agent/{agentType}/{projectId}/event`；
- 空闲 30 分钟自动回收 Agent，防内存泄漏。

边界：本场景讲连接与事件通道；工具循环见第 05 篇，流水线事件见第 06 篇。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| WS 不能自定义鉴权头 | 只能用 query 参数 `token` |
| 路径即上下文 | `agentType`/`projectId`/`scriptId` 需在握手时解析并写入 session |
| 事件类型多 | 流式、工具、子 Agent、章节、进度…… 需统一信封 |
| 会话生命周期 | 断开要清理；长期空闲要回收 |
| 多入口复用 | outline/novel/storyboard/pipeline 共用一套机制 |

---

## 3. 实现链路

### 3.1 连接 → 事件

```
客户端: ws://host/ws/agent/novel/{projectId}?token=xxx
  → JwtHandshakeInterceptor.beforeHandshake
      validateToken(token) → attributes{userId}
      parsePathAttributes(path) → attributes{agentType, projectId}
      (可选) scriptId
  → 连接建立 → SessionConnectedEvent
      → WebSocketEventListener.handleSessionConnected
          agentSession.getOrCreate(projectId, agentType, factory)
  → 客户端发送消息 → @MessageMapping → AgentWebSocketHandler.handleMessage
      agentSession.get(projectId, agentType).onMessage(...)
  → Agent 产出 → AgentEmitter.send → /topic/agent/{type}/{projectId}/event
  → 断开 → SessionDisconnectEvent → agentSession.remove(...)
```

### 3.2 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| 端点注册 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/websocket/WebSocketConfig.java:44-53` | `/ws/agent/outline|novel|storyboard/{projectId}`、`/ws/pipeline/{projectId}` |
| 握手拦截 | `WebSocketConfig.java:57-82` | `JwtHandshakeInterceptor` |
| token 校验 | `WebSocketConfig.java:63-66` | query 参数 `token` → `validateToken` → `userId` |
| 路径解析 | `WebSocketConfig.java:90-100` | `parsePathAttributes` |
| 消息处理 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/websocket/AgentWebSocketHandler.java:25-51` | `handleMessage` / `handleCleanHistory` |
| 取 Agent | `AgentWebSocketHandler.java:34` | `agentSession.get(projectId, agentType)` |
| 连接事件 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/websocket/WebSocketEventListener.java:35-72` | `handleSessionConnected` |
| 懒创建 | `WebSocketEventListener.java:49-57` | `agentSession.getOrCreate(...)` |
| 断开事件 | `WebSocketEventListener.java:73-89` | `handleSessionDisconnect` → `agentSession.remove` |
| 事件发射 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/emitter/AgentEmitter.java:16-86` | 主题与各事件方法 |
| 主题规则 | `AgentEmitter.java:22-23` | `/topic/agent/{agentType}/{projectId}/event` |
| 事件信封 | `AgentEmitter.java:85-86` | `convertAndSend(topic, {type, data})` |
| 会话管理 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/core/AgentSession.java:22-80` | `@Component`、`getOrCreate/get/remove/size` |
| 空闲回收 | `AgentSession.java:25,59-74` | `MAX_IDLE_MILLIS = 30min`，`@Scheduled(fixedDelay=300000)` |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| STOMP over WebSocket | 有主题/订阅语义，客户端库成熟 | 比裸 WS 重 |
| token 走 query | 浏览器限制 | token 进 URL 需脱敏 |
| 连接时懒创建 Agent | 避免无连接也占资源 | 连接即创建可能浪费 |
| `AgentSession` + 定时回收 | 简单有效防泄漏 | 仅单实例；多实例需共享 |
| 统一 `{type,data}` 信封 | 前端好处理 | 类型字符串需前后端约定 |

---

## 5. 关键工程实现

**（1）握手一次性解析上下文（`WebSocketConfig.java:62-77`）**：鉴权与路径解析都在握手完成，后续事件直接用 session 属性，无需重复解析。【代码事实】

**（2）事件类型覆盖全流程（`AgentEmitter.java`）**
```
stream / response_end / subAgentStream / subAgentEnd / toolCall / transfer / refresh / error
layerStart / layerComplete / chapterStart / chapterDelta / chapterEnd / progress
```
【代码事实】前端据此渲染流式文本、工具轨迹、子 Agent 面板、章节进度。

**（3）空闲回收（`AgentSession.java:59-74`）**：每 5 分钟扫描，回收「空闲超 30 分钟且状态为 IDLE」的 Agent。【代码事实】

---

## 6. 踩坑根因排障

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| 连接立刻被拒 | 缺 `token` 或 token 失效 | 对照 `WebSocketConfig.java:63-64` |
| 收到事件但订阅不到 | 主题不匹配 | 对照 `AgentEmitter.java:22` |
| 多用户收到彼此事件 | 主题未按用户隔离，仅按 projectId | 需在主题中加 userId 或做服务端订阅校验 |
| Agent 内存泄漏 | 回收条件苛刻（需 `IDLE`） | 检查 `state` 是否长期非 IDLE |
| 断开后重连无 Agent | `remove` 了但未重建 | `SessionConnectedEvent` 会重新 `getOrCreate` |
| `scriptId` 丢失 | 未走 query 参数 | 对照 `WebSocketConfig.java:72-75` |

---

## 7. 可迁移落地方案

`【落地建议】`

1. **握手鉴权 + 上下文解析**：所有鉴权、路径参数在 `HandshakeInterceptor` 一次搞定。
2. **连接事件驱动建会话**：用 `SessionConnectedEvent`/`SessionDisconnectEvent` 管理生命周期，而非在 handler 里手工建。
3. **统一事件信封**：`{type, data}`，类型集中定义，前端按 type 分发。
4. **会话回收**：空闲超时 + 状态门控定时清理。
5. **订阅安全**：若数据敏感，主题应按用户隔离或在 STOMP 订阅拦截器校验 `projectId` 归属。
6. **水平扩展**：多实例需引入 Redis/STOMP broker relay，否则事件只在本实例广播。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| STOMP over WS（本方案） | 需要主题/订阅、Spring 生态 | 极简推送 |
| 裸 WebSocket | 自定义协议、低开销 | 需自研路由 |
| SSE | 只需服务端单向流 | 需双向 |
| 长轮询 | 兼容性优先 | 实时性差 |

---

## 9. 验收清单

- [ ] 无/错 token 握手被拒。
- [ ] 握手成功后 session 含 `userId`/`agentType`/`projectId`（视路径）。
- [ ] 连接建立后 Agent 被创建，断开后被移除。
- [ ] 各类事件按 `{type,data}` 推送到正确主题。
- [ ] 空闲 30 分钟且 IDLE 的 Agent 被回收。
- [ ] 多用户事件不串（需确认主题隔离策略）。

---

## 10. 待确认项

- 主题是否做了用户级隔离（当前仅 `agentType/projectId`）。【待确认】
- STOMP 是否配置了外部 broker relay（多实例扩展）。【待确认】
- `@MessageMapping` 的完整消息协议与鉴权（连接后是否再校验）。【待确认】
- 前端心跳/断线重连策略。【待确认】
