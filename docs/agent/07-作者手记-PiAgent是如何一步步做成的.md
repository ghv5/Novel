# 07 · 作者手记：PiAgent 是如何一步一步做成的

> 假设我是 Pi 的作者，回头复盘这个 Agent 从 0 到生产级的全过程。
> 本篇不重复 02–06 的源码原理，而是按"我当时先做什么、为什么这么选、踩了什么坑、后来怎么补"的顺序讲一遍。
>
> **阅读约定**：仓库里能直接核对的事实标【仓库证据】并给出路径/版本；作者视角的复盘判断标【作者复盘】；面向你新项目的建议标【给新项目】。
> 这样你能分清哪些是 Pi 真的做了什么，哪些只是我作为复盘者的推断。

## 0. 先用一句话讲清 Agent 是什么

**Agent = 一个"调用大模型 → 模型要求用工具 → 执行工具 → 把结果喂回模型 → 再调用"的循环，外面套上状态、持久化、压缩、事件和恢复。**

后面所有工程复杂度，都是在回答这个循环的四个问题：

1. 模型怎么调（多协议、流式）→ 第 2 节
2. 循环怎么转、什么时候停（02 文的主循环）→ 第 3 节
3. 工具怎么安全地执行（03 文）→ 第 4 节
4. 崩溃/长会话/多端怎么不丢不炸（05、06、08、09 节）

如果你能把这四问讲清楚，你就已经能讲清 Agent 的主体开发过程了。

---

## 1. 起点：先做能用的产品，再抽出内核

【仓库证据】`packages/coding-agent/CHANGELOG.md` 的 `[0.10.0] - 2025-11-25` 是"Initial public release"，首批能力是：交互式 TUI、会话管理（`--continue`/`--resume`/`--session`）、内置工具 `read/write/edit/bash/glob/grep/think`、Anthropic/OpenAI/Google 三家模型、流式响应、流式中排队消息。

【作者复盘】注意时间线：**先有一个能用的编码 CLI，再把它拆成 12 个包**（见 01 文的包清单）。真正的顺序不是"先设计完美框架"，而是：

1. 先把 `cli.ts` 和模型 SDK 拼起来，能对话、能读文件、能改文件；
2. 发现"换模型要改一堆调用代码" → 抽出 `packages/ai`；
3. 发现"循环逻辑和 UI 缠在一起、没法单独测试" → 抽出 `packages/agent`（无状态循环 + 有状态包装器）；
4. 发现"一个包什么都装，构建慢、边界乱" → monorepo 化，按职责拆包。

【仓库证据】根 `README.md` 给出最终依赖链：`chord → tui → telemetry → ai → durable → agent → session-backends → protocol → client → server → coding-agent`。01 文也指出 `cli.ts` 只有 7 行，真正的会话核心是 `AgentSession`。

【给新项目】不要一上来就设计 12 个包。**先写一个 500 行能跑的循环，把"模型调用"和"UI"用一层接口隔开**；等你第二次想替换其中一个时，再抽包。拆包是结果，不是起点。

---

## 2. 第一块砖：独立的 LLM 层 `pi-ai`

### 选型

【仓库证据】`packages/ai/CHANGELOG.md` 最早的 `[0.9.4] - 2025-11-26` 写的是"Initial release with multi-provider LLM support"。04 文总结其形态：**Provider（30+ 业务方）→ API 协议（10 种）→ 统一契约**三层，很多 provider 复用同一协议（OpenAI 兼容接口被 deepseek/openrouter/groq 等共享）。

我的三个关键选择：

1. **provider 与协议分离**：`provider = id + baseUrl + auth + models + api`，新增厂商只写一个十几行的轻量对象（04 文的 `openaiProvider` 全文 16 行）。
2. **懒加载**：`lazyApi(() => import("./openai-responses.ts"))`，冷启动不把所有协议实现都载进来。
3. **统一事件流**：不管底层是 OpenAI 的 `function_call`、Anthropic 的 `tool_use` 还是 Google 的 `functionResponse`，上层只看到 `AssistantMessageEvent`（start / text_delta / thinking_delta / toolcall_delta / done / error）。

### 踩过的坑（都在 CHANGELOG 里）

- 【仓库证据】`[0.12.10]` 修了 **OpenAI token 计数把缓存 token 重复计算**：`usage.input` 曾包含 cached token，导致 `input + cacheRead` 算重；改为 `input` 只表示非缓存输入，`input + output + cacheRead + cacheWrite` 才是上下文总量。
- 【仓库证据】`[0.13.0]` 因此给 `Usage` **破坏性新增 `totalTokens`**，并要求所有构造点补齐。
- 【仓库证据】`[0.12.10]` 还修正了 Claude Opus 4.5 的缓存定价（原来贵了 3 倍），并在 `scripts/generate-models.ts` 里加手动覆盖直到上游修好。

【作者复盘】这三条的教训是一致的：**多 provider 的"归一化"不只是类型对齐，更是语义对齐（token 怎么算、价格怎么算）**。统一类型很容易，统一语义才难——所以要给 `ModelCompat` 能力位，把"是否支持 developer 角色、能否会话中插 system 消息、thinking 格式、缓存控制格式"这些差异显式化（04 文 4.6）。

【给新项目】第一版就做两件事：**统一消息/工具调用类型** + **一份 `compat` 能力表**。不要为每个厂商写 if-else，把差异收敛到"能力位 + 协议实现"两个地方。

---

## 3. 核心引擎：无状态双层 `while` 主循环

### 选型

【仓库证据】02 文拆的是 `packages/agent/src/agent-loop.ts`：`runLoop` 是**无状态的双层 while**，`Agent` 类只是**有状态包装器**。这个分层是整个项目最值得抄的设计：

- 外层 `while(true)`：负责 **follow-up** 消息（agent 即将停止时用户又输入）；
- 内层 `while(hasMoreToolCalls || pendingMessages)`：负责**工具调用 + steering**（agent 正在跑、用户插话）。

### 关键决策

- **无硬编码 maxTurns**：轮次上限由宿主通过 `finishTurn` 钩子注入，循环只负责语义。
- **截断保护**：02 文 2.4 指出，当 `stopReason === "length"` 时，tool call 的 JSON 参数可能只写了一半，**绝不能执行**，而应把全部工具调用标记失败、让模型重发。否则 `edit` 只替换半个文件，后果不可预测。
- **流式回写**：partial 消息实时替换 `context.messages` 末位，保证下一轮 `convertToLlm` 看到的是完整消息；delta 不是"通知"，而是"状态更新"（02 文 2.5）。

### 踩过的坑：钩子语义演进

【仓库证据】`packages/agent/CHANGELOG.md` 的 `[0.87.0]` 做了一次破坏性变更：**移除 `shouldStopAfterTurn`，统一为 `finishTurn`**，并要求返回 `{ action: "end" }` 才停；同版本还新增 `prepareRequest`（每次 provider 请求前，包括第一次）。迁移说明里明确：`finishTurn` 在助手消息和全部工具结果定稿之后、`turn_end` 之前运行，对 error/aborted 也会运行但决策被忽略（因为它们本来就是硬退出）。

【作者复盘】这说明"停止"这件事一开始被塞进了不合适的钩子：旧钩子只在正常响应时跑，还带着副作用。**钩子越少越好，但每个钩子的时机和契约必须写死**，否则宿主会做出互相矛盾的行为。

【给新项目】循环第一版就定义清楚三样：**终止条件集合、消息注入点（steering/follow-up）、钩子时机表**。宁可先少几个钩子，也不要给一个语义模糊的钩子。

---

## 4. 让模型动手：工具六阶段生命周期

### 选型

【仓库证据】03 文把工具执行拆成：`prepareToolCall → executePreparedToolCall → finalizeExecutedToolCall`，外加 `beforeToolCall`/`afterToolCall` 钩子。`AgentTool` 是通用契约，`AgentHarnessTool` 是带会话上下文的特化契约，两层解耦。

几个必须做对的细节：

- **parallel 的 preflight + 源序消息**：并行模式下先顺序 prepare 所有工具，再 `Promise.all` 执行；`tool_execution_end` 按**完成顺序**发（UI 实时性），但 `toolResult` 消息按**源码顺序**发（与 assistant 输出一致）。
- **批级 terminate**：03 文 3.4 的 `shouldTerminateToolBatch` 要求**所有**工具结果 `terminate === true` 才终止。因为模型可能一次发"读 A + 读 B"，只有 A 完成就终止是错的。
- **并发写串行化**：`harness/tools/file-mutation-queue.ts` 把写操作入队，避免并行 edit 同一文件产生竞态。
- **replay 策略**：`replay: "safe"`（读）恢复时可重放，`"never"`（写/bash）不可重放。

### 踩过的坑

【仓库证据】`packages/agent/docs/pico/pico-work.md` 是一份 clean-room 重写计划，开头明确列出旧实现（pico2）的真实 bug，并规定新实现每个都要有测试覆盖：

- 一次失败的生成**没有结束 run**（循环卡死/状态悬空）；
- **refs 被塞进 task patch**（引用语义泄漏进可持久化数据）；
- **collapse 死锁住 append**；
- **quiescence（静默判定）在重试等待期间误触发**；
- **watch 丢掉了迟到的 delta 事件**。

【作者复盘】这五条几乎覆盖了工具/事件系统最典型的翻车方式：**终止状态没闭合、引用不该持久化、锁顺序死锁、空闲判定与重试的竞态、增量事件丢失**。它们不是理论风险，是被测试抓出来的。

【给新项目】工具系统先保证四件事：**参数校验（schema）→ 安全拦截（before 钩子）→ 异常必须转成 error result 而不是抛出 → 结果顺序可预测**。并发写一定要有串行化队列，别指望调用方自觉。

---

## 5. 记忆：会话树 + 事务持久化

### 选型

【仓库证据】05 文与 `packages/agent/docs/harness.md`：

- **会话不是线性历史，而是一棵带 `parentId` 的树**。fork/回退的本质是"把 tip 指到另一个节点"，而不是回滚日志。
- **三个存储**：entry 树（写一次、只追加）、values/lists（当前可变状态）、usage 账本（只追加）。所有内容是这三者之一，没有第四处。
- **事务式 mutation**：`commit` 阶段**拒绝落盘 `stopReason === "pending"` 的 assistant 消息**——流式没写完的消息永远不进持久层。
- **双后端**：Memory / JSONL / SQLite 跑同一套 conformance 测试。JSONL 用 `splitCompleteLines` 处理写入中断的"撕裂行"，`open()` 时自动做 v3→v4 迁移。

### 踩过的坑

- 【仓库证据】`harness.md` §1.4：**一个事务要么全成要么全不成**；JSONL 的"撕裂尾行整行丢弃"就是为了让"事务内不存在崩溃前缀"在文件编码里也成立。
- 【仓库证据】存储格式一直在演进：`pico-work.md` 里反复强调"value 是整体替换、list 只在整体删除前追加"，`values.md` 则把早期的 `registers.ts` 换成绑定的 `value<T>()`/`list<T>()` 地址——**因为全局 namespace→type 映射表会失控**。

【作者复盘】把"写一次的历史"和"可变的当前状态"分开，是后来能安心做 fork、压缩、恢复的前提。很多 Agent 项目把对话历史当可变数组改来改去，做到分支和恢复时就会痛苦。

【给新项目】对话存储第一版就用**只追加的事件/entry + 一个小的可变状态**两层，别把 UI 状态和持久化状态混在一个对象里。

---

## 6. 长会话不爆：上下文压缩

【仓库证据】05 文（源码 `harness/compaction/compaction.ts`）：

- 触发条件：`contextTokens > contextWindow - reserveTokens`；
- 默认值：`reserveTokens = 16384`、`keepRecentTokens = 20000`；
- **token 估算**：优先用最后一条合法 assistant 的 provider `usage.totalTokens`，其余按"字符数 / 4"回退，图片固定按 4800 字符；
- **切点安全边界**：`findValidCutPoints` 只允许在非 toolResult 的条目处切——**绝不能在 tool call 和 tool result 中间切开**；
- **split turn**：切点落在某轮前半段时，对 turn 前缀单独生成一份 `turn-prefix summary`；
- **结构化摘要模板**：Goal / Constraints / Progress(Done/In Progress/Blocked) / Key Decisions / Next Steps / Critical Context，并强调**保留文件路径、函数名、错误消息**；
- 摘要产物追加 `CompactionDetails{readFiles, modifiedFiles}`，保证压缩后模型仍知道读过/改过哪些文件。

【作者复盘】压缩最容易犯两个错：**在工具调用对中间切断**（模型看到孤立 toolCall），和**摘要丢关键标识符**（模型重新瞎猜文件名）。这两点在实现里都用了硬约束解决。

【给新项目】压缩=估算 + 合法切点 + 结构化摘要 + 文件操作台账，四件套缺一不可。先保证"不破坏 tool call/result 配对"，再谈摘要质量。

---

## 7. 把控制权交出去：10 个事件 + 10 个钩子

### 选型

【仓库证据】06 文：Pi 用 10 种 `AgentEvent`（`agent_start/end`、`turn_start/end`、`message_start/update/end`、`tool_execution_start/update/end`）驱动 UI，用 `AgentLoopConfig` 的 10 个钩子向宿主开放扩展点：`convertToLlm`、`transformContext`、`prepareRequest`、`prepareNextTurn`、`getSteeringMessages`、`getFollowUpMessages`、`beforeToolCall`、`afterToolCall`、`finishTurn`、`getApiKey`。`Agent.processEvents` 是**纯状态归约**，事件与状态机解耦。

### 踩过的坑

- 【仓库证据】06 文 2.7 强调 **`agent_end ≠ idle`**：只有当所有 listener settle 之后 `finishRun()` 才让 agent 变空闲。如果 UI 渲染慢，agent 不会提前变 idle。
- 【仓库证据】harness 层事件总线用 `structuredClone` 隔离 payload（避免 listener 改到原始事件），handler 异常转成 `handler_error` 事件而不中断分发。
- 【仓库证据】钩子的增删本身就是坑：第 3 节的 `shouldStopAfterTurn → finishTurn` 变更说明，钩子语义必须精确到"正常/错误/中止响应都跑不跑、什么时候跑"。

【给新项目】事件流只给 UI 用，不要让它持有业务真相；业务真相由状态归约产出。每个钩子写一行契约注释：**何时调用、能不能抛错、返回值怎么用**。

---

## 8. 生产级难题：可恢复且不重复副作用

这是从"能跑"跨到"生产可用"的一步，也是 Pi 花力气最多的地方。

### 选型

【仓库证据】`packages/agent/docs/harness.md` 的核心模型：

- **三个存储 + 一个不变量**：任何 payload 只会在 entry、value/list、usage ledger 之一（§0.3）；
- **durable restart point**：每次持久转换后，用**完整状态**替换 `pi.op.state`，恢复时读取它并从对应 procedure 继续，**不重放日志、不靠"缺什么猜什么"**；
- **intent → effect → settlement**：每次外部调用（provider 请求、真实工具调用）都先提交"打算做什么、将用哪个 id"，再执行不确定的副作用，最后提交结果。这样**副作用已发生但结果没落盘**的窗口是可识别的；
- **replay 策略**：`replay: "never"` 的工具崩溃后不重跑，用最新 checkpoint + 显式中断警告合成一个 interrupted 结果；`replay: "safe"` 的只读工具才允许带原参数重放；
- **非目标**：明确写死"不保证 exactly-once 外部副作用""不重连 provider 流""不做多写者"——把这些留给上层或直接声明不做。

### 踩过的坑（文档自己承认的）

- 【仓库证据】`harness.md` §0.4 的 Slack 例子：**kill 在 provider 流中间**是"唯一真正不确定的窗口"——请求可能已计费、可能产出了也可能没有。方案是已提交的 frame 前缀保留"最近一次持久 partial"，但**不证明请求如何结束**。
- 【仓库证据】`post-wp05-roadmap.md` 记录了性能债：一个 303,920 字节、569 行的真实 mini session 里，477 次 assistant-frame 追加约 118KB，12 次 frame list 删除；推导出"持续变化的 bash 输出按 100ms 更新、2 秒 checkpoint、50KiB/次，十分钟约 15MiB"。
- 【仓库证据】JSONL 的 snapshot compaction（回收死字节）**规范写了但当时没实现**，导致被删除的 pending payload、被覆盖的 `pi.op.state` 会一直占物理字节。

【作者复盘】"可恢复"不是加个重试就行，它要求你把**每一个副作用都显式分成意图与结果两段**。这件事在写第一版时可以不做，但只要你的 Agent 会真的删文件、发请求、扣钱，它就迟早要做。

【给新项目】先问自己：**崩溃后重跑一次这个工具，会不会造成第二次伤害？** 会，就必须有 replay 策略 + 幂等键（Pi 用的是 operation id）。不会，就可以先简化为"恢复即重试"。

---

## 9. 规模化后的自我纠偏：状态机简化与 clean-room 重写

【仓库证据】`packages/agent/docs/runtime-simplification.md`：

- 简化前 runtime 5,358 行 → 第一次简化 4,667 行 → 简化后基底 4,654 行（共减 704 行，13.1%）；
- 删掉了一整套 **ownership 机制**：`installerSignal`、`DriveAbandoned`、`LostOwnership`、`commandDriveOwned`、精确对象 ABA 检查、`finalizedOutcome`；
- 最终 `OperationState` 收敛为**一个判别式 `at` + 13 个直接叶子**（starting / checkpoint / assistant.* / tools / deferred.* / summary.* / navigation），公共 drive 的可见顺序固定为 `prepare → publish intent → perform effect → publish outcome`；
- 明确写下"**不要引入通用 Procedure 接口、runner、scheduler、graph、callback plan**"。

【仓库证据】`pico-work.md` 记录了一次更彻底的 **clean-room 重写**：不从旧 runtime 导入任何代码，但会**读旧测试、把抓到的 bug 逐条变成新实现的测试用例**（就是第 4 节那五条）。

【作者复盘】这是我最想强调的一点：**Agent 的状态机会越长越复杂**。当"所有权/恢复/并发"交织到几百行防御性检查时，正确做法不是继续打补丁，而是先收敛状态模型（13 个叶子），再决定哪些检查该删。Pi 的 `AGENTS.md` 甚至直接规定"不要为了向后兼容而保留代码，除非用户要求"。

【给新项目】给状态机设一条红线：**状态用判别联合穷举，转换只允许少数几个集中入口**。当你发现自己在每个分支重复写"再校验一次状态是否存在"，就说明该收敛了。

---

## 10. 用户摸得到的两件事：TUI 与供应链

### 终端界面（TUI）

【仓库证据】`tui-plan.md` 把备用屏（alt screen）布局系统的决策写得非常清楚：主屏（main screen）的滚动由终端拥有，因此**不假装主屏支持 sticky 行、嵌套滚动、命中测试**；备用屏引入 `VStack`/`HStack`/`ScrollView`，transcript 可滚动、底部 dock 固定，滚轮按指针所在区域路由并支持 scroll chaining，`follow: "end"` 只在下沉到底时跟随。非目标也明确：不做 CSS flexbox、不做网格、不做虚拟化（第一版）。

【作者复盘】终端 UI 的坑几乎都来自"主屏/备用屏语义不同却想用一套 API"。Pi 的解法是加一个显式能力 `ViewportTUI`，主屏不实现它——**宁可让类型系统告诉你"这个模式没这能力"，也不要给主屏伪造一套假语义**。

### 供应链与权限

【仓库证据】根 `README.md` "Supply-chain hardening"：直接依赖精确 pin、`.npmrc` 设 `save-exact`/`min-release-age=2`、以 `package-lock.json` 为唯一事实、发布包内置 `npm-shrinkwrap.json`、CI 用 `--ignore-scripts`、定时 `npm audit`；`AGENTS.md` 把依赖与 lockfile 变更当成"需要人工审查的代码变更"。

【仓库证据】权限模型：Pi **不内置权限系统**，默认以启动用户权限运行，强隔离靠容器化（Gondolin micro-VM / Docker / OpenShell）。

【给新项目】Agent 会执行 shell、写文件，"安全边界"必须在你决定"要不要内置权限系统"时就想清楚。Pi 的选择是：不自造沙箱，把隔离交给容器，把供应链当代码审。这个取舍值得抄。

---

## 11. 踩坑总表（一页速查）

| 环节 | 坑 | 解决方案 |
|------|----|---------|
| LLM 层 | 多 provider 的 token/价格语义不一致（缓存 token 重复计数、缓存定价错） | 统一类型之外，统一语义；用 compat 能力位；模型数据脚本化并可手动覆盖 |
| 主循环 | 截断（`stopReason="length"`）时执行半截 tool call | 不执行，全部标记失败，让模型重发 |
| 主循环 | 停止钩子语义模糊、对异常响应行为不一致 | 收敛为 `finishTurn`，明确 error/aborted 仍硬退出；补 `prepareRequest` |
| 工具 | 并行写同一文件竞态 | `file-mutation-queue` 串行化写，读仍可并发 |
| 工具 | 单个工具想终止整个 agent | 批级 terminate：所有结果都 terminate 才停 |
| 会话 | 流式未完成消息落盘，崩溃后状态撕裂 | 事务 + 拒绝 pending 落盘 + JSONL 撕裂尾行丢弃 |
| 压缩 | 在 toolCall/toolResult 中间切断 | 切点只在非 toolResult 条目；split turn 单独摘 |
| 压缩 | 摘要丢文件/函数/错误信息 | 结构化模板 + `readFiles/modifiedFiles` 台账 |
| 事件 | `agent_end` 提前当 idle；listener 阻塞 | listener 全 settle 后才 idle；事件总线 `structuredClone` 隔离 |
| 可恢复 | 副作用重复执行（重跑 bash） | intent→effect→settlement + replay `never/safe` + operation id 幂等 |
| 可恢复 | 崩溃窗口"可能已计费/已产出" | 明确不确定窗口，保留已提交 frame 前缀，不假装知道结果 |
| 状态机 | 所有权/恢复/并发防御检查爆炸 | 判别联合收敛状态、集中转换入口、删掉冗余检查 |
| 重写 | 失败生成不结束 run、refs 进 patch、collapse 死锁、静默误触发、watch 丢 delta | clean-room + 把旧 bug 逐条变新测试 |
| TUI | 主屏/备用屏语义混用 | 显式能力接口，主屏不假装支持备用屏布局 |
| 供应链 | npm 依赖被投毒 | 精确 pin + lockfile 为准 + shrinkwrap + `--ignore-scripts` + 审计 |

## 12. 小白版：Agent 开发的七步

1. **打通模型**：一套统一消息/工具类型 + 流式事件 + 多协议适配（先支持 1–2 家）。
2. **写出循环**：拿模型回复 → 若有 tool call 就执行并回灌 → 没有就结束；明确终止条件。
3. **加上工具**：schema 校验 → 安全拦截 → 执行 → 异常转 error result → 结果顺序可预测。
4. **加上记忆**：只追加的对话记录 + 可变状态分离；写操作事务化。
5. **加上压缩**：估算 token → 在合法边界切 → 结构化摘要 + 关键信息台账。
6. **加上事件与钩子**：UI 只订阅事件；扩展点写清时机与契约。
7. **加上恢复**：把副作用拆成"意图提交 → 执行 → 结果提交"，为危险操作准备 replay/幂等策略。

【给新项目】这七步**每一步都能单独上线**。不要跳过第 4、7 步就想上生产；也不要在第 1 步就追求支持 30 家 provider。

## 13. 验证清单：怎么知道你做的 Agent 是对的

- 循环：模型输出被 `max_tokens` 截断时，工具参数不完整是否被拒绝执行？
- 工具：并行两个 edit 同一文件，最终文件是否为两次修改的串行结果？
- 工具：一个工具返回 `terminate` 而另一个没有，agent 是否继续？
- 会话：在写消息事务中间 kill 进程，重启后是否看不到半条消息？
- 压缩：压缩后模型是否仍能说出关键文件路径和函数名？
- 事件：UI listener 阻塞时，agent 是否不会提前进入 idle？
- 恢复：危险工具执行到一半重启，是否**没有**第二次执行，且历史里有一条明确的中断结果？
- 状态机：状态用尽判别联合了吗？是否存在重复的"状态再校验"分支？
- 安全：崩溃/重试/恢复三种路径下，外部副作用是否都有幂等键或 replay 策略？

## 14. 结语：Pi 教给我的三句话

1. **先做产品，再抽内核**。框架是被复用需求逼出来的，不是一次设计出来的。
2. **复杂度要收敛，不要堆防御**。状态用判别联合穷举，转换集中；当所有权/恢复检查爆炸时，先重构状态模型。
3. **承认不确定**。副作用、provider 流、崩溃窗口都不会凭空消失；把它们显式建模（intent/effect/settlement、replay、非目标清单），比假装能保证 exactly-once 更工程。

---

## 延伸阅读

- 01 文：12 包 monorepo 分层总览
- 02 文：双层 `while` 主循环逐行精读
- 03 文：工具六阶段生命周期
- 04 文：30+ Provider 统一抽象
- 05 文：会话分支树与安全压缩
- 06 文：10 事件与 10 钩子的精确时机
- `pi/packages/agent/docs/harness.md`：可恢复运行时的规范（intent/effect/settlement、三存储、13 叶子状态）
- `pi/packages/agent/docs/runtime-simplification.md`：状态机简化与删减记录
- `pi/packages/agent/docs/pico/pico-work.md`：clean-room 重写与旧 bug 清单
- `pi/packages/agent/docs/post-wp05-roadmap.md`：性能债与未完成项（含真实字节数测量）
- `pi/tui-plan.md`：备用屏布局系统的决策与非目标
