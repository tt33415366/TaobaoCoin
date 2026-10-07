# 当日模式 module 的诞生与签到序列收口

架构复审（2026-10-07）发现两处 seam 泄漏，本轮一并修复。术语见 [CONTEXT.md](../../CONTEXT.md)；与 ADR 0001 的关系见各自的修订指针。

- **Status**: accepted
- **日期**: 2026-10-07

## 决策 1：当日模式（DayMode）成为 module

**背景**：狙击日/收菜日/纯收取日是系统中心概念，但没有任何 module 拥有它——三个模式只以日志行和 docstring 存在，判定逻辑横跨 config / taobao_coin / gateway / cascade 四个文件；「已达周限」信号经 关键词 → 异常子类 → 计数器 → `all_week_limited` → `DailyRunResult` → truthiness 退出码 五级转手；规格实际住在 test_main.py 的 4-fake 端到端里（全 repo churn 最高的文件）。

**决策**：新增 `day_mode.py`，拥有完整日常流程：读探测 → 推导模式 → 分派执行 → 按「兑换永远优先」收尾收取。

- interface 两个动词：`run_day(cfg, ...)`（features 开关的唯一决策点）与 `run_exchange(cfg, ...)`（狙击+级联，供 `--now` 与 run_day 的狙击日分支复用；不收取、不读 features——「手动路径绕过 features」从 docstring 散文变成 interface 形状）
- `DayResult(mode, exchange, collection)`：`mode` 是具名枚举（三个 CONTEXT.md 词条第一次成为代码里的值）；`DailyRunResult` 删除，IntEnum-as-bool 的 truthiness 暗桩随之拆除
- 退出码推导收进 main() 的 `exit_code()` 一处：仅狙击日兑换失败退出码 1——与旧契约逐字节一致，但从巧合变成具名推导
- 收取摘要渲染（含 🔑 登录态标记）收进 `day_mode.log_collection_summary`，常驻与 `--collect` 共用

与 ADR 0001 决策 4（无跨天状态文件）不冲突：模式仍由当天首页数据现推，本决策只给「推导」一个主。

## 决策 2：页面挂载序列完整收口进 `gateway.sign_in()`

**背景**：ADR 0001 修订 3 只收了一半——town 预热与收尾同步仍在 collect，gateway 的 docstring 反向引用调用方（「town 一步由调用方先查」），编排知识漏过 seam 写进了 interface 契约。

**决策**：`sign_in() -> SignOutcome(skipped, reward, balance_before)` 独占完整页面挂载序列（town 起始 → 已签短路 → home 热身 → 签到 → 同步）；`collect_sign_reward` 与 `sync_sign_status` 从 interface 删除。已签短路时连热身也不发——「无意义请求不发」从编排纪律变成结构保证。

**边界（TDD 过程中浮现）**：收尾 town 查询**不进** sign_in。它不是页面挂载序列的一部分（序列四步由 ADR 0001 修订 3 钉死），而是每日收取的 +X 余额差测量业务（修订 1）。若收进 sign_in，收尾查询的 SessionExpired 会让已获得的 reward 死在异常里（test_collect 钉着 `coins_gained=5 且 session_expired=True` 的结局），SignOutcome 就得染上「异常+标记」双表示。收尾查询留 collect，SignOutcome 保持三字段。

**边界 2（评审过程中浮现）**：同一个「reward 死在异常里」的陷阱也适用于序列内部的同步步——sign_in 的初版对同步的 SessionExpired 选择重抛，会让已获得的 reward 一并丢失（HEAD 语义是保留）。定为：热身与同步两步的**任何**失败都 best-effort，含 SessionExpired——签到步数秒前刚成功即证明登录态活着，此时报失效是假阳性；cookie 探针职责由 collect 的收尾 town 查询承担。起始 town 的 SessionExpired 仍然抛出（登录态已死则签到必然同样失败），与 HEAD 一致。

**后果**：collect 瘦身为 DailyCollection 的业务语义（失败隔离、+X 算术、摘要行），fake gateway 从三动词缩到两动词；顺序守卫全部收进 test_gateway 的 transport 级断言（未签四步次序、已签只发 town），test_collect 的跨 module 顺序断言删除——顺序不再跨 module，没有缝可守。签到若再次静默失效，二分点唯一：`gateway.sign_in`（呼应 ADR 0001 修订 2 的未决变量）。

## 决策 3：双 fetch 保留并正名——探测快照不复用于兑换

**背景**：架构复审报告曾把「模式探测与首轮级联各发一次 fetch_benefits」判为冗余，建议快照复用省一次请求。核实配置后否决：`run_time`（09:59:30）比 `snipe_time`（10:00:00）**早 30 秒**，中间隔着整点库存刷新；「benefitCode 跨刷新不变」是未验证假设——本代码库的教训（ADR 0001 三次修订全部源于「实测证伪假设」）是这种赌不该打。

**决策**：两次 fetch 保留，在 day_mode 内正名——「模式探测」（run_time 准点、只读、判狙击/收菜）与「尝试快照」（级联每轮刷新，含首轮）。docstring 钉死设计意图。

**记录此决策的理由**：消除冗余的直觉会让每个复审者都再提一次快照复用；ADR 的价值是记录「为什么明显更聪明的方案被否决了」。

## 测试迁移

- test_main.py → test_day_mode.py：模式规格改打 `run_day` / `run_exchange`；FakeCfg 删除（Config 是普通 kwargs 类，直接构造真对象，手工复刻的漂移源消失）；test_main 只留退出码映射一张皮
- test_gateway.py：`TestCollectVerbs` 由 `TestSignIn` 取代（transport 级全序列守卫）；小镇查询测试保留为 `TestQueryCoinTown`
- test_cascade / test_config / test_snipe_clock 不动
