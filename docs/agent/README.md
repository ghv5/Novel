# 吃透 Pi Agent 内部逻辑 —— 源码研读系列

> 本系列基于 [Pi Agent](https://pi.dev) 开源 monorepo 源码（`pi/` 目录），逐层拆解一个生产级 Coding Agent 的内部实现。
> 代码引用格式：`packages/agent/src/agent-loop.ts` 等，路径相对于 `pi/` 仓库根目录。

## 系列目录

| # | 文章 | 核心问题 | 主要源码 |
|---|------|---------|---------|
| 1 | [01 架构总览：一个 Coding Agent 的 Monorepo 解剖](01-architecture-overview.md) | Pi 由哪些包组成？它们如何分层协作？ | `README.md`、`package.json`、各包 `index.ts` |
| 2 | [02 主循环：agent-loop.ts 逐行精读](02-agent-loop.md) | 从 `agent.prompt()` 到最终返回，一个 turn 里到底发生了什么？ | `packages/agent/src/agent-loop.ts`、`agent.ts` |
| 3 | [03 工具系统：六阶段生命周期与内置工具](03-tool-system.md) | 工具如何被声明、校验、执行、回灌？sequential 与 parallel 有何区别？ | `types.ts`、`agent-loop.ts` 工具段、`harness/tools/` |
| 4 | [04 多模型层：30+ Provider 的统一抽象](04-multi-model-layer.md) | 如何用一套接口归一化 OpenAI/Anthropic/Google 等 10 种协议？ | `packages/ai/src/`（providers/、api/、types.ts） |
| 5 | [05 会话管理与上下文压缩](05-session-and-compaction.md) | 会话如何持久化、fork、恢复？上下文溢出时如何安全压缩？ | `harness/session/`、`harness/compaction/` |
| 6 | [06 事件流、钩子体系与扩展点](06-events-hooks-extension.md) | 8 种 AgentEvent 如何驱动 UI？9 个钩子各自的精确时机？ | `agent.ts`、`types.ts`（AgentLoopConfig）、`harness/events.ts` |

## 阅读建议

- **快速建立全局观**：先读 01 → 02，理解"双层 while 主循环"是理解 Pi 一切行为的主线。
- **想写自己的 Agent 框架**：重点 02 + 06，Pi 的"无状态低层循环 + 有状态包装器"分层是最值得借鉴的设计。
- **关心多模型接入**：04 给出了 provider→api→统一事件流的完整范式，含懒加载与 compat 能力位。
- **关心长会话稳定性**：05 的 token 估算、切点选取、结构化摘要模板是生产级压缩的完整参考。

## 关键设计速览

```
┌─────────────────────────────────────────────────────────────────┐
│  coding-agent (CLI / interactive / print / rpc / SDK)           │
│    AgentSession: 会话核心，桥接 UI 与 runtime                    │
├─────────────────────────────────────────────────────────────────┤
│  agent (pi-agent-core)                                          │
│    Agent 类（有状态包装器）                                      │
│    runLoop（无状态双层 while 主循环）                             │
│    harness/: tools / session / compaction / events / runtime    │
├─────────────────────────────────────────────────────────────────┤
│  ai (pi-ai)                                                     │
│    Provider 工厂(30+) → API 协议实现(10种) → 统一事件流          │
├─────────────────────────────────────────────────────────────────┤
│  周边: tui / chord / telemetry / durable / protocol / client    │
└─────────────────────────────────────────────────────────────────┘
```

## 一句话总结 Pi 的核心架构

> **无状态低层循环（agent-loop.ts 的 runLoop）+ 有状态包装器（Agent 类）**，
> 以 `AgentEvent` 事件流对外输出，以 `AgentLoopConfig` 钩子向宿主开放 9 个精确时机的扩展点，
> 以 `StreamFn` 把 LLM 调用完全解耦到 pi-ai 包。
