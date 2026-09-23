# 05 Agent 框架：工具调用循环与子 Agent 权限分级编排

> 标签：Agent / Function Calling / 工具循环 / 子 Agent / 权限收敛

---

## 1. 场景与边界

平台用 **Agent + Function Calling** 驱动内容生成：主 Agent（如 `NovelMainAgent` 小说主编、`OutlineMainAgent` 大纲主编）通过与 LLM 多轮工具调用，读写业务数据（世界设定、角色、章节、分镜等），并**调用子 Agent** 完成专职子任务。

核心工程问题：
1. **工具调用循环**：LLM 返回 `tool_calls` 后执行工具、把结果回填、再问 LLM，直到无工具调用或达到轮数上限。
2. **子 Agent 权限分级**：子 Agent 只能拿到**被裁剪过的工具集**，防止它再调用 `call*` 递归编排、也防止越权写数据。

边界：本场景讲 `BaseAgent` 的循环与子 Agent 编排；Prompt 组装见第 09 篇，实时事件见第 07 篇。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| 循环必须有上限 | 否则 LLM 可能陷入工具死循环，需 `maxIterations` |
| 子 Agent 不能递归 | 子 Agent 工具集必须剔除 `call*` 编排类工具 |
| 工具集按角色裁剪 | 不同子 Agent 职责不同，工具集也不同 |
| 历史与上下文 | 工具调用记录要进 `messages`，并持久化 |
| 可观测 | 每次工具调用要落 `t_agent_log` |
| 暂停/恢复/取消 | 长生成需支持中途控制 |

---

## 3. 实现链路

### 3.1 工具循环（`executeWithToolLoop`）

```
for i in 0..19:                 # maxIterations = 20
    resp = provider.invokeWithTools(buildAiRequest(messages, tools))
    if resp.hasToolCalls() == false: 结束（可流式输出）
    追加 assistant(toolCalls) 到 messages
    for each toolCall:
        tool = tools.getTool(name)
        result = tool.execute(args)
        log to t_agent_log
        messages.add(ChatMessage.toolResult(id, result))
    # 继续循环
```

### 3.2 子 Agent 编排

- `NovelMainAgent` 注册 **7 个子 Agent**：`worldArchitect / characterDesigner / plotArchitect / chapterPlanner / novelWriter / editor / qualityInspector`。【代码事实】
- `OutlineMainAgent` 注册 **3 个子 Agent**：`storyteller / outliner / director`。【代码事实】
- 每个 `callXxx` 工具内部调用 `invokeSubAgent(name, task, systemPrompt, subTools)`，`subTools` 由 `buildXxxTools()` 专门构造（**不含 `call*`**）。【代码事实】

### 3.3 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| 基类 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/base/BaseAgent.java:41` | `abstract class BaseAgent` |
| 依赖 | `BaseAgent.java:43-55` | emitter/registry/queue/checkpoint/provider/retry/mappers/state |
| 工具注册 | `BaseAgent.java:74-78` | `new ToolRegistry()` + `registerTools(registry)` |
| 抽象钩子 | `BaseAgent.java:82-86` | `getAgentType/registerTools/buildSystemPrompt/buildContextPrompt/getAiFunctionKey` |
| 入口 | `BaseAgent.java:89-101` | `onMessage(userMessage)` |
| 循环 | `BaseAgent.java:111-115` | `executeWithToolLoop(...)`，`maxIterations = 20` |
| 调 LLM | `BaseAgent.java:125` | `provider.invokeWithTools(request)` |
| 无工具结束 | `BaseAgent.java:148` | `if (!response.hasToolCalls())` |
| 遍历工具 | `BaseAgent.java:167-201` | 执行、落日志、回填 `toolResult` |
| 查工具 | `BaseAgent.java:174` | `tools.getTool(toolCall.getName())` |
| 事件 | `BaseAgent.java:180` | `emitter.toolCall(agentType, name, args)` |
| 超限告警 | `BaseAgent.java:203` | 达到最大迭代 |
| 子 Agent | `BaseAgent.java:207-230` | `invokeSubAgent(name, task, subSystemPrompt, subTools)` |
| 控制 | `BaseAgent.java:238-268` | `onConnect/onDisconnect/cleanHistory/pause/resume/cancel/getState` |
| 工具注册表 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/registry/ToolRegistry.java:18-36` | `register/getTool/getAllDefinitions/hasTool` |
| 工具接口 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/tool/AgentTool.java:12-14` | `@FunctionalInterface Object execute(Map)` |
| 小说主 Agent 注册 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/novel/NovelMainAgent.java:157-195` | 业务工具 + 7 个 `registerCallXxx` |
| 子 Agent 工具集 | `NovelMainAgent.java:893-966` | `buildWorldArchitectTools` … `buildQualityInspectorTools` |
| 编排实现 | `NovelMainAgent.java:983-1100` | `registerCallWorldArchitect` … `registerCallQualityInspector` |
| 动态 Prompt | `NovelMainAgent.java:1047-1066` | `callNovelWriter` 用 `mapGenreToCode(project.getType())` 拼 `novel-gen-*` |
| 大纲主 Agent | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/outline/OutlineMainAgent.java:145-160` | 业务工具 + 3 个 `registerCallXxx` |
| 大纲子工具集 | `OutlineMainAgent.java:376-383` | `buildSubAgentTools()`（仅读+写故事线/大纲） |
| 大纲编排 | `OutlineMainAgent.java:387-425` | storyteller / outliner / director |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| 手写工具循环（非框架） | 可控、无额外依赖 | 需自行处理并发、超时、状态 |
| `maxIterations=20` | 防止死循环 | 复杂任务可能提前截断 |
| 子 Agent 工具集裁剪 | 防递归、符合职责最小权限 | 工具集维护成本高 |
| 工具 = Lambda（`AgentTool`） | 注册简洁 | 复杂工具内联代码量大（`NovelMainAgent` 达 ~1159 行） |
| 子 Agent 共享 `executeWithToolLoop` | 复用循环 | 子 Agent 无独立 `maxIterations` 配置 |

---

## 5. 关键工程实现

**（1）循环 + 工具结果回填（`BaseAgent.java:111-201`）**：每次把 `assistant.tool_calls` 和 `tool.toolResult` 追加进 `messages`，形成标准 OpenAI 工具调用序列。【代码事实】

**（2）权限分级（`NovelMainAgent.java:932-941`）**：
```java
private ToolRegistry buildNovelWriterTools() {
    ToolRegistry sub = new ToolRegistry();
    registerGetCharacters(sub);
    registerGetChapterPlan(sub);
    registerSaveChapter(sub);
    // 注意：不注册任何 callXxx，子 Agent 无法再编排
    return sub;
}
```
`【工程推断】` 这是防止「子 Agent → 子 Agent」无限递归的关键手段。

**（3）动态组合 Prompt（`NovelMainAgent.java:1047-1066`）**：`callNovelWriter` 依据作品类型动态拼接体裁 prompt（`mapGenreToCode`）。

---

## 6. 踩坑根因排障

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| Agent 陷入工具死循环 | 无 `maxIterations` 或工具总返回「继续」 | 已有 20 轮上限；检查工具返回语义 |
| 子 Agent 又调子 Agent | 子工具集误注册了 `call*` | 审查各 `buildXxxTools()` |
| 工具调用报「unknown tool」 | LLM 幻觉出未注册工具名 | `BaseAgent.java:175-178` 会记 failed 并回填错误 |
| 复杂任务被截断 | 20 轮不够 | 调大上限或拆分任务 |
| `NovelMainAgent` 难维护 | 单类 ~1159 行 | 按工具组拆分到多个 `*Tools` 装配类 |
| 找不到实际使用的类 | **同名死代码** | `agent/core/{ToolRegistry,AgentTool,MessageQueue}` 全仓 0 引用；真正在用的是 `registry/tool/queue` 下同名类 |

> ⚠️ **重要**：项目里存在**两组同名的工具/队列类**，其中 `agent/core/` 一组是死代码。迁移或阅读时务必认准 `agent/registry/ToolRegistry`、`agent/tool/AgentTool`、`agent/queue/MessageQueue`。【代码事实】

---

## 7. 可迁移落地方案

`【落地建议】`

1. **工具循环骨架**：`请求 → 有 tool_calls？→ 执行 → 回填 → 再请求`，设 `maxIterations`（建议 10~30）与总超时。
2. **工具注册表**：`name → (executor, ToolDefinition)`，`getAllDefinitions()` 直接喂给 LLM。
3. **子 Agent 权限分级**：子 Agent 的工具集 = 主工具集 **减去所有编排类工具**，实现职责最小权限。
4. **每步可观测**：工具调用名称/参数/结果/耗时/状态落审计表，便于复盘。
5. **控制面**：提供 pause/resume/cancel 与状态查询。
6. **拆分大 Agent**：单个 Agent 超过 ~500 行就该按工具组拆分装配。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| 手写循环（本方案） | 需精细控制、无框架依赖 | 复杂多 Agent 图 |
| Spring AI / LangChain4j | 快速接入、内置工具循环 | 需深度定制 |
| 状态图工作流（LangGraph 类） | 多 Agent 编排、可回放 | 引入新范式成本 |
| 纯编排（无 LLM 循环） | 流程固定 | 需模型自主决策 |

---

## 9. 验收清单

- [ ] LLM 返回 `tool_calls` 后工具被正确执行并回填。
- [ ] 无工具调用时循环终止并输出结果。
- [ ] 达到 20 轮有明确告警。
- [ ] 子 Agent 无法调用任何 `call*` 工具（工具集已裁剪）。
- [ ] 每次工具调用在 `t_agent_log` 有记录（名称/参数/结果/耗时/状态）。
- [ ] pause/resume/cancel 生效。
- [ ] 未注册工具名返回 `Error: unknown tool` 而非崩溃。

---

## 10. 待确认项

- 子 Agent 是否可配置独立 `maxIterations`（当前共用基类默认值）。【待确认】
- `invokeSubAgent` 的返回解析（取子历史最后一条 assistant）是否健壮。【待确认】
- `agent/core/` 死代码是否计划删除。【待确认】
- `NovelMainAgent` 7 个子 Agent 的 system prompt 是否全部由 `PromptService` 提供（见第 09 篇）。【待确认】
