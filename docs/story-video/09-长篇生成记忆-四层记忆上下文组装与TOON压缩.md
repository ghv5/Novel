# 09 长篇生成记忆：四层记忆上下文组装与 TOON 压缩

> 标签：长上下文 / 上下文工程 / 记忆分层 / Token 压缩 / 伏笔一致性
>
> 证据根：`story-video-agent/src/main/java/io/binghe/framework/ai/agent/novel/{ContextAssembler,ToonSerializer}.java`

---

## 1. 场景与边界

写一部长篇小说，模型必须「记得」前面写过什么，否则人物前后矛盾、伏笔断线。但上下文窗口有限、token 昂贵，不可能把整本书都塞进去。

工程问题：

1. **记忆怎么分层**：哪些信息必须始终带、哪些按章召回、哪些只留近期？
2. **召回怎么选**：本章要出场的角色如何从「全量角色」里挑出来？
3. **一致性怎么保**：埋了但没回收的伏笔，如何每次都提醒模型？
4. **token 怎么省**：结构化数据（角色表、章摘要、伏笔表）如何用比 JSON 更紧凑的格式表达？
5. **脏数据怎么容错**：字段可能是 JSON 数组、也可能是逗号分隔。

边界：
- 本文聚焦「上下文组装 + 序列化压缩」。
- 组装结果被谁使用见第 05 篇（`NovelMainAgent` 的工具）。
- 提示词本身的管理（`t_prompts`）见 §6.4。

---

## 2. 约束与难点

| 难点 | 说明 |
| --- | --- |
| 窗口有限 | 四层内容拼起来可能超窗，必须对每层设长度上限 |
| 召回精度 | 角色字段可能是 JSON 数组或逗号串，解析要容错 |
| 伏笔状态 | 「已回收」的判定是集合运算，需要统一的表示 |
| 压缩比 | JSON 冗长，表格（TOON）更省 token |
| 分隔符污染 | 表格用逗号分列，字段内出现逗号会错列 → 必须转义 |
| 查询次数 | 一次组装要查多张表，易产生 N+1 |

---

## 3. 实现链路

### 3.1 四层记忆模型

| 层 | 内容 | 数据来源 | 长度上限 |
| --- | --- | --- | --- |
| A 固定记忆 | 世界观摘要、力量体系、核心规则、禁忌、全书主线、当前卷、当前章概要 | `t_novel_world` / `t_novel_outline` / `t_novel_chapter_plan` | 世界观 2000 / 力量体系 500 / 规则 500 / 禁忌 300 / 主线 200 / 卷主线 1000 |
| B 角色记忆 | **本章出场**角色的完整档案（外貌/性格/能力/关系/当前状态/说话风格） | `t_novel_character`（按章概要 `characters` 字段召回） | 无显式上限（受召回数量影响） |
| C 短期记忆 | 前 `DEFAULT_SHORT_TERM_COUNT=2` 章的**正文全文** | `t_novel` | 无显式上限（潜在超窗点） |
| D 中长期记忆 | 本卷已写章摘要、前卷卷摘要、活跃伏笔清单 | `t_novel` / `t_novel_outline` / `t_novel_chapter_plan` | 章摘要 200 / 前卷卷主线 500 / 伏笔 100 |

四层经 `ToonSerializer.format` 拼成最终 Prompt：

```
=== 固定记忆 ===
...
=== 角色记忆 ===
...
=== 短期记忆（前文正文）===
...
=== 中长期记忆 ===
...
```

### 3.2 角色召回（Layer B）

```
章概要 characters 字段
  ├─ 若以 "[" 开头 → Jackson 解析成 List<String>
  ├─ 否则按 [,，、] 切分（容错）
  └─ 得 characterNames
查询本项目全部角色 → 过滤 name ∈ characterNames → toCharacterDetail(...)
```

### 3.3 活跃伏笔（Layer D 的一致性核心）

```
取 chapterIndex < 当前章 的全部章计划
  ├─ 收集每条计划的 foreshadowing（埋设）→ planted[{plantedChapter, content}]
  └─ 收集每条计划的 payoff（回收）→ resolved Set<content>
返回 planted 中 content ∉ resolved 的项   ← 集合差 = 「埋了但没回收」
```

### 3.4 TOON 表格格式

TOON 用 `名称[数量]{列1,列2,...}:` 头 + 缩进行的方式表达表格，比 JSON 省去大量括号/引号/字段名重复：

| 结构 | 头 | 行格式 |
| --- | --- | --- |
| 角色表 | `characters[n]{name,role,age,appearance,personality,ability,speechTone}:` | `  值,值,...` |
| 章计划表 | `chapterPlans[n]{chapterIndex,volumeIndex,title,emotionCurve,cliffhanger}:` | 同上 |
| 章摘要 | `chapterSummaries[n]:` | `  第i章 标题: 摘要` |
| 伏笔表 | `activeForeshadowing[n]{plantedChapter,content}:` | 同上 |

**关键细节**：`safe()` / `truncField()` 会把字段里的 `,` 替换、`\n` 替换成空格，**防止字段内容破坏表格的列结构**。

### 3.5 证据表

| 环节 | 位置 | 关键内容 |
| --- | --- | --- |
| 组装入口 | `ContextAssembler.java:35-47` | `assemble(projectId, chapterIndex)` → 四层 → `format` |
| 常量 | `ContextAssembler.java:30-31` | `DEFAULT_SHORT_TERM_COUNT=2`、`MAX_WORLD_SUMMARY_LENGTH=2000` |
| Layer A | `ContextAssembler.java:51-115` | 世界观 + 主线 + 当前卷 + 当前章概要 |
| Layer B | `ContextAssembler.java:120-138` | 角色召回 |
| Layer C | `ContextAssembler.java:142-164` | 前 2 章正文 |
| Layer D | `ContextAssembler.java:169-224` | 本卷章摘要 + 前卷摘要 + 活跃伏笔 |
| 活跃伏笔 | `ContextAssembler.java:229-260` | 集合差 |
| 角色名解析 | `ContextAssembler.java:262-276` | JSON 优先，逗号降级（`[,，、]`） |
| JSON 数组解析 | `ContextAssembler.java:278-291` | JSON 优先，逗号降级（`[,，]`） |
| 截断工具 | `ContextAssembler.java:293-300` | `truncate` / `safe` |
| 拼接格式 | `ToonSerializer.java:19-34` | `format` |
| 角色表 | `ToonSerializer.java:42-59` | `toCharacterTable` |
| 角色档案 | `ToonSerializer.java:64-79` | `toCharacterDetail` |
| 章计划表 | `ToonSerializer.java:84-99` | `toChapterPlanTable` |
| 章摘要 | `ToonSerializer.java:104-115` | `toChapterSummaryList` |
| 伏笔表 | `ToonSerializer.java:120-132` | `toForeshadowingTable` |
| 转义 | `ToonSerializer.java:134-142` | `safe` / `truncField`（逗号/换行替换） |
| tone 提取 | `ToonSerializer.java:144-154` | 字符串查找 `"tone"` 字段 |
| 装配 | `.../agent/novel/NovelAgentFactory.java:41-52` | `new ContextAssembler(...)` |
| 提示词服务 | `.../agent/prompt/PromptService.java:21-51` | `getPromptValue`：custom 优先，缺省 defaultValue |

---

## 4. 关键选型与权衡

| 选择 | 理由 | 代价 |
| --- | --- | --- |
| 四层记忆分层 | 固定信息每次带、可变信息按需召回，兼顾一致与成本 | 分层策略需按业务调 |
| 短期记忆用「全文最近 2 章」 | 保证连贯性最强 | **最占 token**，是超窗主因 |
| 中长期用「摘要 + 伏笔表」 | 用压缩信息覆盖长程一致性 | 摘要质量决定效果 |
| TOON 表格替代 JSON | 省 token、可读 | 需手写序列化+转义 |
| 角色按章概要召回 | 只带出场角色，省 token | 依赖章概要 `characters` 字段质量 |
| 伏笔用集合差 | 逻辑直观 | 依赖文本完全相等才能判「已回收」（见 §6.3） |
| 每字段独立截断 | 防止单字段撑爆 | 可能截掉关键信息 |

---

## 5. 关键工程实现

**（1）四层装配（`ContextAssembler.java:35-47`）**

```java
public String assemble(Long projectId, int chapterIndex) {
    NovelChapterPlan plan = novelChapterPlanMapper.selectOne(... projectId + chapterIndex ...);
    String layerA = buildFixedMemory(projectId, plan);
    String layerB = buildCharacterMemory(projectId, plan);
    String layerC = buildShortTermMemory(projectId, chapterIndex, DEFAULT_SHORT_TERM_COUNT);
    String layerD = buildLongTermMemory(projectId, chapterIndex);
    return ToonSerializer.format(layerA, layerB, layerC, layerD);
}
```

**（2）活跃伏笔集合差（`ContextAssembler.java:229-260`）**

```java
List<Map<String,Object>> planted = new ArrayList<>();
Set<String> resolved = new HashSet<>();
for (NovelChapterPlan plan : plans) {
    for (String item : parseJsonArray(plan.getForeshadowing())) {   // 埋设
        Map<String,Object> fs = new HashMap<>();
        fs.put("plantedChapter", plan.getChapterIndex());
        fs.put("content", item);
        planted.add(fs);
    }
    resolved.addAll(parseJsonArray(plan.getPayoff()));              // 回收
}
return planted.stream()
        .filter(fs -> !resolved.contains(String.valueOf(fs.get("content"))))
        .collect(Collectors.toList());
```

**（3）TOON 转义（`ToonSerializer.java:134-142`）**

```java
private static String safe(String s) {
    return s != null ? s.replace(",", "，").replace("\n", " ") : "-";
}
private static String truncField(String s, int maxLen) {
    if (s == null) return "-";
    s = s.replace(",", "，").replace("\n", " ");
    return s.length() <= maxLen ? s : s.substring(0, maxLen) + "...";
}
```

**（4）容错解析（`ContextAssembler.java:262-291`）**：先试 JSON 数组，失败再按中英文逗号/顿号切分。

---

## 6. 踩坑根因排障

### 6.1 【代码事实】短期记忆是「全文最近 2 章」，是超窗的最大风险

Layer C 直接把最近 2 章的 `chapterData`（正文全文）拼进去，且**没有长度上限**。若单章常规 2000–4000 字、模型上下文有限，2 章正文 + A/B/D + 系统提示可能直接超窗。建议：限制单章纳入的最大字符数、或只带「上一章全文 + 更早摘要」。

### 6.2 【代码事实】伏笔一致性依赖「文本完全相等」

活跃伏笔用 `resolved.contains(content)` 判定回收，要求 `payoff` 里的字符串与 `foreshadowing` 里的**完全相同**。一旦模型换一种说法（「陈默的身世之谜」vs「主角身世」），就会被判为「未回收」永远挂在上下文里，越积越多。建议给伏笔加稳定 id，而非靠文本匹配。

### 6.3 【代码事实】角色召回依赖章概要 `characters` 字段

Layer B 只召回 `plan.getCharacters()` 里列出的角色。若该字段缺失/为空，则**返回空**——本章不会带任何角色档案，人物设定可能漂移。而该字段由大纲阶段生成，质量不可控。建议：至少兜底带上「主角 + 主要角色」。

### 6.4 【代码事实】两个 `PromptService` 内容完全相同

仓库里存在两个**逐行相同**的 `PromptService`：

| 类 | Bean 名 | 用途 |
| --- | --- | --- |
| `io.binghe.framework.ai.agent.prompt.PromptService` | `@Service("agentPromptService")` | Agent 侧 |
| `io.binghe.framework.service.PromptService` | `@Service`（默认 `promptService`） | 服务侧 |

两者 `list`/`updateCustomValue`/`getPromptValue` 实现一字不差。**不是 Bean 冲突**（Bean 名显式区分了），但属重复代码：改一处忘另一处会导致行为不一致。建议合并为一个共享组件。

### 6.5 【工程推断】一次组装可能产生多次查询（N+1 风险）

`assemble` 内部依次查：章计划（1）、世界观（1）、大纲主线（1）、卷大纲（1）、角色（1 全量）、前 2 章正文（1）、本卷第几章摘要（1）、前卷大纲（1）、章计划全集（1，`getActiveForeshadowing`）……对单章尚可，但若在循环里逐章调用 `assemble`（长篇写作场景），会退化成 N 倍查询。建议对「正常情况下不变」的世界观/角色做缓存。

### 6.6 【代码事实】`extractTone` 是脆弱的字符串解析

```java
int idx = speechStyleJson.indexOf("\"tone\"");
if (idx < 0) return "-";
int start = speechStyleJson.indexOf("\"", idx + 6);
int end = speechStyleJson.indexOf("\"", start + 1);
return speechStyleJson.substring(start + 1, end).replace(",", "，");
```
用「找 `"tone"` 后第一个引号对」的方式取 tone。若 `speechStyle` 是嵌套对象、或 tone 值本身含转义引号，会解析出错。应由 Jackson 正式解析。

### 6.7 常见现象对照表

| 现象 | 根因 | 排查/处理 |
| --- | --- | --- |
| 请求超长/超 token | 短期记忆全文（§6.1） | 限制单章字符数或改摘要 |
| 伏笔永远「未回收」 | 文本不相等（§6.2） | 引入伏笔 id |
| 人物设定漂移 | 角色未召回（§6.3） | 兜底带主角 |
| 长篇写作越来越慢 | N+1 查询（§6.5） | 缓存世界观/角色 |
| 表格串列 | 字段含逗号未转义 | 确认 `safe`/`truncField` 被使用 |
| tone 取不到 | 字符串解析（§6.6） | 用 JSON 解析 |

---

## 7. 可迁移落地方案

`【落地建议】`

1. **分层记忆 + 各自的预算**：给每层设「token/字符预算」，超预算按优先级裁剪（固定记忆 > 角色 > 长期 > 短期）。
2. **长程一致性用结构化对象**：伏笔/角色/线索都应有稳定 id 与状态字段，用 id 匹配而非文本匹配。
3. **结构化数据用紧凑格式**：表格（TOON）比 JSON 省 token；但**必须转义分隔符**，否则列错位。
4. **召回要兜底**：按需召回失败时，至少带上「主角/关键角色/主线」。
5. **解析要容错**：历史脏数据用「JSON 优先、分隔符兜底」策略；新数据统一用 JSON。
6. **减少重复查询**：对「整本书不变」的设定做进程内缓存或请求内缓存。
7. **可观测**：把「本次组装的四层各自 token 数」打日志，便于持续调优。

---

## 8. 替代方案与适用边界

| 方案 | 适用 | 不适用 |
| --- | --- | --- |
| 分层记忆 + TOON（本方案） | 长篇、需强一致、成本敏感 | 短篇/单次生成（过重） |
| 向量检索 RAG | 海量资料、语义召回 | 精确一致性（伏笔/数值）不保证 |
| 全文塞入长上下文模型 | 窗口足够大、成本不敏感 | 成本高、仍会超窗 |
| 摘要链（recursive summarization） | 超长篇 | 丢失细节 |
| 知识图谱 | 复杂人物关系 | 构建成本高 |

---

## 9. 验收清单

- [ ] 四层内容按 A/B/C/D 顺序拼装，空层不产生空标题。
- [ ] 每层有长度上限；短期记忆有兜底上限。
- [ ] 角色只召回本章出场者，字段缺失时有兜底。
- [ ] 活跃伏笔 = 埋设 − 回收，逻辑正确。
- [ ] TOON 表格中字段内的逗号/换行被转义，不串列。
- [ ] 脏数据（逗号分隔）能容错解析。
- [ ] 长篇逐章组装无 N+1 放大。
- [ ] 组装结果的各层 token 数可观测。

---

## 10. 待确认项

- 单章正文平均长度与模型窗口是否匹配（§6.1 是否真会超窗）。【待确认】
- 伏笔在真实数据里是否常出现「同义不同文」。【待确认】
- `toCharacterTable` 是否仍被调用（当前 Layer B 用的是 `toCharacterDetail`）。【待确认】
- 两个 `PromptService` 是否会被上游按不同 Bean 名注入。【待确认】
- `speechStyle` 的实际 JSON 结构。【待确认】
