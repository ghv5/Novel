# 分布式事务-SeataAT跨服务订单一致性

## 1. 场景与边界

- 何时会遇到：下单要同时写订单库和扣商品库存，两个操作在不同服务的不同数据库；其中一个成功、另一个失败，会导致超卖或订单与库存不一致。
- 业务目标：把“订单写入 + 库存扣减”纳入一个全局事务，任一步失败时整体回滚；成功时全部提交。
- 非目标：不涉及 TCC/SAGA/XA 模式的完整实现；不涉及 Seata 集群与高可用部署；不涉及跨 MQ 的最终一致。
- 适用前提与规模假设：MySQL + InnoDB；Seata AT 模式（README 声明 Seata `1.4.2`）；参与方均为 Spring Cloud 服务；允许全局锁带来的性能开销。

## 2. 约束与难点

- **本地事务与全局事务的关系**：AT 对每个分支做“本地提交 + undo_log 反向补偿”，业务库必须有 `undo_log` 表。
- **`vgroupMapping` 一致性**：客户端 `tx-service-group` 必须能在 Seata 服务端映射到 TC 集群，否则拿不到全局事务。
- **事务边界与调用方式**：只有经 Seata 代理的数据源/参与的远程调用才在全局事务内；自研 RPC、MQ 需要额外适配。
- **异常必须抛出**：只有异常传播到 `@GlobalTransactional` 方法之外才会触发全局回滚；被降级/吞掉的异常不会回滚。
- **隔离性**：AT 的默认隔离是“读未提交”语义，回滚期间其他事务可能读到中间态；需要 `@GlobalLock` 或改用 TCC 的强隔离场景要特别注意。
- **回滚有效性**：undo_log 记录前后镜像，回滚时校验数据是否被其他事务改动（脏写防护）。
- **性能与全局锁**：全局锁 + 两阶段提交带来额外开销与死锁风险，热点行尤其明显。
- **版本兼容**：Spring Cloud Alibaba 与 Seata 版本必须匹配，否则注册/配置加载会失败。

## 3. 项目中的实现链路

```text
OrderController#submit_order -> OrderServiceV8Impl#saveOrder
  @GlobalTransactional                      // 开启全局事务，向 TC 申请 XID
    1) userService.getUser()                // Feign，只读
    2) productService.getProduct()          // Feign，只读
    3) orderMapper.insert(order)            // 订单库分支事务，写 undo_log，本地提交
    4) orderItemMapper.insert(orderItem)    // 同上
    5) productService.updateCount()         // 商品服务分支事务，扣库存，写 undo_log，本地提交
    6) int i = 1 / 0;                       // 故意抛异常
  -> 异常传播出方法 -> TC 收到 rollback -> 各分支用 undo_log 反向补偿
  -> 订单/明细/库存全部回到事务前
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-order/.../OrderServiceV8Impl.java:48` | `@GlobalTransactional` | 订单方法是全局事务发起方（TM） |
| 【代码事实】 | `shop-order/.../OrderServiceV8Impl.java:88-97` | 扣库存后 `int i = 1 / 0;` | 演示全局回滚 |
| 【代码事实】 | `shop-order/.../OrderServiceV8Impl.java:47-49` | 方法只有 `@GlobalTransactional`，无 `@Transactional` | 与 V7 的本地事务边界不同 |
| 【代码事实】 | `shop-order/.../OrderServiceV9Impl.java:38-42,48` | `@RpcReference(...)` + `@GlobalTransactional` | 与 `bhrpc` 混合使用（XID 传播待确认） |
| 【代码事实】 | `shop-order/src/main/resources/bootstrap.yml` | `alibaba.seata.tx-service-group: ${spring.application.name}-tx_group`、`seata.service.vgroup-mapping`、`seata.registry.nacos`、`seata.config.nacos` | 客户端如何找 TC 并读配置 |
| 【代码事实】 | `shop-product/src/main/resources/bootstrap.yml` | 同上（`server-product-tx_group`） | 商品服务作为参与方（RM） |
| 【代码事实】 | `docs/nacos/config/chapter25/config.txt:15-16` | `service.vgroupMapping.server-order-tx_group=default`、`server-product-tx_group=default` | TC 侧事务分组映射 |
| 【代码事实】 | `docs/nacos/config/chapter25/registry.conf` | `type="nacos"`、`namespace=seata_namespace_001`、`group=SEATA_GROUP` | Seata 服务端注册/配置来源 |
| 【代码事实】 | `docs/nacos/config/chapter25/file.conf`、`config.txt` | `store.mode=db`、`global_table`/`branch_table`/`lock_table` | TC 使用数据库存储会话 |
| 【代码事实】 | `docs/nacos/config/chapter25/mysql.sql` | `global_table`、`branch_table`、`lock_table` 建表 | TC 服务端库结构 |
| 【代码事实】 | `docs/nacos/config/chapter25/mysql_client.sql` | `undo_log` 表（AT 专用） | 业务库必需表，作为客户端脚本提供 |
| 【代码事实】 | `shop-order/pom.xml`、`shop-product/pom.xml` | `spring-cloud-starter-alibaba-seata` | 参与方引入 Seata |
| 【代码事实】 | `shop-bean/src/main/resources/dbscript/20220412/shop.sql` | 只有 `t_order`/`t_order_item`/`t_product`/`t_user` | 业务库迁移脚本**不含** `undo_log` |
| 【代码事实】 | `shop-product/.../mapper/ProductMapper.xml:6-8` | `update t_product set t_pro_stock = t_pro_stock - #{count} where id = #{id}` | 扣库存无 `stock >= count` 校验 |

## 4. 关键选型与权衡

- 【代码事实】项目采用 **Seata AT 模式**：订单服务是发起方（TM），订单与商品服务是参与方（RM），TC（Seata Server）以 DB 模式存储会话，注册到 Nacos `seata_namespace_001`。
- **解决了什么**：跨库写要么全成功要么全回滚；开发者只需加一个注解，无需手写补偿。
- **牺牲了什么 / 风险**：
  1. **需要 `undo_log` + 全局锁 + TC 高可用**，运维复杂度上升。
  2. **性能低于本地事务**，热点数据（同一商品）并发扣减会因全局锁串行化甚至冲突。
  3. **不保证隔离**（AT 默认读未提交语义），对隔离性要求高的场景需配合 `@GlobalLock` 或改 TCC。
  4. **V8 缺少本地 `@Transactional`**：V7 有 `@Transactional`，V8 只有 `@GlobalTransactional`。虽然 Seata 会为分支注册事务并管理本地提交，但方法内多次写库（`orderMapper.insert` + `orderItemMapper.insert`）是否处于同一本地事务、回滚粒度是否理想，需要结合数据源代理确认。建议全局事务方法内**显式声明本地事务边界**。
  5. **建表脚本缺口**：`shop` 库的 `20220412/shop.sql` 不含 `undo_log`，而 AT 回滚依赖它。虽在 `docs/nacos/config/chapter25/mysql_client.sql` 提供了客户端脚本，但未纳入正式迁移，易漏执行。
  6. **与 Feign 降级冲突**：若下游调用走了 fallback 且上层未抛异常，全局事务不会回滚（见“服务调用”篇）。
- 【项目中未发现明确依据】没有 Seata 版本兼容矩阵记录；没有 TC 集群、`store.mode` 生产选型说明；没有全局事务超时配置。

## 5. 关键工程实现

### 5.1 客户端三件事：分组、注册、配置

```yaml
spring:
  cloud:
    alibaba:
      seata:
        tx-service-group: ${spring.application.name}-tx_group   # 声明所属事务分组
seata:
  application-id: ${spring.application.name}
  service:
    vgroup-mapping:
      server-order-tx_group: default      # 分组 -> TC 集群名
  registry:
    nacos: { server-addr: ..., group: SEATA_GROUP, namespace: seata_namespace_001 }
  config:
    type: nacos
    nacos: { ... }                        # 从 Nacos 读 Seata 参数
```

要点：
- `tx-service-group` 的值必须与 TC 侧 `service.vgroupMapping.*` 的键一致。
- `service.vgroup-mapping` 把分组映射到 TC 集群名（这里是 `default`）。
- `registry` 决定客户端如何发现 TC；`config` 决定参数从哪来。
- 项目同时配置了 `spring.cloud.alibaba.seata.tx-service-group` 与 `seata.service.vgroup-mapping`，二者有冗余，注意以生效版本为准。

### 5.2 发起方

```java
@Override
@GlobalTransactional                       // 注意：V8 未叠加 @Transactional
public void saveOrder(OrderParams orderParams) {
    // ... 本库写订单、明细
    productService.updateCount(...);       // 远程分支事务
    int i = 1 / 0;                         // 触发全局回滚
}
```

常见加固写法：

```java
@GlobalTransactional(timeoutMills = 30000, name = "submit-order")
@Transactional(rollbackFor = Exception.class)   // 明确本地事务边界
public void saveOrder(...) { ... }
```

### 5.3 参与方与 undo_log

商品服务 `updateProductStockById` 走 Seata 代理的数据源，执行前把旧值写入 `undo_log`，本地提交；全局回滚时读 `undo_log` 把库存加回去。建表脚本（`docs/nacos/config/chapter25/mysql_client.sql`）：

```sql
CREATE TABLE IF NOT EXISTS `undo_log` (
  `branch_id` BIGINT NOT NULL,
  `xid` VARCHAR(128) NOT NULL,
  `context` VARCHAR(128) NOT NULL,
  `rollback_info` LONGBLOB NOT NULL,
  `log_status` INT NOT NULL,
  `log_created` DATETIME(6) NOT NULL,
  `log_modified` DATETIME(6) NOT NULL,
  UNIQUE KEY `ux_undo_log` (`xid`,`branch_id`)
) ...;
```

**每个参与方的业务库都要有这张表**。AT 分支还依赖 `rollback_info` 中的前后镜像做脏写校验（对应 `client.undo.dataValidation=true`）。

### 5.4 TC 侧表结构

| 表 | 作用 | 关键列 |
|---|---|---|
| `global_table` | 全局事务会话 | `xid`、`status`、`application_id`、`transaction_service_group`、`timeout` |
| `branch_table` | 分支事务 | `branch_id`、`xid`、`resource_id`、`branch_type`、`status` |
| `lock_table` | 全局锁 | 行锁记录，防止并发修改同一数据 |

`store.mode=db` 意味着这些会话持久化到 MySQL，TC 重启后能恢复未完成事务。

### 5.5 超卖缺陷（相邻但重要）

```xml
<update id="updateProductStockById">
    update t_product set t_pro_stock = t_pro_stock - #{count} where id = #{id}
</update>
```

没有 `and t_pro_stock >= #{count}`，也 `t_pro_stock` 是 `int` 有符号，库存可被扣成负数。Seata 解决的是“跨库原子性”，**不解决并发扣减的正确性**。需要配合条件更新或库存预占。

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 已证实（脚本对比） | 业务库缺 `undo_log` 回滚失败 | AT 依赖 undo_log | 单独提供 `mysql_client.sql` | 迁移脚本未包含 | 执行全局事务并触发回滚，看报错 |
| 已证实（代码） | 库存可扣成负数 | SQL 无 `stock >= count` | 未处理 | 条件更新/预占 | 并发扣减到 0 继续扣 |
| 潜在风险 | 全局事务报“no available service / vgroup not found” | `tx-service-group` 与 TC 映射不一致或 TC 未注册 | 两处都配置 | 运行时核对 | 查客户端日志与 Nacos `SEATA_GROUP` |
| 潜在风险 | 库存未回滚但订单回滚 | 分支未纳入全局事务 | 依赖自动代理 | 自研 RPC/MQ 需适配 | 用 V9（bhrpc）对照 |
| 潜在风险 | 降级被当成功导致不回滚 | Feign fallback 吞异常 | V8 有 code 检查抛异常 | 若上层不抛则不回滚 | 关商品服务触发降级观察结果 |
| 潜在风险 | 同一商品并发冲突 | 全局锁竞争 | 未处理 | 业务重试/热点隔离 | 并发压测观察锁冲突/超时 |
| 潜在风险 | 回滚期间读到中间态 | AT 默认读未提交 | 未处理 | `@GlobalLock` 或 TCC | 回滚期间并发读同一行 |
| 潜在风险 | 本地事务边界不清 | V8 无 `@Transactional` | 未处理 | 显式声明 | 检查数据源代理与提交时机 |
| 潜在风险 | 未完成事务堆积 | 无超时/无监控 | 未处理 | 监控 TC 会话 | 查 `global_table` 未完成记录 |

## 7. 可迁移的落地方案

【落地建议】

1. **把 `undo_log` 纳入正式迁移脚本**并做启动检查：启动时校验表存在，缺失即告警/失败。
2. **统一事务分组命名**：`应用名-tx_group`，在 TC 配置集中维护映射，纳入配置管理。
3. **明确回滚异常语义**：`@GlobalTransactional` 默认对 `RuntimeException` 回滚；受检异常、被 catch 的异常、降级返回值都不会触发回滚，需显式处理。
4. **全局事务方法内显式声明本地事务**（`@Transactional`），避免边界模糊。
5. **参与方全覆盖**：任何被全局事务覆盖的写操作，其数据库都要有 undo_log，且数据源被 Seata 代理。
6. **与 Feign 降级共存要谨慎**：全局事务路径上禁用降级或让降级抛异常。
7. **并发正确性单独治理**：条件更新（`where stock >= count`）、乐观锁或预占库存，不要指望 Seata 解决超卖。
8. **性能与热点评估**：同一行高并发写要有重试/排队/分片；压测全局事务 TPS 与锁冲突率。
9. **可观测性**：监控全局事务成功率、回滚率、未完成事务数、TC 会话堆积。
10. **TC 高可用**：`store.mode=db` + 多 TC 实例 + Nacos 注册；规划容量与清理策略。

### 最小闭环

`undo_log 迁移 + 分组映射 + 回滚异常约定 + 本地事务边界 + 参与方全接入 + 降级不吞异常 + 条件更新防超卖 + 监控告警 + 并发压测`。

## 8. 替代方案与适用边界

- **Seata AT**：改造小、适合已有业务 SQL；有全局锁与隔离性代价。
- **Seata TCC**：需要强隔离/复杂补偿、能接受改造成本。
- **SAGA**：长流程、允许中间态可见。
- **本地消息表 / Outbox + 定时补偿**：不需要强一致、追求可用性时更简单可靠。
- **业务层消除分布式事务**：把跨库写收敛到同一库/同一服务，最稳妥。

判断依据：一致性强度、改造预算、性能要求、团队对 Seata 的运维能力。

## 9. 验收清单

- [ ] 所有参与方业务库存在 `undo_log`，且被 Seata 数据源代理。
- [ ] `tx-service-group` 与 TC 端 `vgroupMapping` 完全一致，有自动校验。
- [ ] 全局事务方法有明确本地事务边界与超时。
- [ ] 触发任意分支失败时，其余分支被正确回滚（有演练记录）。
- [ ] 全局事务路径上的降级不会返回“假成功”。
- [ ] 库存并发扣减不会为负（条件更新/预占）。
- [ ] 全局事务成功率、回滚率、TC 会话有监控。
- [ ] 热点行并发压测通过，锁冲突可接受。
- [ ] TC 具备高可用与容量规划。

## 10. 待确认项

- Seata Server 版本与 Spring Cloud Alibaba 2.2.7 的兼容矩阵未在仓库记录。
- `bhrpc` 自定义 RPC 是否支持 XID 传播（`OrderServiceV9Impl`）无法确认。
- 生产环境是 file 还是 db 存储模式、TC 实例数、undo_log 保留策略未体现。
- 全局事务与 RocketMQ 发送的先后顺序是否会导致幻影消息，需结合运行确认。
- V8 无 `@Transactional` 是否为有意设计，未在仓库说明。
