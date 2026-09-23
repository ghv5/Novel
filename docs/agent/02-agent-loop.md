# 02 · 主循环：agent-loop.ts 逐行精读

> 本文是整系列的核心。读懂 `runLoop` 的双层 while，就读懂了 Pi 90% 的行为。

源码：`packages/agent/src/agent-loop.ts`（约 900 行）、`packages/agent/src/agent.ts`（约 610 行）。

## 2.1 一次 prompt 的完整链路（12 步）

```
agent.prompt("帮我修复这个 bug")
  │
  ├─ 1. 入口校验：activeRun 互斥（同一时刻只允许一个 run）
  ├─ 2. 生命周期包装：runWithLifecycle
  │     创建 AbortController + activeRun promise
  │     isStreaming = true
  │     finally 里 finishRun() 清状态
  │
  ├─ 3. 快照 + 配置组装
  │     createContextSnapshot() → 拷贝 messages/tools
  │     createLoopConfig() → 把队列 drain 函数 + 9 个钩子打包成 AgentLoopConfig
  │
  ├─ 4. 首轮初始化（runAgentLoop）
  │     declareToolChanges → 比对 context.tools 与 transcript 已声明工具
  │     差集写成 system 消息（toolsAdded / toolsRemoved）
  │     emit agent_start → turn_start → message_start/end
  │
  ├─ 5. 外层 while(true)
  │     ┌─────────────────────────────────────────────────────┐
  │     │ 内层 while (hasMoreToolCalls || pendingMessages)    │
  │     │  ┌──────────────────────────────────────────────┐   │
  │     │  │ a. prepareNextTurn（可换 context/model）       │   │
  │     │  │ b. 合并 pending 消息 → declareToolChanges     │   │
  │     │  │ c. prepareRequest（请求前最后调整）            │   │
  │     │  │ d. streamAssistantResponse                   │   │
  │     │  │    transformContext → convertToLlm → streamFn│   │
  │     │  │    流式事件: start/delta/done                 │   │
  │     │  │ e. 硬退出判断: stopReason error/aborted       │   │
  │     │  │ f. 提取 toolCalls → executeToolCalls         │   │
  │     │  │    (sequential / parallel)                   │   │
  │     │  │ g. finishTurn 钩子 → turn_end                │   │
  │     │  └──────────────────────────────────────────────┘   │
  │     │ 外层: getFollowUpMessages → 有则 continue           │
  │     └─────────────────────────────────────────────────────┘
  │
  ├─ 6. 正常退出: emit agent_end
  ├─ 7. EventStream.end(newMessages)
  └─ 8. Agent.processEvents 做状态归约
        listener 全部 settle → finishRun → agent idle
```

## 2.2 双层 while 的精确语义

### 外层 `while (true)`

职责：**follow-up 消息轮询**。

```typescript
// agent-loop.ts L191
while (true) {
    let hasMoreToolCalls = true;
    // 内层循环...
    
    // 内层退出后：
    const followUpMessages = (await config.getFollowUpMessages?.()) || [];
    if (followUpMessages.length > 0) {
        pendingMessages = followUpMessages;
        continue;  // 重新进内层
    }
    if (explicitContinuation) {
        // finishTurn 返回了裸 continue，再跑一轮 context-only
        pendingMessages = [];
        continue;
    }
    break;  // 真正退出
}
```

**关键**：follow-up 是"agent 本应停止时，用户又输入了新消息"。steering 是"agent 正在跑，用户插话"。两者 drain 时机不同：
- steering：每轮 turn 结束时 drain（内层 while 底部）
- follow-up：agent 将停时 drain（外层 while 底部）

### 内层 `while (hasMoreToolCalls || pendingMessages.length > 0)`

职责：**持续处理工具调用 + steering 消息，直到两者皆空**。

```typescript
// agent-loop.ts L195
while (hasMoreToolCalls || pendingMessages.length > 0) {
    // a. prepareNextTurn（上轮完成后调，可换 context/model/thinkingLevel）
    // b. 合并 prepared + pending 消息
    // c. prepareRequest
    // d. streamAssistantResponse
    // e. 硬退出判断
    // f. 工具分发
    // g. finishTurn → turn_end
}
```

## 2.3 终止条件的全部来源

| 来源 | 代码位置 | 行为 |
|------|---------|------|
| `stopReason === "error"` | L243 | 硬退出，发 agent_end |
| `stopReason === "aborted"` | L243 | 硬退出，发 agent_end |
| `finishTurn` 返回 `{action:"end"}` | L281 | 立即退出 |
| 工具批级 terminate（**所有**工具结果都 terminate=true） | L846 `shouldTerminateToolBatch` | `hasMoreToolCalls = false` |
| 无工具调用 + 无 steering + 无 follow-up + finishTurn 未 continue | 自然退出 | break |

**注意**：Pi 核心循环**没有硬编码 maxTurns**。轮次上限由上层通过 `finishTurn` 钩子注入（如"已跑 10 轮，返回 end"）。

## 2.4 工具调用的提取与截断保护

```typescript
// agent-loop.ts L235
const toolCalls = message.content.filter((c) => c.type === "toolCall");

// L241-243: 截断保护
if (message.stopReason === "length") {
    // 输出被 token 限制截断，工具参数可能不完整
    // 不执行，全部标记失败，让模型重发
    const executedToolBatch = await failToolCallsFromTruncatedMessage(toolCalls, emit);
    toolResults.push(...executedToolBatch.messages);
    hasMoreToolCalls = !executedToolBatch.terminate;
}
```

**为什么**：`stopReason === "length"` 表示模型输出被 max_tokens 截断，tool call 的 JSON 参数可能只写了一半。执行截断参数的工具会导致不可预测的行为（如 edit 工具只替换了文件的一半）。正确做法是告诉模型"参数被截断了，请重发"。

## 2.5 流式消息的回写机制

```typescript
// agent-loop.ts L401-451 (streamAssistantResponse 内部)
// 流式事件处理：
case "start":
    // 把 partial 消息 push 进 context.messages 末位
    context.messages.push(partialMessage);
    emit({ type: "message_start", message: partialMessage });
    break;
case "text_delta" / "thinking_delta" / "toolcall_delta":
    // 替换 context.messages 末位消息（原位更新）
    context.messages[context.messages.length - 1] = partialMessage;
    emit({ type: "message_update", message: partialMessage });
    break;
case "done" / "error" / 流结束:
    // 取最终消息
    const finalMessage = await response.result();
    context.messages[context.messages.length - 1] = finalMessage;
    emit({ type: "message_end", message: finalMessage });
    return finalMessage;
```

**设计意图**：partial 消息实时回写 context，保证下一轮 `convertToLlm` 看到的是最终完整消息。如果只在 `done` 时写入，中间的 delta 事件就只是"通知"，context 里没有实际内容。

## 2.6 工具声明的动态同步

```typescript
// agent-loop.ts L326-378 (declareToolChanges)
function declareToolChanges(context, messages) {
    // 比对 context.tools 与 transcript 中已声明的工具
    // 差集写成 system 消息:
    //   { type: "system", toolsAdded: [...], toolsRemoved: [...] }
    // pending system 消息视为"意图被覆盖"
}
```

**场景**：运行中动态增加/移除工具（如用户切换到"只读模式"，移除 write/edit 工具）。Pi 不重新发整个 system prompt，而是发一条增量 system 消息告诉模型"工具变了"。

## 2.7 Agent 状态归约（processEvents）

```typescript
// agent.ts L498-547
async processEvents(event, signal) {
    switch (event.type) {
        case "message_start" / "message_update":
            state.streamingMessage = event.message;
            break;
        case "message_end":
            state.messages.push(event.message);
            state.streamingMessage = undefined;
            break;
        case "tool_execution_start":
            state.pendingToolCalls.add(event.toolCall.id);
            break;
        case "tool_execution_end":
            state.pendingToolCalls.delete(event.toolCall.id);
            break;
        case "turn_end":
            state.errorMessage = event.message.stopReason === "error" 
                ? event.message.errorMessage : undefined;
            break;
        case "agent_end":
            // 最终消息已确定
            break;
    }
    // 每个事件归约后，按订阅顺序 await 所有 listener
    for (const listener of this.listeners) {
        await listener(event, signal);
    }
}
```

**关键**：`agent_end ≠ idle`。listener 全部 settle 后，`finishRun()` 才让 agent 变 idle。如果某个 listener 阻塞（如 UI 渲染慢），agent 不会提前变 idle。

## 2.8 abort 的传播链

```
Agent.abort()
  → AbortController.abort()
  → signal.aborted = true
  → 传给 runAgentLoop → streamFn / tool.execute
  → 循环内多个 signal?.aborted 检查点:
    - L554: prepareToolCall 前
    - L583: executePreparedToolCall 前
    - L612: 并行执行前
    - L624: 等待 Promise.all 时
  → handleRunFailure 合成失败 assistant 消息
  → 补齐 message_end/turn_end/agent_end 事件序列
```

**设计**：abort 不是"立即杀掉进程"，而是"标记信号，在安全点检查并优雅退出"。工具执行中可以安全中断（如 bash 命令的 `process.kill`），但不会在消息写入中途撕裂。

## 2.9 steering vs follow-up：两种消息注入模式

| 维度 | steering | follow-up |
|------|----------|-----------|
| 时机 | agent 正在跑（内层 while 中） | agent 将停（外层 while 底部） |
| 队列 | `steeringQueue` | `followUpQueue` |
| drain 时机 | 每轮 turn 结束点 + prepareNextTurn 期间 | agent 本应 break 前 |
| QueueMode | `all`（一次全取）/ `one-at-a-time`（每轮取一条） | 同左 |
| 典型场景 | 用户按 Enter 插话"等一下，先查下日志" | agent 说完了，用户又输入"再帮我看看另一个文件" |

## 2.10 核心设计总结

| 设计决策 | 原因 |
|---------|------|
| 双层 while | 分离"工具调用循环"与"用户续输入"两个正交关注点 |
| 无 maxTurns | 轮次控制是宿主策略，不是循环语义 |
| 截断保护 | 不执行参数不完整的工具调用 |
| 流式回写 | 保证 context 一致性，delta 不是"通知"而是"状态更新" |
| 工具声明差量 | 避免每次工具变化都重发整个 system prompt |
| 事件归约 + listener | 状态机与 UI 解耦，listener 可阻塞但不影响核心循环正确性 |
| abort 信号传播 | 优雅退出，不在不安全点中断 |

## 延伸阅读

- 03 文：工具系统六阶段生命周期（`executeToolCalls` 内部）
- 06 文：9 个钩子的精确调用时机与契约
