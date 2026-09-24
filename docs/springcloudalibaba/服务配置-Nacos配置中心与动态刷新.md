# 服务配置-Nacos配置中心与动态刷新

## 1. 场景与边界

- 何时会遇到：多环境（dev/test/prod）配置不同、多个服务共享数据库连接等公共配置、改一个参数要重新打包发版。
- 业务目标：把配置从代码/镜像里抽出来集中管理；按环境隔离；公共配置可共享；应用运行期能感知配置变更。
- 非目标：不做配置加密与权限审计、不做配置灰度发布、不涉及多集群 Namespace 治理。
- 适用前提与规模假设：Nacos 作为配置中心；Spring Boot 2.3.x + Spring Cloud Hoxton（bootstrap 阶段默认可用）；配置以 YAML 为主。

## 2. 约束与难点

- **加载时机**：配置必须早于数据源等 Bean 创建，因此要靠 `bootstrap` 阶段。Spring Cloud 2020 之后需额外引入 `spring-cloud-starter-bootstrap`，本项目 Hoxton 默认可用的 `bootstrap.yml` 方式。
- **隔离维度**：需要同时利用 **DataId（应用名+profile）** 与 **group** 两个维度隔离；命名混乱容易读到别人的配置。
- **共享配置优先级**：`shared-configs`、`extension-configs`、应用专属配置、`bootstrap.yml` 本地配置的覆盖顺序必须明确。
- **动态刷新范围**：不是所有配置都能热更新；数据源、连接池、线程池需要专门支持，`@RefreshScope` 只对 `@Value`/`@ConfigurationProperties` 生效。
- **配置漂移**：控制台改动 vs 仓库版本化，容易出现“环境里改了但代码里没有”。
- **敏感信息**：账号密码直写配置中心，需评估加密与权限。
- **配置缺失兜底**：配置中心不可达时是启动失败还是用本地默认值，需要有明确策略。

## 3. 项目中的实现链路

```text
应用启动
  -> bootstrap 阶段读取 bootstrap.yml
       spring.application.name = server-user
       spring.profiles.active   = dev
       spring.cloud.nacos.config.server-addr = 127.0.0.1:8848
       spring.cloud.nacos.config.group = user_group
       spring.cloud.nacos.config.file-extension = yaml
       shared-configs[0] = server-all.yaml (group=all_group, refresh=true)
  -> 从 Nacos 拉取 DataId = server-user-dev.yaml (user_group)
  -> 拉取共享配置 server-all.yaml (all_group)
  -> 合并进 Environment，创建数据源等 Bean
运行期
  -> Nacos 配置变更 -> 发布事件 -> @RefreshScope Bean 重建 / @Value 更新
  -> NacosController#nacosName 读取到新值
```

### 3.1 证据表

| 类型 | 位置 | 符号或配置键 | 能证明什么 |
|---|---|---|---|
| 【代码事实】 | `shop-user/src/main/resources/bootstrap.yml` | `config.server-addr`、`group: user_group`、`file-extension: yaml`、`profiles.active: dev` | 配置文件命名与分组规则 |
| 【代码事实】 | `shop-user/src/main/resources/bootstrap.yml` | `shared-configs[0].data_id: server-all.yaml`、`group: all_group`、`refresh: true` | 共享配置及动态刷新开关 |
| 【代码事实】 | `shop-product/src/main/resources/bootstrap.yml`、`shop-order/src/main/resources/bootstrap.yml` | `group: product_group` / `order_group`，同样的 shared-configs | 各服务独立分组 + 统一共享配置 |
| 【代码事实】 | `shop-gateway/src/main/resources/bootstrap.yml` | `group: gateway_group`、无 `shared-configs` | 网关接入方式与其他服务不一致 |
| 【代码事实】 | `shop-order/src/main/resources/bootstrap.yml:14-15` | `alibaba.seata.tx-service-group`、`profiles.active: dev` | 配置中心与 Seata 配置共存 |
| 【代码事实】 | `shop-user/.../controller/NacosController.java:17,24-29,35` | `@RefreshScope`、`@Value("${author.name}")`、`context.getEnvironment().getProperty("author.name")` | 两种读取方式与刷新演示 |
| 【代码事实】 | `docs/nacos/config/chapter23/user_group/server-user-dev.yaml` | `author.name: binghe_dev` | dev 环境配置 |
| 【代码事实】 | `docs/nacos/config/chapter23/user_group/server-user-test.yaml` | `author.name: binghe_test` | test 环境配置 |
| 【代码事实】 | `docs/nacos/config/chapter23/user_group/server-user.yaml` | `server.port: 8060`、`context-path: /user` | 不带 profile 的默认 DataId |
| 【代码事实】 | `docs/nacos/config/chapter23/all_group/server-all.yaml` | 数据源、mybatis-plus、http encoding | 公共配置集中管理 |
| 【代码事实】 | `docs/nacos/config/chapter23/order_group/server-order-dev.yaml` | sentinel、rocketmq、feign 配置 | 服务专属配置 |
| 【代码事实】 | `docs/nacos/config/*.zip` | Nacos 控制台导出包（chapter22/23/25） | 证明配置通过控制台管理，未做 Git 版本化 |

> 说明：`docs/nacos/config/*.zip` 是 Nacos 控制台导出的配置包，本文据此还原服务端配置；实际生效值以运行环境为准。

## 4. 关键选型与权衡

- 【代码事实】项目选择 **Nacos Config + `bootstrap.yml` + 服务名/profile 隔离 + 共享配置**，并给共享配置开启 `refresh: true`。
- **解决了什么**：多环境隔离（dev/test）、公共配置（数据源）集中、改配置无需改代码；运行期可刷新。
- **牺牲了什么 / 风险**：
  1. **环境隔离依赖 profile + group，未用 namespace**：`bootstrap.yml` 无 `namespace`，所有服务都在 Nacos 默认（public）命名空间，用 `user_group`/`product_group`/`order_group`/`gateway_group` 区分服务。好处是隔离清晰，代价是每加一个服务都要新建分组，跨环境共享更繁琐，且**环境隔离（dev/test/prod）没有物理边界**。
  2. **`server-all.yaml` 含数据源且 `refresh: true`**：数据源是否能在运行期真正重建连接池取决于 Spring Cloud 与连接池支持，Druid + MyBatis-Plus 组合下热刷新不一定安全，存在“配置刷新了但连接池未变”的风险。
  3. **配置未在仓库版本化**：只有导出的 zip，存在配置漂移；无法在 CI 中复现环境。
  4. **账号密码明文**写在配置里，无加密。
  5. **网关的 shared-configs 缺失**：其他服务都共享 `server-all.yaml`，网关没有；`server-gateway-dev.yaml` 里也没有数据源，说明网关不需要数据库，但配置风格不统一。
  6. **`@Value` 与 `@RefreshScope` 的隐式依赖**：不在 `@RefreshScope` Bean 里的 `@Value` 不会刷新，容易误判“配置没生效”。
- 【项目中未发现明确依据】未见 namespace、配置权限、加密、变更审批/回滚流程。

## 5. 关键工程实现

### 5.1 DataId 命名与优先级

Nacos 配置的 DataId 规则为：

```text
${spring.application.name}-${spring.profiles.active}.${spring.cloud.nacos.config.file-extension}
例: server-user-dev.yaml
```

- 同时存在 `server-user.yaml`（无 profile）与 `server-user-dev.yaml` 时，profile 专属配置优先级更高。
- `spring.profiles.active` 为空时只加载 `server-user.yaml`，此时 DataId 不包含 `-`。
- 切换环境只需改 `spring.profiles.active`（或启动参数 `--spring.profiles.active=test`），即可切到 `server-user-test.yaml`。

### 5.2 共享配置

```yaml
spring:
  cloud:
    nacos:
      config:
        shared-configs[0]:
          data_id: server-all.yaml
          group: all_group
          refresh: true
```

- `shared-configs` 用于多个服务共享的配置（数据源、公共 mybatis 配置）。
- **优先级低于应用自身配置**，因此应用可覆盖共享项。
- 还有 `extension-configs`（优先级介于 shared 与应用之间）。合理分层：`shared（最通用）→ extension（环境通用）→ 应用专属`。
- `refresh: true` 决定该配置变更是否触发应用刷新。

### 5.3 动态刷新的两种读法

```java
@RefreshScope          // 类级：Bean 在刷新时被重建，@Value 才会更新
@RestController
public class NacosController {
    @Value("${author.name}")
    private String nacosAuthorName;              // 配合 @RefreshScope 才能刷新

    @Autowired
    private ConfigurableApplicationContext context;

    public String nacosTest() {
        // 直接读 Environment，通常无需 @RefreshScope 就能拿到最新值
        return context.getEnvironment().getProperty("author.name");
    }
}
```

原理：配置变更 -> Nacos 客户端发布 `RefreshEvent` -> `ContextRefresher` 重建 `@RefreshScope` 代理 Bean -> 重新注入 `@Value`。因此：
- `@Value` 必须落在 `@RefreshScope` Bean 中；
- `@ConfigurationProperties` Bean 默认支持刷新（无需 `@RefreshScope`）；
- 数据源等已实例化的基础设施 Bean 不会自动重建。

### 5.4 配置来源与优先级（本项目实测结构）

| 来源 | 示例 | 优先级 | 备注 |
|---|---|---|---|
| `bootstrap.yml` | 注册/配置中心地址、服务名 | 早期引导 | 先于 application 加载 |
| Nacos 共享配置 | `server-all.yaml` | 低 | 用于公共配置 |
| Nacos 应用配置 | `server-user-dev.yaml` | 高 | 覆盖共享配置 |
| 本地 `application.yml` | 各服务同名文件（多为注释） | 与 Nacos 合并 | 本项目多数内容被注释 |

## 6. 踩坑、根因与排障

| 性质 | 现象 | 根因 | 项目处理 | 仍存缺口 | 验证或排查方法 |
|---|---|---|---|---|---|
| 潜在风险 | `@Value` 不刷新 | 缺少 `@RefreshScope` | NacosController 已加 | 其他类若用 `@Value` 需自查 | 改配置后调 `/nacos/name` |
| 潜在风险 | 数据源配置刷新但连接池未变 | 数据源 Bean 未绑定刷新语义 | 共享配置标了 `refresh: true` | 需验证是否真热更新 | 改连接串后观察是否新建连接 |
| 潜在风险 | 读到错误配置 | group/DataId 命名不一致 | 用 group 隔离 | 无启动自检 | 启动日志确认拉取的 DataId 与 group |
| 潜在风险 | 启动失败：拉不到配置 | 配置中心不可达/DataId 不存在 | 无本地兜底 | fail-fast 策略与告警 | 断开 Nacos 启动观察行为 |
| 潜在风险 | 配置漂移 | 控制台改动未回写仓库 | 无流程 | 配置版本化与审计 | 对比导出 zip 与运行值 |
| 潜在风险 | 敏感信息泄露 | 明文密码 | 未处理 | 加密/权限最小化 | 审计控制台权限 |
| 潜在风险 | 网关配置与其他服务风格不一致 | 缺 shared-configs | 未处理 | 统一接入模板 | 对比各 `bootstrap.yml` |
| 潜在风险 | 环境切换只改 profile，但 group 未同步 | group 是固定值 | 未处理 | 明确环境与 group 关系 | 切 test 观察是否读到正确配置 |

## 7. 可迁移的落地方案

【落地建议】

1. **统一命名规范**：`DataId = 应用名-profile.yaml`；环境隔离用 **Namespace**，服务隔离用 DataId/group，避免多种维度混用。
2. **Namespace 做环境隔离**：dev/test/prod 各一个 namespace，权限与配置天然隔离；profile 只区分应用维度差异。
3. **共享配置分层**：`公共基础（连接池/日志）` → `环境（地址/开关）` → `应用`，明确覆盖顺序。
4. **动态刷新要验证**：每个“希望热更新”的配置写一次变更测试；数据源/线程池类默认按“需重启”处理。
5. **配置版本化与审计**：用配置中心版本/回滚能力，或在 CI 中把配置导入作为一步。
6. **敏感配置加密**：Nacos 加密插件或外部密钥管理，至少限制控制台权限。
7. **本地兜底**：明确配置中心不可用时“启动失败”还是“用默认值”，并配告警。
8. **统一接入模板**：所有服务（含网关）使用同一份 `bootstrap.yml` 骨架，减少风格差异。

### 最小闭环

`Namespace 隔离环境 + 统一 DataId 规范 + 分层共享配置 + 刷新验证 + 版本化审计 + 敏感信息保护`。

## 8. 替代方案与适用边界

- **Nacos Config**：与 Spring Cloud Alibaba 一体化，注册与配置同一套（本项目）。
- **Apollo**：配置权限、灰度、审计能力更强，适合中大型组织。
- **Spring Cloud Config + Git**：配置天然版本化，适合 GitOps，但动态刷新能力较弱。
- **Kubernetes ConfigMap/Secret**：已在 K8s 时可用，变更通常触发滚动重启而非热更新。

判断依据：是否已有 Nacos、是否需要配置审计与灰度、是否 GitOps、是否要求热更新。

## 9. 验收清单

- [ ] 每个服务的 DataId 命名符合规范，启动日志可确认拉取来源。
- [ ] 环境隔离方案唯一（namespace 或 profile），不混用。
- [ ] 共享配置的覆盖顺序有文档与测试。
- [ ] 需要热更新的配置经过实际变更验证；不可热更新的配置有说明。
- [ ] 配置中心不可用时的行为符合预期并有告警。
- [ ] 敏感配置加密或权限受控。
- [ ] 配置变更可审计、可回滚。
- [ ] 所有服务（含网关）接入方式一致。

## 10. 待确认项

- 生产是否使用 namespace 隔离，仓库中 `bootstrap.yml` 未配置。
- 数据源在运行期是否真的热更新成功，未验证。
- 各环境 group 是否与仓库导出一致，需与运维核对。
- 配置的权限与加密策略未在仓库体现。
- 是否存在其他环境（prod）的配置导出，仓库目前只见 dev/test。
