# 吃透 Pi Agent 内部逻辑 —— 源码研读系列

> 本系列基于 [Pi Agent](https://pi.dev) 开源 monorepo 源码（`pi/` 目录），逐层拆解一个生产级 Coding Agent 的内部实现。
> 代码引用格式：`packages/agent/src/agent-loop.ts` 等，路径相对于 `pi/` 仓库根目录。

## 分析基线

- **包版本**：`@earendil-works/pi-coding-agent` / `pi-agent-core` / `pi-ai` 均为 **0.87.0**（`packages/*/CHANGELOG.md` 最新条目，2026-09-21）。
- **仓库提交**：`git rev-parse HEAD` = `16787ad5b2dc748047f314ca1bfe7708f30f54f3`（该工作区 `git HEAD` 已损坏，`git status` 报 `bad object HEAD`，无法确认分支与未提交改动）。
- **行号约定**：文中 `L###` 基于 0.87.0，版本升级后会漂移；定位代码时优先用类/函数/配置键名。

## 已覆盖 / 未覆盖

- **已覆盖**：架构分层（01）、主循环（02）、工具系统（03）、多模型层（04）、会话与压缩（05）、事件与钩子（06）、构建过程手记（07）、可恢复运行时（08）、扩展系统（09）、终端 UI 与布局（10）。
- **未覆盖**：telemetry/protocol/client/server 的协议细节、supply-chain / 发布流程、pico 等实验性子项目。

## 系列目录

| # | 文章 | 核心问题 | 主要源码 |
|---|------|---------|---------|
| 1 | [01 架构总览：一个 Coding Agent 的 Monorepo 解剖](01-架构总览.md) | Pi 由哪些包组成？它们如何分层协作？ | `README.md`、`package.json`、各包 `index.ts` |
| 2 | [02 主循环：agent-loop.ts 逐行精读](02-主循环.md) | 从 `agent.prompt()` 到最终返回，一个 turn 里到底发生了什么？ | `packages/agent/src/agent-loop.ts`、`agent.ts` |
| 3 | [03 工具系统：六阶段生命周期与内置工具](03-工具系统.md) | 工具如何被声明、校验、执行、回灌？sequential 与 parallel 有何区别？ | `types.ts`、`agent-loop.ts` 工具段、`coding-agent/src/core/tools/` |
| 4 | [04 多模型层：30+ Provider 的统一抽象](04-多模型层.md) | 如何用一套接口归一化 OpenAI/Anthropic/Google 等 10 种协议？ | `packages/ai/src/`（providers/、api/、types.ts） |
| 5 | [05 会话管理与上下文压缩](05-会话管理与上下文压缩.md) | 会话如何持久化、fork、恢复？上下文溢出时如何安全压缩？ | `harness/session/`、`harness/compaction/` |
| 6 | [06 事件流、钩子体系与扩展点](06-事件流与钩子体系.md) | 10 种 AgentEvent 如何驱动 UI？10 个钩子各自的精确时机？ | `agent.ts`、`types.ts`（AgentLoopConfig）、`harness/events.ts` |
| 7 | [07 作者手记：PiAgent 是如何一步一步做成的](07-作者手记-PiAgent是如何一步步做成的.md) | 如果从头做 Agent，该按什么顺序？每步的选型、坑与解决方案是什么？ | `packages/*/CHANGELOG.md`、`packages/agent/docs/`、`tui-plan.md` |
| 8 | [08 可恢复运行时：让 Agent 在崩溃后接着跑](08-可恢复运行时.md) | 崩溃/中断后如何保证副作用不重放、未完成的能续上？ | `harness.md`、`values.md`、`assistant-durability.md`、`tool-durability.md` |
| 9 | [09 扩展系统：Prompt Templates、Skills 与热插拔 Extensions](09-扩展系统.md) | 三种扩展形态各自能力与边界？哪些点可挂？ | `coding-agent/docs/extensions.md`、`skills.md`、`prompt-templates.md` |
| 10 | [10 终端界面与交互：主屏 / 备用屏与约束布局系统](10-终端界面与交互.md) | 主屏与备用屏的滚动语义有何根本差异？如何做固定 dock + 可滚动 transcript？ | `tui-plan.md`、`coding-agent/docs/tui.md`、`packages/tui/` |

## 阅读建议

- **快速建立全局观**：先读 01 → 02，理解"双层 while 主循环"是理解 Pi 一切行为的主线。
- **想写自己的 Agent 框架**：重点 02 + 06，Pi 的"无状态低层循环 + 有状态包装器"分层是最值得借鉴的设计。
- **关心多模型接入**：04 给出了 provider→api→统一事件流的完整范式，含懒加载与 compat 能力位。
- **关心长会话稳定性**：05 的 token 估算、切点选取、结构化摘要模板是生产级压缩的完整参考。
- **想系统了解开发过程 / 带新人**：先读 07（作者手记，按阶段串起选型、踩坑与解决方案），再按需回到 01–06 看原理。
- **要动手做自己的 Agent**：07 的“七步法”“踩坑总表”“验证清单”可直接当落地与验收参照。
- **关心崩溃恢复 / 生产可靠性**：08 是本系列最“工程”的一篇，intent→effect→settlement、恢复矩阵、效果门可直接照搬到自己的运行时。
- **想给 Agent 做插件生态**：09 给出了 prompts / skills / extensions 的三级扩展模型与安全边界。
- **做终端/UI**：10 讲清了主屏 vs 备用屏、约束布局与输入路由。

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
│              （08 详述 runtime 的 intent/effect/settlement）      │
├─────────────────────────────────────────────────────────────────┤
│  ai (pi-ai)                                                     │
│    Provider 工厂(30+) → API 协议实现(10种) → 统一事件流          │
├─────────────────────────────────────────────────────────────────┤
│  周边: tui / chord / telemetry / durable / protocol / client    │
│  扩展层: prompts / skills / extensions（09）；UI 见 10           │
└─────────────────────────────────────────────────────────────────┘
```

## 一句话总结 Pi 的核心架构

> **无状态低层循环（agent-loop.ts 的 runLoop）+ 有状态包装器（Agent 类）**，
> 以 `AgentEvent` 事件流对外输出，以 `AgentLoopConfig` 钩子向宿主开放 10 个精确时机的扩展点，
> 以 `StreamFn` 把 LLM 调用完全解耦到 pi-ai 包。
