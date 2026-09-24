# 链路追踪-Sleuth与Zipkin及异步上下文传递

## 1. 场景与边界

- 何时会遇到：一次用户请求跨了网关、订单、商品、用户多个服务，出问题时无法把这些服务的日志串起来，只能按时间猜测。
- 业务目标：为一次请求分配全局可传播的 `traceId`，在每个服务、每条日志、每次远程调用里带上；把 span 数据上报到 Zipkin，能按 traceId 查看完整调用链和耗时。
- 非目标：不做指标（Metrics）与日志聚合平台；不涉及 OpenTelemetry 迁移；不涉及采样率调优的完整策略。
- 适用前提与规模假设：Spring Cloud Sleuth（Brave）方案；服务通过 HTTP/Feign 调用；Zipkin 集中存储；服务数量中小规模。

## 2. 约束与难点

- **上下文传播载体**：Sleuth 用 ThreadLocal（`CurrentTraceContext`）保存 span，跨线程（异步、线程池、MQ 消费）会丢失，必须显式包装。
- **过滤器顺序**：自定义过滤器必须在 Sleuth 建立 span 之后运行，才能拿到 `currentSpan`。
- **上报地址**：Zipkin `base-url` 与 `discovery-client-enabled` 配置不当会导致上报失败或把 Zipkin 当注册中心里的服务去找。
- **网关与非 Servlet 栈**：Gateway 是 WebFlux，追踪实现与 MVC 不同，需要单独确认链路是否连续。
- **采样**：`probability` 过高带来存储/网络压力，过低会漏掉问题请求。
- **存储**：Zipkin 需要存储后端（内存/ES/MySQL），否则重启丢数据。
- **日志关联**：traceId 只有进入日志格式（MDC）才有排障价值；只放在 response header 作用有限。

## 3. 项目中的实现链路

```text
请求进入 server-user
  -> Sleuth 的 TraceFilter 建立/复用 span（traceId、spanId）
  -> 自定义 MyGenericFilter（顺序在 Sleuth 之后）
       取 currentSpan().context().traceIdString()
       写入 request attribute "traceId" 与响应头 "SLEUTH-HEADER"
  -> Controller/日志输出 traceId（MDC 由 Sleuth 注入）
  -> 业务内异步执行: 用 LazyTraceExecutor 包装的线程池，traceId 继续传播
  -> 跨服务调用: Feign/HTTP 自动注入 B3 头，下游继续同一条链
  -> 上报到 Zipkin(base-url)，在 UI 按 traceId 查看
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-user/.../filter/MyGenericFilter.java:24-26` | `@Component`、`@Order(HIGHEST_PRECEDENCE + 6)` | 过滤器顺序在 Sleuth 之后 |
| 【代码事实】 | `shop-user/.../filter/MyGenericFilter.java:30-33` | 构造注入 `Tracer` | 依赖 Brave 的 Tracer |
| 【代码事实】 | `shop-user/.../filter/MyGenericFilter.java:28,39-55` | `DEFAULT_SKIP_PATTERN`、`tracer.currentSpan()`、`setAttribute`、`addHeader` | traceId 落进 request 与响应头 |
| 【代码事实】 | `shop-user/.../controller/UserController.java:68-74` | `/sleuth/filter/api` 读取 request attribute | 验证 traceId 已写入请求上下文 |
| 【代码事实】 | `shop-user/.../controller/UserController.java:28-29` | 注入 `Executor executor` 但类中未使用 | 异步能力预留但未接入业务 |
| 【代码事实】 | `shop-user/.../config/ThreadPoolTaskExecutorConfig.java:17-34` | `@Configuration` + `@EnableAutoConfiguration` + `AsyncConfigurerSupport` + `LazyTraceExecutor` | 异步线程池包装追踪上下文 |
| 【代码事实】 | `shop-user/.../UserStarter.java:20` | `@EnableAsync` | 启用异步执行 |
| 【代码事实】 | `shop-user/pom.xml`、`shop-product/pom.xml`、`shop-order/pom.xml`、`shop-gateway/pom.xml` | `spring-cloud-starter-sleuth`、`spring-cloud-starter-zipkin` | 全链路引入追踪依赖 |
| 【代码事实】 | `docs/nacos/config/chapter23/gateway_group/server-gateway-dev.yaml` | `sleuth.sampler.probability: 1.0`、`zipkin.base-url: http://127.0.0.1:9411`、`discovery-client-enabled: false` | 网关侧采样与上报配置 |
| 【代码事实】 | `shop-gateway/src/main/resources/application.yml`（注释） | `sleuth`/`zipkin` 段 | 本地配置被注释，实际以 Nacos 为准 |
| 【代码事实】 | `shop-gateway/src/main/resources/scripts/mysql.sql` | `zipkin_spans`、`zipkin_annotations`、`zipkin_dependencies` | Zipkin MySQL 存储建表脚本 |

## 4. 关键选型与权衡

- 【代码事实】项目采用 **Spring Cloud Sleuth（Brave）+ Zipkin**，依赖进入所有业务服务与网关。
- **解决了什么**：跨服务 traceId 自动传播；日志按 traceId 串联；Zipkin UI 可看调用链与耗时。
- **牺牲了什么 / 风险**：
  1. `probability: 1.0` 全量采样，教学可行，生产会带来显著存储与上报压力。
  2. `discovery-client-enabled: false` 强制用 `base-url` 直连 Zipkin；若 `base-url` 配错（容器内 `127.0.0.1`），span 会静默丢失。
  3. 自定义过滤器把内部 `traceId` 写入响应头 `SLEUTH-HEADER` 暴露给客户端，需评估安全与渗透测试要求。
  4. `ThreadPoolTaskExecutorConfig` 上同时标注 `@Configuration` 与 `@EnableAutoConfiguration`，后者通常应放在启动类，可能带来自动配置的重复/意外；`AsyncConfigurerSupport` 也只应有一个实现。
  5. **其他服务未显式配置 zipkin**：`shop-order`/`shop-product`/`shop-user` 的 Nacos 配置导出中都没有 `zipkin.base-url`，依赖默认 `http://localhost:9411`。多机部署时这些服务所在机器若没有 Zipkin，span 无法上报。
- 【项目中未发现明确依据】没有日志 pattern 配置（`logging.pattern.level` 中带 traceId 的 MDC 输出）；没有 MQ 消费侧的追踪适配；没有网关到服务链路的端到端验证记录。

## 5. 关键工程实现

### 5.1 为什么自定义过滤器要排在 Sleuth 之后

```java
@Component
@Order(Ordered.HIGHEST_PRECEDENCE + 6)   // 【工程推断】Sleuth 的 TraceFilter 通常排在 HIGHEST_PRECEDENCE 之后不久，+6 是为了确保本过滤器在其之后执行
public class MyGenericFilter extends GenericFilterBean {
    public void doFilter(...) {
        Span currentSpan = tracer.currentSpan();
        if (currentSpan == null) { chain.doFilter(request, response); return; } // 防御
        String traceId = currentSpan.context().traceIdString();
        httpServletRequest.setAttribute("traceId", traceId);
        httpServletResponse.addHeader("SLEUTH-HEADER", traceId);
        chain.doFilter(request, response);
    }
}
```

如果顺序排在 Sleuth 之前，`currentSpan()` 为 null，过滤器就退化成空实现。Sleuth 的 span 由 Web Filter 建立，顺序常量随版本可能变化，`+6` 属于经验值，升级 Sleuth 后需回归。

### 5.2 异步场景必须包装线程池

```java
@Configuration
public class ThreadPoolTaskExecutorConfig extends AsyncConfigurerSupport {
    @Override
    public Executor getAsyncExecutor() {
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        executor.setCorePoolSize(2);
        executor.setMaxPoolSize(5);
        executor.setQueueCapacity(10);
        executor.setThreadNamePrefix("trace-thread-");
        executor.initialize();
        return new LazyTraceExecutor(this.beanFactory, executor); // 关键：包裹追踪上下文
    }
}
```

直接用原生 `ThreadPoolTaskExecutor`，新线程里拿不到父线程的 traceId，异步日志会断链。`LazyTraceExecutor` 会在任务提交时捕获当前 span 并在新线程中还原。

### 5.3 MDC 与日志关联

Sleuth 会把 `traceId`/`spanId` 放进行业标准的 MDC key（`traceId`、`spanId`、`X-B3-TraceId` 等）。要真正用于排障，需要在日志格式里输出，例如 logback pattern：

```xml
<pattern>%d{HH:mm:ss.SSS} [%thread] %-5level [${spring.application.name},%X{traceId:-},%X{spanId:-}] %logger{36} - %msg%n</pattern>
```

仓库未见 logback 配置文件，因此**无法确认日志是否真的带了 traceId**；`MyGenericFilter` 把 traceId 写进 request/响应头，但这不是日志关联的常规做法。

### 5.4 Zipkin 存储与上报

- 上报端点：Zipkin 的 `/api/v2/spans`；Sleuth 通过 HTTP 发送 JSON span。
- `base-url`：Zipkin 服务地址；`discovery-client-enabled: false` 表示不通过服务发现找 Zipkin。
- `shop-gateway/src/main/resources/scripts/mysql.sql` 是标准 Zipkin MySQL schema（`zipkin_spans` / `zipkin_annotations` / `zipkin_dependencies`）。
- README 提到 Zipkin 与 ElasticSearch 版本兼容问题（建议 Zipkin 2.23.16 + ES 7.17.4），属于外部组件选型的坑。

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 已证实（代码） | 异步线程里 traceId 丢失 | 原生线程池不继承 ThreadLocal | 用 `LazyTraceExecutor` 包装 | 仅用户服务配置 | 触发异步任务，比较父子线程日志 |
| 已证实（代码） | `Executor` 注入未使用 | `UserController` 未调用异步 | 未处理 | 预留代码需清理或启用 | 搜索 `executor.` 使用 |
| 潜在风险 | 自定义过滤器拿不到 span | 顺序早于 Sleuth 的 TraceFilter | 用 `+6` 顺序规避 | 升级需回归 | 调整顺序做对照实验 |
| 潜在风险 | 订单/商品服务 span 无法上报 | 未配置 `zipkin.base-url`，默认 localhost | 未处理 | 各服务显式配置 | 查服务日志是否有 Zipkin 连接错误 |
| 潜在风险 | Zipkin 收不到 span | `base-url` 指向 localhost 或网络不通 | 网关显式禁用服务发现 | 需容器网络验证 | 查 Zipkin `/api/v2/spans` 与控制台 |
| 潜在风险 | 全量采样压垮存储 | `probability: 1.0` | 未处理 | 生产设 0.1 或按需 | 压测前后 Zipkin 存储增长 |
| 潜在风险 | traceId 暴露给外部 | 响应头写 `SLEUTH-HEADER` | 未处理 | 脱敏或仅内网 | 抓包查看响应头 |
| 潜在风险 | 日志里没有 traceId | 无 logback pattern 配置 | 未处理 | 加 MDC 到 pattern | 查看实际日志格式 |
| 潜在风险 | 网关与服务端链路不连续 | Gateway 为 WebFlux，传播实现不同 | 仅有 zipkin 配置 | 端到端验证 | 通过网关发请求，Zipkin 看是否一条链 |
| 潜在风险 | MQ 消费链路断开 | `RocketConsumeListener` 无追踪适配 | 未处理 | 消息头携带 traceId | 对比生产/消费日志 |

## 7. 可迁移的落地方案

【落地建议】

1. **先定义 traceId 使用规范**：日志格式统一带 traceId；错误响应是否返回 traceId 由安全团队决定。
2. **采样率按环境设置**：开发/测试 1.0，生产从 0.05~0.1 起步，重点接口可强制采样。
3. **异步与 MQ 必须显式传递上下文**：线程池用 `LazyTraceExecutor`/`TaskDecorator`；MQ 消息头携带 traceId，消费端还原。
4. **上报地址用服务发现或固定域名**，不要用 `localhost`；容器环境确保网络可达。
5. **选择合适存储**：小规模内存/MySQL，规模化 ES/Cassandra；设置保留期与清理策略。
6. **端到端验证**：从网关发一次跨三服务请求，确认 Zipkin 是一条完整链，且各服务日志 traceId 一致。
7. **最小侵入**：用 `LazyTraceExecutor` 而非手工传递；避免把 traceId 写进业务响应头。
8. **演进到 OpenTelemetry**：新项目可直接用 OTel + OTLP，Sleuth 已进入维护模式。

### 最小闭环

`统一日志格式 + 合理采样 + 异步包装 + 可达上报地址 + 存储保留策略 + 端到端验证脚本`。

## 8. 替代方案与适用边界

- **Sleuth + Zipkin**：本项目所用，Spring Cloud 老项目改造量最小。
- **OpenTelemetry + Jaeger/Tempo**：新项目、多云、需要统一指标/追踪/日志时首选。
- **SkyWalking**：需要 APM（拓扑、告警、性能剖析）一体化。
- **仅日志 traceId 不建平台**：服务少、预算有限时，先用 MDC + 统一日志格式。

判断依据：服务规模、是否需要链路可视化、存储与运维成本、是否已有可观测性平台。

## 9. 验收清单

- [ ] 一次跨服务请求在 Zipkin 中是一条完整链，无断点。
- [ ] 各服务日志含 traceId，且统一日志格式。
- [ ] 异步任务、MQ 消费场景下 traceId 不丢。
- [ ] 采样率按环境配置并有记录。
- [ ] 上报失败有告警，不静默丢数据。
- [ ] Zipkin 存储有保留期与容量规划。
- [ ] traceId 的对外暴露策略明确且合规。
- [ ] 网关到服务的链路连续。

## 10. 待确认项

- 网关到各服务的 trace 是否真正贯通，需运行验证。
- 除网关外各服务的 Zipkin 上报地址是否配置、是否可达。
- 生产采样率、存储后端、保留策略均未在仓库体现。
- 响应头 `SLEUTH-HEADER` 是否被保留到生产，未确认。
- 是否存在 logback 配置文件（仓库未见），日志是否真带 traceId。
