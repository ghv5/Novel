# 分布式ID-雪花算法生成与多实例冲突风险

## 1. 场景与边界

- 何时会遇到：分库分表或需要跨服务生成全局唯一的业务主键；不想依赖数据库自增（分库后冲突、暴露数据量、写入热点）。
- 业务目标：在应用侧生成**趋势递增、全局唯一**的 `long` 主键；多个服务/多个实例都能生成且不冲突。
- 非目标：不涉及号段模式（数据库/Redis）、UUID 方案的性能基准；不涉及 ID 的对外展示与脱敏。
- 适用前提与规模假设：单机房；每毫秒每节点 ID 需求远小于 4096；实例数量可控（≤32 数据中心 / 每个 ≤32 机器）。

## 2. 约束与难点

- **节点标识唯一**：`datacenterId + machineId` 必须全网唯一，否则同一毫秒会生成重复 ID。这是雪花算法最关键的前提。
- **时钟回拨**：机器时间回退会产生重复或拒绝服务，需要明确策略。
- **并发**：同一节点需要同步或更细粒度方案保证序列号不重复。
- **生成时机**：插入前由应用生成（`IdType.INPUT`）还是数据库自增，决定分库可行性与幂等设计。
- **序列化精度**：`long` 雪花 ID 超过 JavaScript `Number.MAX_SAFE_INTEGER`（2^53），前端/JSON 直接当数字会丢精度。
- **配置加载**：节点标识从配置文件/环境变量/注册中心获取，必须保证每实例不同。
- **生命周期副作用**：在实体构造函数里生成 ID 会让“创建对象”产生副作用，降级、反序列化、临时对象都会消耗 ID。

## 3. 项目中的实现链路

```text
实体构造 (new Order()/new OrderItem()/new Product()/new User())
  -> SnowFlakeFactory.getSnowFlakeFromCache()      // 固定 dc=1, machine=1 的单例
       -> SnowFlake.nextId()                        // synchronized
            -> (当前毫秒 - START_STAMP) << 22
             | datacenterId << 17
             | machineId << 12
             | sequence
  -> 写入对象字段 id
  -> MyBatis-Plus: @TableId(type=IdType.INPUT) 使用应用传入的 id 落库
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:14` | `START_STAMP = 1649735805910L` | 起始时间戳（2022-04-12） |
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:18-30` | 5+5+12 位、位移常量 | 标准雪花位布局 |
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:36-45` | 构造器校验范围 | dc/machine 越界抛异常 |
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:50-70` | `synchronized long nextId()` | 单节点线程安全；时钟回拨抛异常 |
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:52-54` | `currStmp < lastStmp` 抛 `RuntimeException` | 时钟回拨策略是“拒绝生成” |
| 【代码事实】 | `shop-utils/.../id/SnowFlake.java:56-59` | 同毫秒序列自增、用尽等下一毫秒 | 每毫秒 4096 上限 |
| 【代码事实】 | `shop-utils/.../id/SnowFlakeFactory.java:16-20,39-46` | 默认 `dc=1, machine=1`，`getSnowFlakeFromCache()` | 实体使用的固定实例 |
| 【代码事实】 | `shop-utils/.../id/SnowFlakeFactory.java:53-67` | `getSnowFlakeByDataCenterIdAndMachineIdFromCache` | 支持按参数取实例，但实体未使用；缓存“先查后放” |
| 【代码事实】 | `shop-utils/.../id/SnowFlakeLoader.java:15-16,20,39-45` | 读取 `snowflake/snowflake.properties` | 从配置读节点标识的能力 |
| 【代码事实】 | `shop-utils/src/main/resources/snowflake/snowflake.properties`、`shop-bean/.../snowflake.properties` | `data.center.id=1`、`machine.id=1` | 两处配置值完全相同 |
| 【代码事实】 | `shop-bean/.../bean/Order.java:23-25,55-57` | `@TableId(type=IdType.INPUT)`、构造器生成 id | 应用侧生成主键 |
| 【代码事实】 | `shop-bean/.../bean/OrderItem.java:55-57`、`Product.java:44-46`、`User.java:49-53` | 构造器生成 id；`User` 还计算默认密码 | 所有实体统一做法，且 `User` 有额外副作用 |
| 【代码事实】 | `shop-utils/src/test/.../SnowFlakeFactoryTest.java:23` | `getDataCenterId(), getDataCenterId()` | 测试把 dc 当 machine 传入 |
| 【代码事实】 | `shop-bean/src/main/resources/dbscript/20220412/shop.sql` | `id bigint(20) UNSIGNED NOT NULL`、无 `AUTO_INCREMENT` | 数据库侧不自增 |
| 【代码事实】 | `docs/nacos/config/chapter23/all_group/server-all.yaml` | `mybatis-plus.global-config.db-config.id-type: auto` | 全局声明自增，被实体注解覆盖 |

## 4. 关键选型与权衡

- 【代码事实】项目选择**应用侧雪花算法生成 `long` 主键 + MyBatis-Plus `IdType.INPUT`**，数据库主键非自增。
- **解决了什么**：分库分表时主键全局唯一；生成不依赖数据库，无自增热点；ID 趋势递增，B+ 树索引友好。
- **牺牲了什么 / 风险**：
  1. **节点标识配置形同虚设**：实体调用 `getSnowFlakeFromCache()`（固定 `dc=1, machine=1`），`snowflake.properties` 与 `SnowFlakeLoader` 只在测试类中使用。也就是说，**配置里的 `data.center.id/machine.id` 不会影响实际 ID 生成**。多实例部署时所有实例用同一组 `(1,1)`，同一毫秒生成相同 ID 的概率显著上升。
  2. **缓存非原子**：`getSnowFlakeByDataCenterIdAndMachineIdFromCache` 是“先查后放”，并发下可能创建多个同参数 `SnowFlake` 实例，各自维护独立 `sequence` 与 `lastStmp`，反而增加重复风险。
  3. **主键 `long` JSON 精度丢失**：超过 2^53 后，前端 `JSON.parse` 会丢精度。
  4. **时钟回拨直接抛异常**：业务写入失败，没有等待/备用方案。
  5. **构造函数副作用**：任何 `new Order()`（包括 Feign 降级里 `new Product()`、临时对象）都会消耗 ID；`new User()` 还会做 5 次 MD5 计算默认密码。
  6. **`@TableField(fill = FieldFill.INSERT)` 无效**：仓库内无 `MetaObjectHandler`，该填充不会执行，容易误导。
  7. **`SnowFlakeFactoryTest` 传参错误**：把 `getDataCenterId()` 当作 machine 传入，测试没有真正验证“机器维度”。
- 【项目中未发现明确依据】没有节点标识的分配机制（IP/Pod 序号/注册中心下发）；没有为实体生成调用提供动态配置。

## 5. 关键工程实现

### 5.1 位布局与容量

```text
| 1 bit 符号 | 41 bit 毫秒时间戳 | 5 bit 数据中心 | 5 bit 机器 | 12 bit 序列 |
```

- 单节点每毫秒最多 4096 个 ID；
- `datacenterId`/`machineId` 各 0~31；
- 以 `START_STAMP = 1649735805910L` 为基准，可支撑约 69 年。

### 5.2 生成逻辑

```java
public synchronized long nextId() {
    long currStmp = getNewstmp();
    if (currStmp < lastStmp) throw new RuntimeException("Clock moved backwards...");
    if (currStmp == lastStmp) {
        sequence = (sequence + 1) & MAX_SEQUENCE;
        if (sequence == 0L) currStmp = getNextMill();   // 同一毫秒序列用尽，等到下毫秒
    } else {
        sequence = 0L;
    }
    lastStmp = currStmp;
    return (currStmp - START_STAMP) << TIMESTMP_LEFT
         | datacenterId << DATACENTER_LEFT
         | machineId << MACHINE_LEFT
         | sequence;
}
```

### 5.3 实体生成主键

```java
@Data
@TableName("t_order")
public class Order implements Serializable {
    @TableId(value = "id", type = IdType.INPUT)          // 用应用传入的 id
    @TableField(value = "id", fill = FieldFill.INSERT)   // 无 MetaObjectHandler，实际不生效
    private Long id;

    public Order() {
        this.id = SnowFlakeFactory.getSnowFlakeFromCache().nextId();  // 固定 (1,1)
    }
}
```

### 5.4 修正后的工厂用法

```java
// 推荐：从配置读取节点标识，并用 putIfAbsent 保证单例
public final class SnowFlakeFactory {
    private static final ConcurrentMap<String, SnowFlake> CACHE = new ConcurrentHashMap<>(2);

    public static SnowFlake get(long dc, long machine) {
        String key = "snow_flake_" + dc + "_" + machine;
        return CACHE.computeIfAbsent(key, k -> new SnowFlake(dc, machine));
    }
}
// 实体中改为显式传入实例唯一标识
this.id = SnowFlakeFactory.get(dc, machine).nextId();
```

### 5.5 JSON 精度处理

```java
// 方案一：全局把 Long 序列化为字符串
@Bean
public Jackson2ObjectMapperBuilderCustomizer longToString() {
    return builder -> builder.serializerByType(Long.class, ToStringSerializer.instance);
}
// 方案二：字段级 @JsonSerialize(using = ToStringSerializer.class)
// 方案三：前端用字符串/BigInt 解析
```

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 已证实（代码） | 配置的节点标识不影响实体 ID | 实体用 `getSnowFlakeFromCache()`（固定 1,1） | 未处理 | 用带参数的工厂方法 | 改 `snowflake.properties` 后打印实体 ID 的 machine 位 |
| 已证实（代码） | 测试传参错误 | `getDataCenterId()` 用了两次 | 未处理 | 改为 `getMachineId()` | 代码审查/运行测试 |
| 潜在风险 | 多实例生成重复 ID | 所有实例 `(dc=1, machine=1)` | 未处理 | 实例唯一标识 | 同毫秒多实例并发生成，检查重复 |
| 潜在风险 | 并发下创建多个同参数 SnowFlake | 缓存先查后放，非原子 | 未处理 | `putIfAbsent`/`computeIfAbsent` | 并发调用工厂方法 |
| 潜在风险 | 前端 ID 精度丢失 | 超过 2^53 的 JSON number | 未处理 | 序列化为字符串 | 前端解析后比较首尾数字 |
| 潜在风险 | 时钟回拨导致下单失败 | `nextId` 直接抛异常 | 拒绝生成 | 等待/告警/备用 ID | 修改系统时间触发 |
| 潜在风险 | 每次 `new` 消耗 ID + 密码哈希 | 构造器副作用 | 未处理 | 工厂/静态方法显式赋值 | 统计无意义对象创建 |
| 潜在风险 | `id-type: auto` 与 `IdType.INPUT` 冲突 | 全局配置与注解不一致 | 注解优先 | 统一文档 | 观察 insert 是否使用传入 id |
| 潜在风险 | `@TableField(fill=INSERT)` 不生效 | 无 `MetaObjectHandler` | 未处理 | 删除注解或实现处理器 | 检查 id 是否被覆盖 |

## 7. 可迁移的落地方案

【落地建议】

1. **节点标识必须实例唯一**：从环境变量/Pod 序号/注册中心分配，启动时校验并打印；优先用成熟实现（`uid-generator`/`cosid`/`Leaf`），不要自己维护映射。
2. **修正缓存初始化**：用 `putIfAbsent`/`computeIfAbsent` 或容器单例，保证同参数只有一个实例。
3. **ID 序列化为字符串**对外输出，或使用前端能安全处理的类型；数据库仍存 `bigint`。
4. **明确时钟回拨策略**：小幅回拨等待、大幅回拨告警并熔断生成；部署 NTP 并监控时钟偏移。
5. **不要在构造函数里生成 ID**：改为工厂方法或持久化前统一赋值，避免无意义消耗与副作用。
6. **节点标识纳入配置管理**：把 `dc/machine`（或 workerId）和实例编排绑定。
7. **统一 MyBatis-Plus 主键策略**：全局配置与实体注解保持一致，写进开发规范。
8. **验证**：多实例并发压测生成 ID，断言全局无重复；跨毫秒/跨回拨做专项测试。

### 最小闭环

`实例唯一 workerId + 原子缓存 + 字符串序列化 + 时钟监控 + 并发唯一性测试 + 配置化节点标识 + 无副作用生成`。

## 8. 替代方案与适用边界

| 方案 | 有序性 | 依赖 | 唯一性风险 | 适用 |
|---|---|---|---|---|
| 雪花算法 | 趋势递增 | 无（除时钟） | 节点标识/时钟 | 分库分表、低依赖 |
| 号段模式（Leaf-segment） | 连续递增 | 数据库 | 低 | 能接受 DB 依赖 |
| Redis INCR | 连续递增 | Redis | 低 | 依赖 Redis |
| UUID | 无序 | 无 | 极低 | 非主键/幂等键 |
| 数据库自增/序列 | 连续 | 单库 | 分库冲突 | 单库单表 |

判断依据：是否需要分库分表、对连续性/趋势性要求、是否可接受中心化依赖、实例规模。

## 9. 验收清单

- [ ] 每个实例的 workerId 唯一，启动时校验并记录。
- [ ] 实体 ID 生成使用配置化节点标识，而非固定默认值。
- [ ] 同参数 `SnowFlake` 实例唯一（缓存原子）。
- [ ] 多实例并发压测无重复 ID。
- [ ] 时钟回拨有明确策略与告警。
- [ ] 对外 JSON 中 `long` 主键安全（字符串或前端 BigInteger）。
- [ ] 数据库主键类型与 `IdType` 语义一致并写入规范。
- [ ] 测试覆盖机器维度（`machineId`）而不只是数据中心。

## 10. 待确认项

- 生产是单实例还是多实例；如多实例，workerId 如何分配，仓库无证据。
- `snowflake.properties` 中两组相同配置是否为最终意图，无法判断。
- 是否存在 `MetaObjectHandler` 实现（仓库未见），未确认。
- 前端如何消费该 `long` 主键，未在仓库体现。
- `START_STAMP` 是否为最终基准（一旦上线不可修改），未确认。
