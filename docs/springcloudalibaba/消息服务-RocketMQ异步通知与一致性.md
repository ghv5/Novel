# 消息服务-RocketMQ异步通知与一致性

## 1. 场景与边界

- 何时会遇到：下单成功后需要做“非核心链路”的动作（通知、积分、发券、数据同步），同步调用会让下单变慢、耦合下游；下游故障也会拖垮下单。
- 业务目标：下单事务完成后，向消息中间件投递一条订单消息；用户服务异步消费该消息，完成通知类动作。
- 非目标：不涉及事务消息、顺序消息、广播消费的完整实现；不涉及消息平台部署与运维。
- 适用前提与规模假设：RocketMQ 4.x（`rocketmq-spring-boot-starter 2.0.3`、`rocketmq-client 4.5.2`）；生产者与消费者都是 Spring Boot 应用；允许“最终一致 + 少量重复”的业务语义。

## 2. 约束与难点

- **投递与本地事务的边界**：消息在数据库事务内发送，事务回滚时消息是否已发出？这是最容易产生不一致的点。
- **消费幂等**：MQ 至少一次投递，重复消费必须有幂等设计。
- **失败重试与死信**：消费失败如何重试、重试多少次、进死信后谁处理。
- **顺序性**：同一订单的多条消息是否需要有序；并发消费会打乱顺序。
- **消息体契约**：直接发送数据库实体会导致字段变更影响消费方，需要独立 DTO 与版本管理。
- **可观测性**：发送结果、消费耗时、失败计数、堆积量需要监控。
- **发送阻塞**：同步发送占用业务线程，需评估耗时与超时。
- **与分布式事务叠加**：在 Seata 全局事务里发送 MQ，回滚时消息无法撤回（见“分布式事务”篇）。

## 3. 项目中的实现链路

```text
OrderController#submit_order
  -> OrderServiceV7/V8/V9#saveOrder
       @Transactional / @GlobalTransactional
       userService.getUser(...)
       productService.getProduct(...)
       插入 t_order / t_order_item
       productService.updateCount(...)   扣库存
       rocketMQTemplate.convertAndSend("order-topic", order)   <-- 发送消息
  -> 事务提交
------------------------------------------------------------
server-user: RocketConsumeListener
  @RocketMQMessageListener(consumerGroup="user-group", topic="order-topic")
  onMessage(Order order) -> log.info("用户微服务收到了订单信息：{}", ...)
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-order/.../OrderServiceV7Impl.java:44-45,48,97` | `RocketMQTemplate`、`@Transactional`、`convertAndSend("order-topic", order)` | 事务方法内发送普通消息 |
| 【代码事实】 | `shop-order/.../OrderServiceV8Impl.java:48,97-99` | `@GlobalTransactional` + `1/0` + 发送 | Seata 场景下的发送点 |
| 【代码事实】 | `shop-order/.../OrderServiceV9Impl.java:44-45,92` | 自研 RPC + 发送 | 另一调用栈下的发送 |
| 【代码事实】 | `shop-user/.../rocketmq/RocketConsumeListener.java:16-22` | `@RocketMQMessageListener(consumerGroup="user-group", topic="order-topic")`、`RocketMQListener<Order>` | 消费端实现 |
| 【代码事实】 | `shop-user/.../rocketmq/RocketConsumeListener.java:20-22` | `onMessage` 仅 `log.info` | 消费逻辑为日志占位 |
| 【代码事实】 | `docs/nacos/config/chapter23/order_group/server-order-dev.yaml` | `rocketmq.name-server: 127.0.0.1:9876`、`rocketmq.producer.group: order-group` | 生产者配置来自配置中心 |
| 【代码事实】 | `docs/nacos/config/chapter23/user_group/server-user.yaml` | `rocketmq.name-server: 127.0.0.1:9876` | 消费者配置来自配置中心 |
| 【代码事实】 | `shop-order/src/main/resources/application.yml`（注释）、`shop-user/.../application.yml`（注释） | `rocketmq.name-server` | 本地配置被注释，实际以 Nacos 为准 |
| 【代码事实】 | `shop-order/pom.xml`、`shop-user/pom.xml` | `rocketmq-spring-boot-starter 2.0.3`、`rocketmq-client 4.5.2` | 依赖版本 |
| 【代码事实】 | `shop-user/src/test/java/io/binghe/shop/rocketmq/test/RocketMQProducer.java:15-28` | `DefaultMQProducer` + `producer.send` | 原生 API 对比示例 |
| 【代码事实】 | `shop-user/src/test/.../RocketMQConsumer.java:17-40` | `DefaultMQPushConsumer` + `ConsumeConcurrentlyStatus` | 原生消费者与返回状态语义 |

## 4. 关键选型与权衡

- 【代码事实】项目采用 **RocketMQ + `rocketmq-spring-boot-starter`**，生产者用 `RocketMQTemplate.convertAndSend`，消费者用 `@RocketMQMessageListener` + `RocketMQListener<T>`。
- 【代码事实】消息体直接是 `Order` 对象（经 JSON 序列化），topic 固定 `order-topic`，消费组 `user-group`；未使用 tag 过滤。
- **解决了什么**：下单与通知解耦；用户服务故障不影响下单主流程；消费能力可独立扩展。
- **牺牲了什么 / 风险**：
  1. **消息发送在数据库事务方法内，先发消息后提交事务**。若提交失败（如 `updateCount` 之后抛异常、或提交阶段失败），已发出的消息无法撤回，下游会收到“不存在的订单”。`OrderServiceV8Impl` 在发送前有 `int i = 1 / 0;`，用于演示“异常时消息不发出”，但只覆盖发送前异常，不覆盖提交失败，而且 V8 本身**没有 `@Transactional`**（只有 `@GlobalTransactional`），本地事务边界与 V7 不同，更需谨慎。
  2. **消费端只打日志**，没有幂等、失败处理、重试策略；接入真实业务（发券、扣积分）后重复消费会出问题。
  3. **消息体直接复用数据库实体 `Order`**：实体字段一变（例如新增状态字段），新旧版本消息不兼容。
  4. **同步发送阻塞业务线程**，未见异步发送或超时配置。
  5. **与 Seata 叠加**：全局事务回滚时消息已发送，形成“幻影消息”，仓库没有补偿。
- 【项目中未发现明确依据】没有事务消息（`sendMessageInTransaction`）、没有 outbox/本地消息表、没有消费幂等表、没有死信处理与重试上限配置、没有区分 topic/tag 的治理。

## 5. 关键工程实现

### 5.1 生产者

```java
@Autowired
private RocketMQTemplate rocketMQTemplate;

@Transactional(rollbackFor = Exception.class)
public void saveOrder(...) {
    // ... 业务写库、扣库存
    rocketMQTemplate.convertAndSend("order-topic", order); // JSON 序列化 Order
}
```

`convertAndSend` 使用配置中的默认生产者组（`order-group`）。消息体是对象，消费者泛型 `Order` 即可自动反序列化。若要拿到发送结果，应改用 `syncSend` 返回 `SendResult`：

```java
SendResult sr = rocketMQTemplate.syncSend("order-topic", order);
// 可据此判断 sendStatus 是否 SEND_OK，并写入本地消息表
```

### 5.2 消费者

```java
@Component
@RocketMQMessageListener(consumerGroup = "user-group", topic = "order-topic")
public class RocketConsumeListener implements RocketMQListener<Order> {
    @Override
    public void onMessage(Order order) {
        log.info("用户微服务收到了订单信息：{}", JSONObject.toJSONString(order));
    }
}
```

`onMessage` 正常返回 = 消费成功；抛异常 = 消费失败，RocketMQ 会按消费组配置重试。`RocketMQListener` 接口本身不返回状态，框架内部据此判定。要自定义并发度、重试次数、tag、消息模式，可用 `@RocketMQMessageListener` 的 `consumeMode`/`messageModel`/`selectorExpression` 等属性。

### 5.3 原生 API 与集成 API 的差异

测试类里的原生写法帮助理解封装：

```java
SendResult sendResult = producer.send(message);          // 同步发送，拿到 sendStatus/msgId
return ConsumeConcurrentlyStatus.CONSUME_SUCCESS;        // 显式返回消费结果
```

对比可知，`RocketMQTemplate` 简化了发送，但**隐藏了 SendResult**；需要确认投递成功（如计入本地消息表）时应使用返回 `SendResult` 的方法。

### 5.4 消息一致性方案对比（面向新项目）

| 方案 | 一致性 | 实现复杂度 | 重复 | 适用 |
|---|---|---|---|---|
| 事务内直接发（本项目） | 差（可能幻影消息） | 低 | 无幂等 | 演示/允许丢失 |
| 提交后发送 + 对账 | 中（可能漏发） | 中 | 需幂等 | 通知类 |
| 本地消息表 + 定时投递 | 好（最终一致） | 中高 | 需幂等 | 订单/支付 |
| MQ 事务消息 | 好（半消息+回查） | 高 | 需幂等 | 强一致要求 |

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 潜在风险 | 收到消息但订单不存在 | 消息在事务提交前发出，提交失败 | 未处理 | 事务消息或本地消息表 | 制造扣库存后提交失败，观察下游 |
| 潜在风险 | 重复消费导致重复发券/积分 | 至少一次投递 + 消费无幂等 | 未处理 | 按消息 key/业务唯一键去重 | 手动重发同一条消息 |
| 潜在风险 | 消费失败无重试上限/死信处理 | 未配置重试策略与死信消费 | 未处理 | DLQ 人工/自动处理 | 让 `onMessage` 抛异常，观察重试 |
| 潜在风险 | 消息顺序错乱 | 并发消费（默认） | 未处理 | 顺序消息或业务容忍乱序 | 同一订单连发多条观察顺序 |
| 潜在风险 | 同步发送拖慢下单 | `convertAndSend` 阻塞 | 未处理 | 异步发送或独立线程 | 压测对比发送前后耗时 |
| 潜在风险 | 消息体不兼容 | 直接发 `Order` 实体 | 未处理 | 独立 DTO + 版本号 | 改 `Order` 字段后新旧版本互发 |
| 潜在风险 | Seata 回滚后留下幻影消息 | 发送在全局事务内 | 未处理 | 补偿/幂等/对账 | 触发 `1/0`，看消费者是否收到 |
| 潜在风险 | 发送失败无感知 | 未使用 `SendResult`/未打点 | 未处理 | 监控发送成功率 | 关闭 broker 观察行为 |

## 7. 可迁移的落地方案

【落地建议】

1. **明确一致性目标**：允许偶尔丢用普通消息；必须最终一致用**事务消息**或**本地消息表 + 定时补偿**。
2. **把发送移出本地事务**：先提交数据库事务再发送，配合对账；或在事务内写本地消息表，由独立任务投递。避免“事务未提交先发消息”。
3. **消费必须幂等**：业务唯一键 + 去重表/Redis Set，或让操作天然幂等（状态机条件更新）。
4. **配置重试与死信**：明确次数、退避；死信有消费方与告警。
5. **消息契约版本化**：独立 DTO，加版本号，向后兼容；不要直接发数据库实体。
6. **topic/tag 治理**：按业务拆分 topic，用 tag 做过滤，避免单 topic 全量消费。
7. **可观测性**：发送成功率、端到端延迟、消费失败数、堆积量、死信数接入监控。
8. **压测与演练**：模拟消费者宕机、网络抖动、重复投递，验证补偿与幂等。
9. **与 Seata 协同**：全局事务内不直接发消息，或发送后记录以便回滚补偿。

### 最小闭环

`独立消息 DTO + 提交后发送/本地消息表 + 消费幂等 + 重试与死信 + 监控告警 + 对账补偿`。

## 8. 替代方案与适用边界

- **RocketMQ 普通消息**：允许少量丢失、追求吞吐（本项目当前用法）。
- **RocketMQ 事务消息 / Kafka 事务**：需要“本地事务与消息发送原子”。
- **本地消息表（Outbox）+ 定时投递**：不依赖 MQ 特性，通用性强。
- **Spring 事件（`ApplicationEvent`）**：仅进程内、非持久化，不能当可靠消息；本项目未用于跨服务。
- **直接同步调用 Feign**：实时性要求高、需要立即拿到结果；承担耦合与故障传播。

判断依据：一致性要求、能否容忍重复/丢失、团队对 MQ 事务特性的掌握度。

## 9. 验收清单

- [ ] 消息发送与数据库事务的先后关系明确，一致性方案与业务目标匹配。
- [ ] 消费端幂等，重复消息不产生重复业务效果。
- [ ] 重试次数、退避、死信处理有配置与消费方。
- [ ] 消息体与数据库实体解耦，具备版本兼容策略。
- [ ] 发送/消费/堆积/死信指标有监控与告警。
- [ ] 有“消费者宕机”“重复投递”演练记录。
- [ ] 投递失败有补偿或对账机制。
- [ ] Seata 全局事务回滚时不会留下未被处理的幻影消息。

## 10. 待确认项

- `user-group` 的重试策略、并发度、死信 topic 是否为默认值，未在仓库体现。
- 生产 broker 地址、是否集群、是否开启 ACL，仓库中为单机示例地址。
- 发送是否要求拿到 `SendResult` 做本地记录，未确认。
- 消息最终用途（是否真的用于通知/发券）在代码中仅有日志，业务规则待确认。
- 是否有独立的消息 DTO/版本管理约定，未见。
