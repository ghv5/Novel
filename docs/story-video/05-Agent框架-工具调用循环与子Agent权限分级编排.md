# 05 Agent 框架：工具调用循环与子 Agent 权限分级编排

> 标签：Agent / Function Calling / 工具循环 / 子 Agent 编排 / 权限裁剪
>
> 证据根：`story-video-agent/src/main/java/io/binghe/framework/ai/agent/**`

---

## 1. 场景与边界

平台把「写小说 / 出大纲 / 写剧本 / 落分镜」等任务都建模成 **Agent**，每个 Agent 通过 **Function Calling（工具调用）** 与系统交互。核心需求：

1. **工具循环**：模型返回 `tool_calls` → 本地执行 → 把结果回填 → 继续问模型，直到模型不再调用工具；
2. **主/子 Agent 编排**：主 Agent 需要把「写某一章」「串联伏笔」等子任务交给子 Agent，子 Agent 完成后把结果返回主流程；
3. **权限分级**：子 Agent 拿到的工具集必须被裁剪，**防止子 Agent 反过来调用主 Agent**造成无限递归；
4. **并发与排队**：同一 Agent 实例同时只能跑一个任务，其余消息排队；
5. **可观测**：每次 LLM 调用、工具调用、子 Agent 调用都要落日志表（`t_agent_log`）。

边界：
- 本文聚焦 Agent 运行时骨架（`BaseAgent`）与子 Agent 编排。
- 具体的 `novelAgent` / `outlineAgent` / `storyboardAgent` 及其工具清单只在证据层面引用。
- WebSocket 事件推送与 `AgentSession` 生命周期见第 07 篇。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| Function Calling 循环终止 | 要区分「模型不再调工具」与「达到最大轮次」，后者不能被误判为成功 |
| 子 Agent 递归 | 子 Agent 的工具集若含 `callXxx`，会递归回主 Agent |
| 上下文膨胀 | 每轮工具结果都追加进 messages，20 轮后 token 爆炸 |
| 单实例并发 | 同一会话连续提问要串行，避免上下文交叉 |
| 暂停/取消 | 长循环里要能真正中断，而非只改状态标志 |
| 失败可观测 | LLM 调用失败要落库、要推给前端 |

---

## 3. 实现链路

### 3.1 一次对话的完整链路

```
onMessage(userMessage)
  ├─ synchronized(this)：若 state==RUNNING → messageQueue.enqueue(msg) 并返回（排队）
  │                     否则 state=RUNNING
  ├─ history.add(user(msg))
  ├─ executeWithToolLoop(history, toolRegistry, isSubAgent=false)
  │    └─ for i in 0..19:
  │         ├─ buildAiRequest(messages, tools)           ← 注入系统提示 + 工具定义
  │         ├─ provider = aiProviderService.getTextProvider(getAiFunctionKey())
  │         ├─ aiRetryTemplate.execute(() -> provider.invokeWithTools(request))
  │         ├─ logAction("llmCall", ...)
  │         ├─ 流式推送文本（主 Agent → emitter.stream；子 Agent → emitter.subAgentStream）
  │         ├─ 若 !response.hasToolCalls():
  │         │     messages.add(assistant(text)); emitter.responseEnd / subAgentEnd; return  ← 正常结束
  │         └─ 对每个 toolCall:
  │              ├─ tools.getTool(name)；null → 返回 "unknown tool"
  │              ├─ emitter.toolCall(...)（推前端）
  │              ├─ 解析 JSON 参数 → tool.execute(args)
  │              ├─ logAction("toolCall", ...)
  │              └─ messages.add(toolResult(id, result))
  │         （20 轮仍未结束 → log.warn，方法返回，无 completion 事件）
  └─ finally：synchronized 置 state=IDLE；processNextInQueue()
```

### 3.2 子 Agent 编排（权限裁剪的核心）

```
主 Agent 的工具集 = buildXxxTools(...)（业务工具）
                  + registerCallXxx(...)（“调用某子 Agent” 的入口工具）
子 Agent 的工具集 = 仅 buildXxxTools(...)（业务工具），不含任何 callXxx
```

- `invokeSubAgent(name, task, subSystemPrompt, subTools)`：推 `transfer` 事件 → 建子历史（system + user）→ `executeWithToolLoop(subHistory, subTools, true)` → 取**最后一条 assistant 消息**作为结果 → 落 `invokeSubAgent` 日志。
- 子 Agent 用 `isSubAgent=true`，流式走 `emitter.subAgentStream`，结束走 `emitter.subAgentEnd`。

### 3.3 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| 运行时骨架 | `story-video-agent/.../agent/base/BaseAgent.java:41-55` | 依赖：provider/retry/chatHistory/agentLog/checkpoint/emitter |
| 工具注册表 | `BaseAgent.java:74` | `new ToolRegistry()` |
| 注册钩子 | `BaseAgent.java:78-86` | `registerTools(...)`、`getAgentType()`、`getAiFunctionKey()` |
| 入口 | `BaseAgent.java:88-108` | `onMessage`，`synchronized` + `MessageQueue` 排队 |
| 工具循环 | `BaseAgent.java:110-202` | `executeWithToolLoop`，`maxIterations=20` |
| LLM 调用（带重试） | `BaseAgent.java:121-130` | `aiRetryTemplate.execute(() -> provider.invokeWithTools(...))` |
| 无工具结束 | `BaseAgent.java:146-155` | `responseEnd` / `subAgentEnd` |
| 工具执行 | `BaseAgent.java:165-198` | `getTool` → `execute` → `toolResult` 入 messages |
| 达上限 | `BaseAgent.java:201` | 仅 `log.warn`，不发完成事件 |
| 子 Agent | `BaseAgent.java:205-233` | `invokeSubAgent`（同步），取末条 assistant |
| 声明周期 | `BaseAgent.java:236-247` | `onConnect`/`onDisconnect`/`cleanHistory` |
| 中断控制 | `BaseAgent.java:250-263` | `pause`/`resume`/`cancel` |
| 队列消费 | `BaseAgent.java:275-282` | `processNextInQueue` |
| 历史加载 | `BaseAgent.java:284-...` | `loadHistory`（`t_chat_history`，按 projectId+type） |
| 工具注册表 | `.../agent/registry/ToolRegistry.java:18-37` | `register`/`getTool`/`getAllDefinitions`/`hasTool` |
| 工具接口 | `.../agent/tool/AgentTool.java:12-14` | `@FunctionalInterface execute(Map)` |
| 消息队列 | `.../agent/queue/MessageQueue.java` | 简单 FIFO |
| 小说主 Agent | `.../agent/novel/NovelMainAgent.java:157-195` | 注册 7 个子 Agent 入口 |
| 子 Agent 工具构建 | `NovelMainAgent.java:893-966` | `buildXxxTools` |
| 子 Agent 入口 | `NovelMainAgent.java:983-1100` | `registerCallXxx` |
| 大纲主 Agent | `.../agent/outline/OutlineMainAgent.java:145-160,376-419` | 子 Agent 编排 |
| 断点 | `.../agent/checkpoint/CheckpointManager.java` | `t_task_list.checkpoint` |
| 事件 | `.../agent/emitter/AgentEmitter.java:21-86` | 主题 + `stream/responseEnd/subAgentStream/toolCall/transfer/...` |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| 工具即 `@FunctionalInterface` | 工具=一段可执行逻辑，注册简单 | 无强类型参数校验，靠工具自身解析 |
| 子 Agent 用「入口工具」暴露给 LLM | LLM 只认函数名，天然可编排 | 必须严格裁剪子 Agent 工具集 |
| 子 Agent 同步调用 | 时序简单、结果直接回填 | 主 Agent 阻塞；子 Agent 慢会拖长主循环 |
| `synchronized` + `MessageQueue` | 单实例串行，避免上下文交叉 | 排队消息无超时/无优先级 |
| 每轮工具结果全量入 messages | 模型能看到全部事实 | token 线性增长，20 轮易超限 |
| 循环上限仅告警 | 避免抛错中断 | 用户侧看不到「为什么没结果」 |

---

## 5. 关键工程实现

**（1）工具循环主干（`BaseAgent.java:110-202`）**

```java
private void executeWithToolLoop(List<ChatMessage> messages, ToolRegistry tools, boolean isSubAgent) {
    int maxIterations = 20;
    for (int i = 0; i < maxIterations; i++) {
        AiRequest request = buildAiRequest(messages, tools);
        TextAiProvider provider = aiProviderService.getTextProvider(getAiFunctionKey());
        AiResponse response;
        try {
            response = aiRetryTemplate.execute(() -> provider.invokeWithTools(request),
                                               getAgentType() + ".llmCall");
        } catch (Exception e) {                       // 失败落日志 + 推前端，直接返回
            emitter.error("AI调用失败: " + e.getMessage());
            logAction("llmCall", ..., "failed", e.getMessage(), null);
            return;
        }
        if (!response.hasToolCalls()) {               // 正常终止
            messages.add(ChatMessage.assistant(response.getContent()));
            if (isSubAgent) emitter.subAgentEnd(getAgentType());
            else            emitter.responseEnd(response.getContent());
            return;
        }
        for (ToolCall toolCall : response.getToolCalls()) {
            AgentTool tool = tools.getTool(toolCall.getName());
            Object result = tool == null ? "Error: unknown tool '" + toolCall.getName() + "'"
                                         : tool.execute(objectMapper.readValue(toolCall.getArguments(), ...));
            messages.add(ChatMessage.toolResult(toolCall.getId(), objectMapper.writeValueAsString(result)));
        }
    }
    log.warn("[{}] Tool loop reached max iterations for project={}", getAgentType(), projectId);
}
```

**（2）权限分级：子 Agent 工具集不含 `callXxx`（`NovelMainAgent.java:157-195`）**

主 Agent 注册业务工具 + 7 个「调用子 Agent」入口；子 Agent 只注册业务工具。**这样 LLM 即使想递归，也没有可调用的函数名**。

**（3）子 Agent 同步取结果（`BaseAgent.java:205-233`）**

```java
protected String invokeSubAgent(String subAgentName, String task, String subSystemPrompt, ToolRegistry subTools) {
    emitter.transfer(subAgentName);
    List<ChatMessage> subHistory = new ArrayList<>();
    subHistory.add(ChatMessage.system(subSystemPrompt));
    subHistory.add(ChatMessage.user(task));
    executeWithToolLoop(subHistory, subTools, true);
    for (int i = subHistory.size() - 1; i >= 0; i--) {   // 取最后一条 assistant
        if ("assistant".equals(subHistory.get(i).getRole())) return subHistory.get(i).getContent();
    }
    return "";
}
```

**（4）单实例串行 + 排队（`BaseAgent.java:88-108,275-282`）**：`synchronized(this)` 判断 `RUNNING`，是则入队；`finally` 里置 `IDLE` 并 `processNextInQueue`。

---

## 6. 踩坑根因排障

### 6.1 【代码事实】`pause()` / `cancel()` 无法中断正在跑的工具循环

```java
public void pause()  { state = AgentState.PAUSED; checkpointManager.save(...); }
public void cancel() { state = AgentState.IDLE; messageQueue.clear(); }
```
但 `executeWithToolLoop` 的 `for` 循环体里**从不读取 `state`**。也就是说：

- 循环跑到第 10 轮时调 `pause()`，第 11-20 轮照跑；
- `cancel()` 只是清空**待处理队列**并置 IDLE，正在执行的循环不受影响；
- 更糟的是 `cancel()` 置 `IDLE` 后，`finally` 里也会置 `IDLE` 并 `processNextInQueue()`，若期间有并发 `onMessage`，可能出现状态竞争。

**修复方向**：在循环每次迭代开头检查 `if (state == PAUSED/CANCELLED) return;`，或用 `volatile` + `CancellationToken`。

### 6.2 【代码事实】达 20 轮上限不报错、不通知

```java
log.warn("[{}] Tool loop reached max iterations ...");
```
循环耗尽后方法直接返回：**不发 `responseEnd`，不推 `error`，不落失败日志**。前端表现为「一直转圈直到超时」，排障时需去服务端日志找这行 `max iterations`。

### 6.3 【代码事实】工具结果全量入 messages → 20 轮后 token 爆炸

每轮把所有工具的完整结果（可能是整章正文）追加进 `messages`，且 `buildAiRequest` 每轮把整个 `messages` 重新发上游。轮次越多，请求越大，容易触发超长/超费。建议对旧工具结果做摘要或截断。

### 6.4 【代码事实】「查不到工具」与「工具执行异常」都只是文本回给模型

`tool == null` 或 `execute` 抛异常时，都只是把一段 `Error ...` 文本作为 `toolResult` 回填，**不中断循环**。模型可能反复调用同一个不存在的工具直到 20 轮耗尽。

### 6.5 【代码事实】两套同名「死代码」易误导

仓库里存在两组同名类：

| 类别 | 实际使用（被 `BaseAgent` 引用） | 死代码（0 引用） |
| --- | --- | --- |
| 工具注册表 | `agent/registry/ToolRegistry` | `agent/core/ToolRegistry` |
| 工具接口 | `agent/tool/AgentTool` | `agent/core/AgentTool` |
| 消息队列 | `agent/queue/MessageQueue` | `agent/core/MessageQueue` |

改造时若改到 `agent/core/*`，**不生效**。排查时应以 import 为准。

### 6.6 【代码事实】子 Agent 是同步调用 → 主循环被长时间阻塞

`invokeSubAgent` 直接在当前线程跑子 Agent 的工具循环。若子 Agent 内部又发起多次 LLM 调用（如逐章写小说），主 Agent 会**长时间卡住**，且第 07 篇的 `AgentSession` 单实例锁会让该会话期间无法处理新消息。

### 6.7 常见现象对照表

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| 点暂停/取消无效 | 循环不读 state（§6.1） | 循环内检查状态 |
| 前端一直转圈无结果 | 达 20 轮仅 warn（§6.2） | 查 `max iterations` 日志 |
| 请求越来越慢/超长 | 工具结果线性累积（§6.3） | 截断/摘要旧结果 |
| 模型反复调不存在的工具 | 错误只作文本回填（§6.4） | 未知工具立即终止或纠偏 |
| 改了工具注册但不生效 | 改到 `agent/core/*` 死代码（§6.5） | 核对 import 路径 |
| 长任务期间发新消息无反应 | 子 Agent 同步 + 单实例锁（§6.6） | 子 Agent 异步化或加超时 |

---

## 7. 可迁移落地方案

`【落地建议】`

1. **工具循环必须有三种出口**：正常结束（无 tool_calls）、优雅失败（错误）、超限终止（达 maxIterations）——三种都要通知调用方、落日志，不能静默 return。
2. **子 Agent 权限靠「不注册」而非「运行时校验」**：子 Agent 的工具集直接不含入口工具，从源头杜绝递归；比在 `invokeSubAgent` 里判断更可靠。
3. **循环内可中断**：每次迭代检查取消/暂停标志；用 `CancellationToken` 或 `volatile`。
4. **上下文治理**：只保留最近 N 轮完整工具结果，更早的做摘要/落盘引用；避免 token 线性增长。
5. **未知工具/工具异常要显式纠正**：把错误文本回填可以，但要加「同一工具连续失败 N 次即终止」的保护。
6. **可观测优先**：LLM 调用、工具调用、子 Agent 调用都落结构化日志（本仓 `t_agent_log` 是好实践），并带 parent 关联（`parentLogId`）形成调用树。
7. **子 Agent 考虑异步/并行**：子任务间无依赖时可并行，主循环用「等待全部子任务」而非逐个同步。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| 手写工具循环（本方案） | 需要精细控制、事件推送 | 需求复杂时易失控 |
| LangChain4j / Spring AI Agent | 快速搭建、生态工具多 | 抽象泄漏、定制难 |
| 图编排（LangGraph / 状态图） | 复杂多 Agent 图、条件分支 | 引入较重 |
| 纯 Prompt 编排（让模型自己分步） | 简单任务 | 不可控、难观测 |
| 事件驱动 Agent（MQ） | 多实例、可伸缩 | 时序复杂度上升 |

---

## 9. 验收清单

- [ ] 无工具调用时正常结束并推 `responseEnd`/`subAgentEnd`。
- [ ] 有工具调用时执行、回填、继续下一轮。
- [ ] 达 `maxIterations` 时**有**错误通知与日志。
- [ ] 子 Agent 工具集**不含**任何入口工具（无递归）。
- [ ] `pause`/`cancel` 能在循环中生效。
- [ ] 排队消息在上一任务结束后被处理。
- [ ] 未知工具/工具异常有次数上限保护。
- [ ] `t_agent_log` 有 `llmCall`/`toolCall`/`invokeSubAgent` 且带父子关联。
- [ ] 长循环下请求 token 不无限增长。

---

## 10. 待确认项

- `pause`/`cancel` 是否在运行期被实际调用（若无人调用，§6.1 影响有限）。【待确认】
- `MessageQueue` 是否有容量上限与超时。【待确认】
- `buildAiRequest` 每次是否重复序列化大对象（性能）。【待确认】
- 子 Agent 的 LLM functionKey 与主 Agent 是否共用同一模型配置。【待确认】
- `agent/core/*` 死代码是否计划清理。【待确认】
