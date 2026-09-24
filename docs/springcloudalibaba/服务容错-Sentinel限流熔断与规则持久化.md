# 服务容错-Sentinel限流熔断与规则持久化

## 1. 场景与边界

- 何时会遇到：单个服务流量突增、被上游刷、依赖的第三方变慢，都会把线程/连接耗尽，进而拖垮整条调用链（服务雪崩）。
- 业务目标：在 **Web 入口和相关资源** 上做流量控制与熔断降级；触发限流/熔断时返回**可识别的业务响应**而不是堆栈或超时；规则在重启后不丢失。
- 非目标：不涉及网关侧限流（见“服务网关”篇）、不涉及集群流控、热点参数规则与授权规则的完整调优。
- 适用前提与规模假设：单机或少量实例的 Spring MVC 应用；使用 Sentinel 内存统计；规则来源以本地文件为主；Sentinel `1.8.4`（README 声明）。

## 2. 约束与难点

- **资源定义方式**：Sentinel 默认以 URL 为资源名，但同一业务方法被多个 URL 复用时需要显式 `@SentinelResource` 才能统一统计。
- **异常语义双轨**：限流触发的 `BlockException` 由 `blockHandler` 处理；业务异常的 `fallback` 处理，二者不能混用。
- **统一响应**：未配置时 `BlockException` 直接抛 500，前端拿到框架默认错误。
- **来源识别**：授权规则需要知道“请求来自谁”，必须自定义 `RequestOriginParser`。
- **规则持久化**：Dashboard 推的规则默认只在内存，重启即丢；需要可读可写 `DataSource`。
- **多实例一致性**：文件持久化是**节点本地**的，多实例各自维护，不会互相同步。
- **上下文收敛**：`web-context-unify` 决定同一 URL 前缀下的资源是否合并统计，直接影响链路流控配置。
- **触发链路**：Gateway 用一套 Sentinel 适配器，Web MVC 用另一套；两边的资源名与响应格式不同，需要分别理解（见“服务网关”篇）。

## 3. 项目中的实现链路

```text
HTTP 请求
  -> Sentinel Web Filter（spring-cloud-starter-alibaba-sentinel 自动装配）
       -> 资源: URL 或 @SentinelResource(value)
            -> 触发流控/熔断: BlockException
                 -> BlockExceptionHandler(MyUrlBlockHandler) 统一返回 JSON(HTTP 500)
                 -> 或 @SentinelResource 的 blockHandler(MyBlockHandlerClass)
            -> 业务抛异常: fallback(MyFallbackClass)   // @SentinelResource 场景
  -> 正常返回
规则来源: 本地文件(user.home/sentinel-rules/server-order/*.json)
        <-> Sentinel Dashboard(通过 WritableDataSource 回写)
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-order/.../service/impl/SentinelServiceImpl.java:19,27-33` | `@SentinelResource(value, blockHandlerClass, fallbackClass)` | 方法级资源与两套处理器绑定 |
| 【代码事实】 | `shop-order/.../service/impl/SentinelServiceImpl.java:24,33-40` | `count % 4 == 0` 抛异常 | 制造 25% 异常率用于熔断演示 |
| 【代码事实】 | `shop-order/.../handler/MyBlockHandlerClass.java:12-17` | `public static String blockHandler(BlockException)` | 限流处理（静态方法） |
| 【代码事实】 | `shop-order/.../handler/MyFallbackClass.java:11-16` | `public static String fallback(Throwable)` | 业务异常处理 |
| 【代码事实】 | `shop-order/.../handler/MyUrlBlockHandler.java:22-46` | `implements BlockExceptionHandler`，区分 5 种 `BlockException` | URL 级限流统一响应（HTTP 500） |
| 【代码事实】 | `shop-order/.../parser/MyRequestOriginParser.java:14-18` | `RequestOriginParser` 读 `serverName` 参数 | 授权规则来源解析 |
| 【代码事实】 | `shop-order/.../persistence/SentinelPersistenceRule.java:30-107` | `implements InitFunc`，注册 5 类规则读写数据源 | 规则持久化到本地文件 |
| 【代码事实】 | `shop-order/.../persistence/SentinelPersistenceRule.java:31,34` | `appcationName="server-order"`、`user.home + /sentinel-rules/` | 持久化路径写死、节点本地 |
| 【代码事实】 | `shop-order/src/main/resources/META-INF/services/com.alibaba.csp.sentinel.init.InitFunc` | 内容为 `io.binghe.shop.order.persistence.SentinelPersistenceRule` | SPI 注册，启动自动执行 |
| 【代码事实】 | `docs/nacos/config/chapter23/order_group/server-order-dev.yaml` | `sentinel.transport.dashboard: 127.0.0.1:8888`、`web-context-unify: false` | 接入 Dashboard；关闭链路收敛 |
| 【代码事实】 | `shop-order/.../controller/SentinelController.java:36,44,57,63-67` | `@SentinelResource` 标注 Controller 方法、50% 异常率 | 演示 URL 与方法级资源混用 |
| 【代码事实】 | `shop-order/.../controller/OrderController.java:38-48` | `/test_sentinel`、`/test_sentinel2` | 用于手写流控规则的测试入口 |
| 【代码事实】 | `shop-order/.../service/impl/SentinelServiceImpl.java:43-51` | 类内的 `blockHandler`/`fallback`（未被注解引用） | 同时存在两套写法 |

## 4. 关键选型与权衡

- 【代码事实】项目选择 **Sentinel 内置 Web 适配 + 注解 `@SentinelResource` + 本地文件持久化**，没有引入 Nacos 数据源做规则持久化。
- **解决了什么**：限流/熔断与业务解耦；异常有统一响应；规则重启不丢；Dashboard 可改规则并落盘。
- **牺牲了什么 / 风险**：
  1. **持久化路径写死**：`user.home/sentinel-rules/server-order`，服务名硬编码在代码里（`SentinelPersistenceRule.java:31`），换服务名需改代码。
  2. **多实例规则不一致**：文件是节点本地的，N 个实例有 N 份文件；Dashboard 只把规则推给连接的那个实例（或需逐个推），极易出现实例间行为不一致。
  3. **容器化丢失**：`user.home` 在容器内通常是 `/root`，未挂载持久卷则重启/重建即丢。
  4. **两套 blockHandler 写法并存**：`SentinelServiceImpl` 类内定义了非静态的 `blockHandler`/`fallback`（第 43-51 行），但注解实际引用的是 `MyBlockHandlerClass`/`MyFallbackClass` 的静态方法。类内方法成为死代码，容易误导。
  5. **响应码不一致**：Web 侧 `MyUrlBlockHandler` 返回 HTTP 500；网关侧 `GatewayConfig` 的自定义 block handler 返回 HTTP 200 + code 1001。同一系统两种限流响应，客户端需分别处理。
  6. **授权来源缺失兜底**：`parseOrigin` 返回 `httpServletRequest.getParameter("serverName")`，参数缺失时为 `null`，授权规则可能永不匹配。
  7. **`web-context-unify: false`**：不同 Web 上下文成为独立资源树，链路流控需要按上下文分别配置，容易漏配。
- 【项目中未发现明确依据】仓库没有留存具体流控/熔断规则 JSON，也没有阈值配置记录；规则只存在于运行时文件或 Dashboard。`application.yml` 中 sentinel 配置全部被注释，真实值以 Nacos 为准。

## 5. 关键工程实现

### 5.1 注解式资源 + 两套处理器

```java
@SentinelResource(
    value = "sendMessage2",
    blockHandlerClass = MyBlockHandlerClass.class, // 处理 BlockException（限流/熔断）
    blockHandler = "blockHandler",
    fallbackClass = MyFallbackClass.class,         // 处理业务异常
    fallback = "fallback")
public String sendMessage2() {
    count++;
    if (count % 4 == 0) throw new RuntimeException("25%的异常率"); // 制造异常触发熔断
    return "sendMessage2";
}
```

要点：
- `blockHandler` 方法签名必须与原方法一致并在最后追加 `BlockException`；用 `blockHandlerClass` 引用时必须是 `static`。
- `fallback` 处理业务异常（`Throwable`），**不会**收到 `BlockException`。
- 两者都存在时，限流走 blockHandler，业务异常走 fallback。
- `fallback` 还可以加 `exceptionsToIgnore` 指定不处理的异常。

### 5.2 URL 级统一响应

```java
@Component
public class MyUrlBlockHandler implements BlockExceptionHandler {
    public void handle(req, resp, BlockException e) {
        if (e instanceof FlowException)          msg = "限流了";
        else if (e instanceof DegradeException)  msg = "降级了";
        else if (e instanceof ParamFlowException) msg = "热点参数限流";
        else if (e instanceof SystemBlockException) msg = "系统规则";
        else if (e instanceof AuthorityException) msg = "授权规则不通过";
        resp.setStatus(500);
        resp.setContentType("application/json;charset=utf-8");
        // 写回 {code, codeMsg}
    }
}
```

注意：这里用 HTTP 500 表达限流，与业务异常（被全局异常处理成 HTTP 200 + code 500，见“接口契约”篇）混在一起，客户端很难区分“被限流”与“服务出错”。建议限流用 429，或至少用独立业务码。

### 5.3 规则持久化：读写数据源

```java
// 读：文件变化时刷新内存规则
ReadableDataSource<String, List<FlowRule>> rds =
    new FileRefreshableDataSource<>(flowRulePath, flowRuleListParser);
FlowRuleManager.register2Property(rds.getProperty());

// 写：Dashboard 修改规则时回写文件
WritableDataSource<List<FlowRule>> wds =
    new FileWritableDataSource<>(flowRulePath, this::encodeJson);
WritableDataSourceRegistry.registerFlowDataSource(wds);
```

对 5 类规则（flow/degrade/system/authority/param-flow）重复了同样模式，并通过 SPI（`META-INF/services/com.alibaba.csp.sentinel.init.InitFunc`）在启动时执行。这也是 Sentinel 官方推荐的“本地文件持久化”扩展方式。

### 5.4 规则类型与典型用途

| 规则 | 作用 | 本项目是否演示 | 备注 |
|---|---|---|---|
| 流控（Flow） | QPS/并发数限制，直接拒绝/排队/Warm Up | 是（`/test_sentinel`、`sendMessage`） | 最常用 |
| 熔断（Degrade） | 慢调用比例/异常比例/异常数触发熔断 | 是（`sendMessage2` 25%、`request_sentinel4` 50%） | half-open 自动恢复 |
| 热点参数 | 对某个参数值单独限流 | 仅 `ParamFlowException` 处理分支 | 需 Dashboard 配 |
| 系统规则 | 按 RT/线程数/入口 QPS 保护 | 仅处理分支 | 全局兜底 |
| 授权规则 | 黑白名单 | `MyRequestOriginParser` 提供来源 | 依赖 `serverName` 参数 |

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 已证实（代码） | 规则持久化路径写死 `server-order` | `appcationName` 硬编码 | 未处理 | 多服务复用需参数化 | 改服务名后查看规则目录 |
| 已证实（代码） | 类内 `blockHandler`/`fallback` 是死代码 | 注解引用了静态类 | 未处理 | 删除或统一写法 | 搜索引用关系 |
| 潜在风险 | 多实例规则不一致 | 文件是节点本地的 | 未处理 | 应改用 Nacos/Redis 数据源 | 两实例分别改规则，观察行为差异 |
| 潜在风险 | 容器重启规则丢失 | `user.home` 未挂持久卷 | 未处理 | 挂载 `/root/sentinel-rules` | 重启容器后检查规则 |
| 潜在风险 | 授权规则不生效 | `serverName` 参数缺失来源为空 | 未处理 | 需要默认来源与校验 | 不带参数请求看 origin |
| 潜在风险 | 限流返回 500 与系统错误混淆 | Web 用 500，网关用 200+1001 | 未处理 | 统一契约 | 触发限流比较响应 |
| 潜在风险 | 链路流控配了不触发 | `web-context-unify: false` | 配置了该开关 | 需按上下文逐条配置 | Dashboard 观察资源树 |
| 潜在风险 | 阈值不合理导致误杀 | 仓库无规则内容 | 未处理 | 需压测确定 | 压测观察限流率与业务成功率 |

## 7. 可迁移的落地方案

【落地建议】

1. **先定义“被限制时的响应契约”**：HTTP 状态码（建议 429/503）+ 业务码 + 是否可重试，与业务异常区分开。
2. **资源命名规范化**：URL 资源统一前缀或 `服务名:语义`；方法级用注解；把资源名当对外契约管理。
3. **规则持久化用集中式数据源**（Nacos/ZooKeeper/Apollo），不要用本地文件；多实例一致、重启不丢、可审计。
4. **区分限流、熔断、业务异常三类出口**，分别打点计数与告警；降级返回要安全。
5. **`RequestOriginParser` 覆盖参数缺失**：给出默认 origin 或直接拒绝。
6. **淘汰死代码与重复写法**：一个服务只保留一种 blockHandler 风格。
7. **压测验证**：固定 QPS 验证限流阈值；让下游变慢验证熔断打开与 half-open 恢复。
8. **与网关统一**：Web 与 Gateway 的限流响应尽量一致（见“服务网关”篇）。

### 最小闭环

`明确响应契约 + 规范化资源名 + 集中式规则源 + 三类异常打点 + 压测验证阈值 + 故障演练`。

## 8. 替代方案与适用边界

- **Sentinel**：Java 生态、需要 Dashboard 与丰富规则类型时首选（本项目）。
- **Resilience4j**：轻量、函数式，适合不想引入独立控制台的团队。
- **Hystrix**：已停止维护，新项目不建议（本项目 Feign fallbackFactory 仍依赖 `feign.hystrix` 接口）。
- **网关/Service Mesh 统一限流**：多语言、入口级统一治理时下沉到网关或网格。

判断依据：是否需要独立控制台、规则是否集中管理、是否多语言、团队运维能力。

## 9. 验收清单

- [ ] 限流、熔断、业务异常的响应可区分，且有明确业务码/HTTP 状态。
- [ ] 规则持久化在集中式数据源，多实例一致，重启不丢。
- [ ] `blockHandler` 与 `fallback` 签名正确并分别被触发，无死代码。
- [ ] 授权规则来源解析对缺参场景有默认处理。
- [ ] 熔断后能自动进入 half-open 并恢复。
- [ ] 有压测与故障演练记录，包含限流阈值与熔断生效证据。
- [ ] 限流/熔断事件接入监控与告警。
- [ ] Web 与网关限流响应契约一致。

## 10. 待确认项

- 仓库未保存实际规则内容，阈值与策略无法从代码确认。
- 生产是否已把 `user.home/sentinel-rules` 挂载为持久卷，未见部署文件。
- Dashboard 与应用的 `transport.port`（order 为 9999）在容器网络下是否可达，未验证。
- `web-context-unify: false` 是否为最终生效值（可能被 Nacos 覆盖），未确认。
- Sentinel `1.8.4` 与 Spring Cloud Alibaba `2.2.7` 的兼容性未在仓库记录。
