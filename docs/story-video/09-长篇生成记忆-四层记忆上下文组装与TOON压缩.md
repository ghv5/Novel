# 09 长篇生成记忆：四层记忆上下文组装与 TOON 压缩

> 标签：长上下文 / 记忆分层 / 伏笔追踪 / Token 压缩 / TOON

---

## 1. 场景与边界

写长篇小说时，模型需要「记得」前文：世界观、主要角色、前几章正文、全书摘要、以及**尚未回收的伏笔**。但上下文窗口有限，不能整本灌进去。

平台的解法是**四层记忆组装** + **TOON 表格压缩**：

| 层 | 名称 | 内容 |
| --- | --- | --- |
| A | 固定记忆 | 世界观摘要 + 人物关系 + 全书大纲 + 当前卷 + 当前章概要 + 伏笔/回收 |
| B | 角色记忆 | 仅本卷/本章**出场角色**的详细档案 |
| C | 短期记忆 | 前 N 章（默认 2）**完整正文** |
| D | 中长期记忆 | 各章**摘要** + 活跃伏笔清单 |

边界：本场景讲上下文组装与压缩；工具循环见第 05 篇。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| Token 预算有限 | 需要分层裁剪与压缩 |
| 角色太多 | 只注入「本章出场」角色，避免无关角色占 token |
| 伏笔追踪 | 需计算「已埋未回收」的伏笔 |
| 结构化数据量大 | 角色/章节/伏笔用表格压缩（TOON）而非 JSON |
| 字段格式不统一 | `characters`/`foreshadowing` 可能是 JSON 数组也可能是分隔串 |

---

## 3. 实现链路

### 3.1 组装

```
assemble(projectId, chapterIndex)
  plan = chapterPlan(projectId, chapterIndex)
  layerA = buildFixedMemory(projectId, plan)
  layerB = buildCharacterMemory(projectId, plan)      # 仅出场角色
  layerC = buildShortTermMemory(projectId, chapterIndex, 2)  # 前 2 章正文
  layerD = buildLongTermMemory(projectId, chapterIndex)      # 摘要 + 活跃伏笔
  return ToonSerializer.format(layerA, layerB, layerC, layerD)
```

### 3.2 活跃伏笔（集合差）

```
getActiveForeshadowing(projectId, beforeChapter)
  plans = chapterPlan where chapterIndex < beforeChapter
  planted = 所有 foreshadowing 项
  recovered = 所有 payoff 项
  active = planted - recovered     # 已埋且尚未回收
```

### 3.3 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| 组装器 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/novel/ContextAssembler.java:23` | `class ContextAssembler` |
| 常量 | `ContextAssembler.java:31-32` | 短期章数 = 2；世界观摘要上限 = 2000 |
| 组装 | `ContextAssembler.java:37-48` | 四层 + `ToonSerializer.format` |
| Layer A | `ContextAssembler.java:54-124` | 世界观/关系/大纲/当前卷/当前章/伏笔 |
| Layer B | `ContextAssembler.java:126-145` | `filter(c -> characterNames.contains(c.getName()))` |
| Layer C | `ContextAssembler.java:151-170` | `fromIndex = max(1, chapterIndex - count)` |
| Layer D | `ContextAssembler.java:178-231` | 各章摘要 + 活跃伏笔表 |
| 活跃伏笔 | `ContextAssembler.java:240-273` | `planted - recovered`（`foreshadowing` vs `payoff`） |
| 角色名解析 | `ContextAssembler.java:275-285` | 兼容 JSON 数组与分隔串 |
| JSON 解析 | `ContextAssembler.java:291` | `parseJsonArray` |
| TOON 汇总 | `story-video-agent/src/main/java/io/binghe/framework/ai/agent/novel/ToonSerializer.java:16-147` | `format/toCharacterTable/toCharacterDetail/toChapterPlanTable/toChapterSummaryList/toForeshadowingTable` |
| 截断 | `ToonSerializer.java:141` | `truncField` |
| 语气提取 | `ToonSerializer.java:147` | `extractTone`（从 `speechStyle` JSON 取 tone） |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| 四层记忆 | 按「变化频率」分层，稳定信息少变、近期信息全量 | 组装逻辑复杂 |
| 角色按出场过滤 | 大幅省 token | 需维护「本章出场角色」字段 |
| 短期只取前 2 章 | 控制正文长度 | 更早细节靠摘要 |
| TOON 表格压缩 | 比 JSON 更省 token、更易读 | 需自研序列化、模型需理解表格 |
| 伏笔集合差 | 精准追踪未回收伏笔 | 依赖 `foreshadowing`/`payoff` 字段质量 |

---

## 5. 关键工程实现

**（1）四层拼装（`ContextAssembler.java:43-48`）**
```java
String layerA = buildFixedMemory(projectId, plan);
String layerB = buildCharacterMemory(projectId, plan);
String layerC = buildShortTermMemory(projectId, chapterIndex, DEFAULT_SHORT_TERM_COUNT);
String layerD = buildLongTermMemory(projectId, chapterIndex);
return ToonSerializer.format(layerA, layerB, layerC, layerD);
```

**（2）活跃伏笔（`ContextAssembler.java:240-273`）**：遍历 `chapterIndex < beforeChapter` 的所有章节计划，收集 `foreshadowing` 与 `payoff`，差集即「仍活跃」。`【工程推断】` 该算法假设 payoffs 描述与 foreshadowings 描述可精确匹配（字符串相等）。

**（3）TOON 压缩（`ToonSerializer.java:45-135`）**：角色/章节/伏笔等结构化数据以表格文本输出，字段截断（`truncField`）控制长度。【代码事实】

---

## 6. 踩坑根因排障

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| 模型「忘记」前文 | 短期层只取 2 章，且摘要缺失 | 检查 Layer C/D 是否为空；补章节摘要 |
| 注入大量无关角色 | `plan.characters` 与角色名不匹配 | 检查 `parseCharacterNames` 与名称一致性 |
| 伏笔重复/漏回收 | 集合差用字符串精确匹配，表述不一致即失配 | 建议给伏笔加稳定 ID 而非描述匹配 |
| 上下文超长 | 世界观摘要/正文未截断 | 调 `MAX_WORLD_SUMMARY_LENGTH`、`truncField` |
| 角色语气丢失 | `speechStyle` JSON 结构不符 | 检查 `extractTone` 解析 |
| 字段解析异常 | `characters` 非 JSON 也无分隔符 | `parseCharacterNames` 走分隔分支 `:285` |

---

## 7. 可迁移落地方案

`【落地建议】` 一套可复用的「长文生成记忆」方案：

1. **按变化频率分层**：静态（世界观/大纲）、半静态（角色）、近期（前 N 章全文）、历史（摘要）。
2. **按相关性过滤**：只注入与当前任务相关的实体（如出场角色）。
3. **结构化数据压缩**：用紧凑表格（TOON/CSV）替代 JSON，节省 token。
4. **实体用 ID 追踪**：伏笔/线索应有稳定 ID，回收用 ID 匹配而非文本相等，避免改写导致失配。
5. **字段强制截断**：每个层都设最大长度，保证总预算可控。
6. **可插拔**：把「组装策略」抽象为接口，便于按体裁切换不同记忆方案。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| 四层记忆 + 表格压缩（本方案） | 长篇连载生成 | 短文本 |
| 向量检索 RAG | 海量素材、按相似度召回 | 强结构化、需精确时序 |
| 全量长上下文模型 | 成本不敏感、窗口足够 | 超长篇 |
| 摘要树/递归摘要 | 极长文本分层摘要 | 实现复杂 |

---

## 9. 验收清单

- [ ] 四层内容均非空时能正确拼装。
- [ ] 仅注入本章出场角色。
- [ ] 短期层取前 N 章正文，边界（第 1、2 章）不越界。
- [ ] 活跃伏笔 = 已埋未回收，回收后不再出现。
- [ ] 各字段按上限截断，总长度可控。
- [ ] TOON 表格可读且解析稳定。

---

## 10. 待确认项

- `foreshadowing`/`payoff` 的实际存储格式与匹配规则（是否靠字符串相等）。【待确认】
- `characters` 字段的权威来源与格式（决定 `parseCharacterNames` 分支）。【待确认】
- TOON 是否为自研格式、模型对表格的理解效果。【待确认】
- Layer D「当前卷」的卷划分依据。【待确认】
