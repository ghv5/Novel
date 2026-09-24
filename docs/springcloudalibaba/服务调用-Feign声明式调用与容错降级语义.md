# 服务调用-Feign声明式调用与容错降级语义

## 1. 场景与边界

- 何时会遇到：订单服务要调用用户、商品两个下游 HTTP 接口，接口数量变多，`RestTemplate` 拼 URL、手工反序列化难以维护；下游不可用时需要可控的降级行为，而不是把异常直接抛给用户。
- 业务目标：以接口契约（Java 接口 + 注解）描述远程调用；下游异常/超时/限流时返回**可预期的降级结果**，让上层能区分“真数据”“降级数据”“未找到”。
- 非目标：不涉及 RPC 序列化性能调优、不展开 Sentinel 规则本身（见“服务容错”篇）、不深入 Hystrix（已停更）。
- 适用前提与规模假设：同步 HTTP 调用为主；调用方接受 Feign 动态代理；下游返回结构化结果（对象或统一 `Result`）。

## 2. 约束与难点

- **降级语义不能被吞掉**：`fallback` 无脑返回 `null`/空对象，上层可能把降级当成功，产生脏数据。
- **降级返回值的形状**：随接口返回类型不同（对象/包装结果），降级逻辑必须逐接口实现。
- **异常与降级的关系**：业务异常、网络异常、限流 `BlockException` 都可能进入降级，需要能区分。
- **“错误响应”是否触发降级**：Feign 是否走 fallback，取决于 HTTP 响应状态与整合组件，而不是 body 里的业务码；本项目全局异常处理返回 HTTP 200 + `code=500`（见“接口契约”篇），这类业务失败**不会触发 fallback**。
- **契约重复与漂移**：同一业务能力在 `shop-service-api` 接口、`shop-order` 的 Feign 接口、下游 Controller 三处声明。
- **配置开关**：Feign 与容错组件（Sentinel）的整合必须显式开启，否则 fallback 不生效。
- **超时缺失**：不显式配置时，Feign 依赖底层 Ribbon/HTTP 客户端默认值，容易出现调用方线程长时间阻塞。

## 3. 项目中的实现链路

```text
OrderController#submitOrder
  -> orderService.saveOrder(...)            (V5/V6/V7)
       -> userService.getUser(userId)       @FeignClient(server-user)
       -> productService.getProduct(pid)    @FeignClient(server-product)
       -> productService.updateCount(...)   @FeignClient(server-product)
  -> 下游正常: 返回真实 User/Product/Result
  -> 下游抛异常/超时/被限流: 触发 fallbackFactory -> 返回 id=-1 / code=1001 的“哨兵值”
  -> 下游业务失败但被全局异常处理包成 HTTP200+code=500: 不触发 fallback，Feign 正常反序列化出 code=500
  -> 上层按 哨兵值 / code / null 三种方式判断
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-order/.../fegin/ProductService.java:30-31` | `@FeignClient(value="server-product", fallbackFactory=...)` | fallbackFactory 降级 |
| 【代码事实】 | `shop-order/.../fegin/UserService.java:29-30` | `@FeignClient(value="server-user", ...)` | 用户服务同样工厂降级 |
| 【代码事实】 | `shop-order/.../fegin/ProductService.java:37,43`、`UserService.java:33` | `@GetMapping("/product/get/{pid}")` 等 | 契约含 context-path |
| 【代码事实】 | `shop-order/.../fegin/fallback/ProductServiceFallBack.java:28-42` | 返回 `id=-1` / `code=1001` | 降级“哨兵值”定义 |
| 【代码事实】 | `shop-order/.../fegin/fallback/UserServiceFallBack.java:27-34` | `new User(); user.setId(-1L)` | 降级对象构造副作用 |
| 【代码事实】 | `shop-order/.../fegin/fallback/factory/ProductServiceFallBackFactory.java:30-48` | `FallbackFactory<ProductService>` | 工厂式降级，可拿 cause |
| 【代码事实】 | `shop-order/.../fegin/fallback/factory/UserServiceFallBackFactory.java:29-40` | 同上 | 用户服务工厂 |
| 【代码事实】 | `shop-order/.../OrderStarter.java:20` | `@EnableFeignClients` | 开启 Feign 扫描 |
| 【代码事实】 | `docs/nacos/config/chapter23/order_group/server-order-dev.yaml` | `feign.sentinel.enabled: true` | 启用 Feign 与 Sentinel 整合 |
| 【代码事实】 | `shop-order/.../OrderServiceV5Impl.java:50,54,77-80` | 调用后仅判 `result.getCode()` | V5 未识别对象型降级 |
| 【代码事实】 | `shop-order/.../OrderServiceV6Impl.java:54,61` | 判 `user.getId()==-1`、`product.getId()==-1` | V6 开始识别哨兵值 |
| 【代码事实】 | `shop-order/.../OrderServiceV7Impl.java:54-95` | 完整校验链 + MQ 发送 | V7 成为稳定版（被多数版本沿用逻辑） |
| 【代码事实】 | `shop-service-api/.../service/ProductService.java:10-21`、`UserService.java:10-15` | `getProductById`/`updateProductStockById`/`getUserById` | 与 Feign 接口并存的第二套契约 |
| 【代码事实】 | `shop-order/.../OrderServiceV9Impl.java:38-42` | `@RpcReference(...)` 调用 `io.binghe.shop.service.*` | 第三套调用方式：自研 RPC |
| 【代码事实】 | `shop-order/pom.xml`、`shop-product/pom.xml`、`shop-user/pom.xml` | `spring-cloud-starter-openfeign` + `bhrpc-*-starter` | Feign 与自研 RPC 双栈 |

## 4. 关键选型与权衡

- 【代码事实】项目同时保留两套 HTTP 降级写法：
  - **直接 fallback**（`ProductServiceFallBack`、`UserServiceFallBack`）：实现 Feign 接口，接口一多就要为每个方法写降级逻辑。
  - **fallbackFactory**：通过 `create(Throwable cause)` 拿到具体异常，可在工厂里构造带原因的降级实现。
- 【代码事实】Feign 接口上 `fallback` 与 `fallbackFactory` 同时存在时，实际启用 `fallbackFactory`（`fallback` 被注释）。框架只允许一个生效。
- 【代码事实】项目还有第三套调用方式：`shop-service-api` 的 `ProductService`/`UserService` 接口 + `@RpcService`/`@RpcReference`（`bhrpc`），与 Feign 接口**同名不同包**，见 `io.binghe.shop.service.ProductService` 与 `io.binghe.shop.order.fegin.ProductService`。
- **解决了什么**：调用代码变成接口调用；下游异常有统一出口；降级结果可用哨兵值传递。
- **牺牲了什么 / 引入的风险**：
  1. **降级返回“假对象”掩盖故障**：调用方不检查 `id == -1` 就会用假数据继续计算，V5 是反例。
  2. **哨兵值 `-1`/`1001` 是隐式契约**，无常量/枚举约束，易写错。
  3. **降级对象构造有副作用**：`new User()` 会生成雪花 ID、并计算默认密码（`User.java:49-53`），随后只覆盖了 id；`new Product()` 同理生成雪花 ID。降级不应该消耗 ID 或做密码哈希。
  4. **业务失败不触发降级**：全局异常处理把异常包成 HTTP 200，Feign 视为成功响应，`fallbackFactory` 不触发，故障原因不会进入 `create(cause)`，可观测性进一步下降。
  5. **三套契约并存**：Feign 接口、`shop-service-api` 接口、Controller 三处同名方法，参数名/路径不一致（`getProduct` vs `getProductById`、`updateCount` vs `updateProductStockById`），长期漂移风险高。
- 【项目中未发现明确依据】没有 Feign 超时、重试、日志级别、压缩的显式配置；`fallbackFactory` 里的 `cause` 未被记录或上报。

## 5. 关键工程实现

### 5.1 降级哨兵值协议

```java
// 对象型：id=-1 表示“降级/未找到”
public Product getProduct(Long pid) {
    Product product = new Product(); // 构造函数生成雪花 id（副作用）
    product.setId(-1L);
    return product;
}

// 结果型：code=1001 表示“触发了容错逻辑”
Result<Integer> r = new Result<>();
r.setCode(1001);
r.setCodeMsg("触发了容错逻辑");
```

上层必须知道这两个约定：

```java
// V6/V7
if (user.getId() == -1) throw new RuntimeException("触发了用户微服务的容错逻辑");
if (product.getId() == -1) throw new RuntimeException("触发了商品微服务的容错逻辑");
Result<Integer> result = productService.updateCount(...);
if (result.getCode() == 1001) throw new RuntimeException("触发了商品微服务的容错逻辑");
if (result.getCode() != HttpCode.SUCCESS) throw new RuntimeException("库存扣减失败");
```

### 5.2 V5 / V6 / V7 的降级处理对比

| 版本 | 用户降级判断 | 商品降级判断 | 库存降级判断 | 后果 |
|---|---|---|---|---|
| V5 | 只判 null | 只判 null | 判 `!=200` | 降级对象被当真实数据，可能用 `id=-1` 建单 |
| V6 | `id==-1` | `id==-1` | 判 `!=200`（含 1001） | 对象型安全，库存型靠 `!=200` 覆盖 |
| V7 | `id==-1` | `id==-1` | 显式 `==1001` 再 `!=200` | 语义最清晰，作为参考实现 |

注意 V5 的对象型接口连 null 都没判全：`ProductController#getProduct` 查不到会返回 `null`，而非降级。

### 5.3 fallback 与 fallbackFactory 的选择

```java
// 只需要“一个兜底对象”，用 fallback
@FeignClient(value="server-product", fallback=ProductServiceFallBack.class)

// 需要根据异常类型/原因做不同降级，用 fallbackFactory
@FeignClient(value="server-product", fallbackFactory=ProductServiceFallBackFactory.class)
```

`fallbackFactory` 的 `create(Throwable cause)` 是**唯一能看到真实异常**的地方，适合分类（超时/限流/连接失败）与日志上报：

```java
public ProductService create(Throwable cause) {
    log.error("调用商品服务失败", cause);   // 项目未做，建议补上
    return new ProductService() { ... };
}
```

### 5.4 双栈调用：Feign vs 自研 RPC

| 维度 | Feign（本项目主用） | `bhrpc`（V9 实验） |
|---|---|---|
| 协议 | HTTP + JSON | 自定义（protostuff 序列化） |
| 注册 | Nacos | ZooKeeper |
| 负载 | Ribbon/LoadBalancer | `zkconsistenthash` |
| 契约 | `@FeignClient` 接口 | `@RpcService`/`@RpcReference` 接口 |
| 与 Seata | 走 HTTP，自动带 XID（需适配） | 需自研框架支持 XID 传播 |

`OrderServiceV9Impl` 同时使用 `@RpcReference` 与 `@GlobalTransactional`，但 `bhrpc` 源码不在本仓库，无法确认 XID 是否传播（见索引“待确认场景”）。

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 已证实（版本对比） | V5 用降级数据继续建单 | V5 只判 `result.getCode()`，未识别对象型降级 | V6/V7 补 `id==-1` | 哨兵值无常量约束 | 关掉下游，用 V5/V6 提交订单对比结果 |
| 已证实（代码） | 降级消耗雪花 ID / 计算密码 | 降级实现 `new` 实体 | 未处理 | 应用无副作用值对象 | 单元测试调用降级实现，观察实体字段 |
| 潜在风险 | 业务失败不触发降级 | 全局异常处理返回 HTTP 200 | 未处理 | 需统一错误契约 | 让下游抛业务异常，确认不进入 `create` |
| 潜在风险 | 降级原因完全丢失 | `cause` 未记录 | 未处理 | 日志/Metric 按异常分类 | 看降级时是否只有哨兵值、无堆栈 |
| 潜在风险 | Feign 超时默认值不适合生产 | 无 `feign.client.config` | 未处理 | 显式 connect/read timeout | 注入延迟观察等待时长 |
| 潜在风险 | 契约漂移 | 三处同名不同方法 | 未处理 | 契约测试 | 对比 Feign 路径与 Controller 注解 |
| 潜在风险 | fallback 与 fallbackFactory 共存困惑 | 框架只允许一个 | 注释保留另一写法 | 团队约定 | 代码评审 |
| 潜在风险 | Feign 与 Sentinel 整合未开则降级静默失效 | 配置开关在 Nacos | 配置了 `enabled: true` | 环境可能覆盖 | 关下游触发限流，确认走 fallback |

## 7. 可迁移的落地方案

【落地建议】

1. **先定义降级协议**：对象型接口用 `Optional`/专用错误对象，结果型接口用统一 `Result` + 集中错误码；避免裸 `-1`/`1001`。
2. **统一用 `fallbackFactory`**，在 `create(cause)` 中记录异常类型、关键参数、耗时，并区分超时/限流/连接失败。
3. **业务层强制校验降级结果**：把“是否降级”编码进返回结构（如 `Result.degraded=true`），而不是靠 id 值猜测。
4. **让“业务失败”也触发可控路径**：要么统一用非 2xx，要么调用方封装 `ResultUtils.check()`；不要让 HTTP 200 掩盖业务失败。
5. **显式配置超时与重试**：`connectTimeout`/`readTimeout`，只对幂等接口重试。
6. **开启并验证整合开关**：`feign.sentinel.enabled=true`，用“关掉下游”演练确认 fallback 真被触发。
7. **收敛契约到一处**：以 `shop-service-api` 或 OpenAPI 为单一事实来源，Feign 接口与 Controller 从契约生成/校验，避免三套漂移。
8. **降级埋点**：降级计数、耗时、异常分类接入监控，否则降级静默存在。

### 最小闭环

`Feign 接口 + 统一 Result/错误码 + fallbackFactory + 降级计数 + 超时配置 + 关闭下游演练 + 契约测试`。

## 8. 替代方案与适用边界

- **`@LoadBalanced RestTemplate`**：接口少、逻辑简单时更轻，但缺声明式契约与统一降级。
- **WebClient / RestClient**：高并发、非阻塞场景，改造量大。
- **Dubbo / gRPC**：内部服务量大、追求性能与强契约时；本项目 `bhrpc` 是同类实验。
- **不要用降级实现业务逻辑**：降级只应返回“安全的空/错误”，不应伪造可继续计算的业务数据；业务必须继续时改用异步补偿。

判断依据：接口数量、性能要求、团队对 Feign/网关/Sentinel 的熟悉度、是否需要与现有 Seata 链路协同。

## 9. 验收清单

- [ ] 每个 Feign 接口都有明确降级行为，且降级结果可被上层识别。
- [ ] 降级对象无副作用（不生成 ID、不做加密运算）。
- [ ] 降级原因被记录/上报，可区分超时、限流、连接失败、业务异常。
- [ ] Feign 连接与读取超时显式配置，且小于上游接口超时。
- [ ] 有“下游不可用”演练记录，确认降级路径被触发而非静默成功。
- [ ] 不存在“降级后仍写订单/扣库存”的路径。
- [ ] 契约只有一处事实来源，Feign 路径与 Controller 一致。
- [ ] `feign.sentinel.enabled` 在目标环境为 true 且有变更记录。

## 10. 待确认项

- 目标环境是否默认启用 Sentinel 与 Feign 整合；`bootstrap.yml` 未包含该开关，需以 Nacos 实际配置为准。
- Feign 超时、重试、压缩、日志级别参数未在仓库体现。
- 降级是否接入监控告警，仓库无证据。
- `bhrpc` 的 `@RpcReference` 与 Feign 是否长期并行，或仅为教程示例，未确认。
