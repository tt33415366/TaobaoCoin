# 淘宝淘金币自动兑换红包

每天自动调用淘金币网页版的 mtop 接口兑换红包。
档位策略由 `tier_strategy` 控制：`random` = 每天随机优先 **20元(6000金币)** 或 **10元(3000金币)**；`fixed` = 严格按 `tiers` 数组顺序（20元 永远优先）。失败自动落到下一档，**5元(1500金币)** 兜底。

**关键机制**：红包库存**每天上午 10:00 更新**（页面数据里的 `useArea` 字段），每档**每周限兑 1 次**，限量抢完即止。
所以脚本采用「准点狙击」打法：

1. **09:59:30 启动当天的尝试流程**，先用 `mtop.common.getTimestamp` 校准本地时钟与淘宝服务器的偏差
2. **睡到 09:59:59.4 开第一枪**——扣除一次「查询+兑换」约 0.6s 的耗时，让兑换请求正好在 **10:00:00 整点**抵达服务器
3. 失败后按递增间隔重试（1s→2s→4s→8s→15s 封顶），共 14 轮，覆盖到 10:01:30

白天跑只会得到「权益已变更」（=当天库存已空），属正常。

**每日收取与收菜日**：兑换之外，脚本每天还会做一次每日收取（签到+收币、签到状态同步）。当天模式由 `run_time` 的首页探测推导，**无跨天状态文件**：

- **狙击日**（当周仍有可兑档位）：流程同上，狙击与级联结束后立刻执行一次每日收取（时刻天然不规则，不加抖动）。
- **收菜日**（三档都已兑完——级联全报「已达周限」或首页显示全部已兑）：当天不再空转重试兑换，只在 `run_time` + **0–30 分钟随机抖动**后做一次每日收取；首页权益恢复（新一周）自动回到狙击日。抖动是因为 perfectly regular 的时间戳本身是风控特征；首页探测仍在 `run_time` 准点（只读），狙击时刻精度绝不受影响。
- 收取与兑换共用 cookie 会话、风控连坐，故收取的任何失败只记日志、**绝不反噬兑换**；唯独收取发现登录态失效会在当日摘要打出 **🔑** 标记——收菜日当天收取是 cookie 存活的唯一探针。决策依据见 [docs/adr/0001](docs/adr/0001-collection-scope-and-exchange-first.md)。

## 使用步骤

1. **填入 cookie**：浏览器登录淘宝后，按 F12 打开开发者工具 → Network → 刷新
   [淘金币页面](https://huodong.taobao.com/wow/z/tbhome/pc-growth/tao-coin)
   → 在请求列表里找发往 **`h5api.m.taobao.com`** 的请求（过滤框输入 `h5api`）
   → 复制请求头里整串 `Cookie`，粘贴到 `config.json` 的 `cookie` 字段。

   ⚠️ 必须是从 **mtop 接口请求**里复制，不要复制页面文档（`tao-coin` HTML）请求的
   cookie——后者只有设备指纹，不带登录态。合格的 cookie 应包含
   `cookie2`、`_m_h5_tk`、`_tb_token_` 等字段，脚本启动时会自动检查并提示缺失。

2. **测试连通性**（只查询不兑换）：

   ```bash
   python3 taobao_coin.py --list
   ```

   能打印出可兑换列表说明 cookie 有效。

3. **立刻兑换一次**：

   ```bash
   python3 taobao_coin.py --now
   ```

4. **手动执行一次每日收取**（调试入口）：

   ```bash
   python3 taobao_coin.py --collect
   ```

   先查金币小镇状态（余额 + 今日是否已签），未签才执行签到+收币与状态同步，
   打印摘要「今日收取 +X，余额 Y」（+X 取收取前后余额差）。已签到时记「今日已签到」
   并跳过——当日已签、余额照报是正常结局，不是失败。
   接口参数集中在 `gateway.py` 顶部常量（`SIGN_COLLECT_PARAMS` 的 spm 串、
   `REWARD_FIELDS` 金币字段名）；余额与已签判定来自 `town.index.get.pc`，已实测校准。

5. **每天自动跑**（部署目标是常开的 Linux 设备，如树莓派）：

   常驻模式 + 终端解耦（当前用法）：

   ```bash
   stty -ixon    # 禁用 Ctrl+S/Ctrl+Q 流控，防止 pty 输出被冻结
   nohup python3 taobao_coin.py >> stdout.log 2>&1 &
   ```

   日志：业务日志在 `taobao_coin.log`；stdout/stderr 副本和崩溃 traceback 在 `stdout.log`。
   每天运行时会重读 `config.json`，改配置不用重启。

   ⚠️ 不要直接前台跑或挂 screen/tmux 里跑：终端掉线、误按 Ctrl+S 都会让进程无声冻结。

   更省心的替代：cron 每天触发一次（`--once` 模式，跑完即退，无常驻进程；一次完整的
   「狙击等待+级联重试+每日收取」日常流程，含狙击日/收菜日判定）：

   ```cron
   59 9 * * * cd /home/pi/Workspace/TaobaoRedpocket && /usr/bin/python3 taobao_coin.py --once >> cron.log 2>&1
   ```

   注意 nohup/cron 都不负责重启后拉起；Pi 重启后需要重新启动，或换用 systemd service。

## 代码结构

| 文件 | module | 职责 |
|---|---|---|
| `taobao_coin.py` | 薄入口 | CLI、常驻循环、重试编排、狙击日/收菜日状态机 |
| `gateway.py` | 淘金币 Gateway | mtop 签名 / token 刷新 / 拆包 / 错误分类，对外是 `fetch_benefits()`、`exchange(code)` 与收取动词（签到+收币 / 状态同步 / 小镇状态） |
| `collect.py` | 每日收取 | 签到编排（已签跳过、失败隔离、余额差计收益）与摘要「今日收取 +X，余额 Y」 |
| `cascade.py` | 档位级联 | 按优先级逐档尝试，`exchange_fn` 是可注入的 seam |
| `snipe_clock.py` | 狙击时钟 | 时钟校准 + 准点等待 |
| `config.py` | Config | 加载、默认值、cookie 校验 |
| `CONTEXT.md` | — | 领域术语表 |

领域术语（Gateway / 级联 / 狙击时刻）见 [CONTEXT.md](CONTEXT.md)。

## config.json 说明

| 字段 | 默认 | 含义 |
|---|---|---|
| `cookie` | — | 淘宝登录 cookie（见上面第 1 步） |
| `run_time` | `09:59:30` | 每天启动时间（支持秒），要早于 snipe_time |
| `snipe_time` | `10:00:00` | 准点狙击时刻：校准时钟后让第一发兑换在整点抵达服务器 |
| `exchange_retries` | `14` | 当天重试轮数（默认覆盖到 10:01:30） |
| `exchange_retry_base` | `1` | 首次重试间隔秒数，之后指数翻倍（1→2→4→8…） |
| `exchange_retry_max` | `15` | 重试间隔上限秒数 |
| `tier_strategy` | `random` | 档位优先级策略：`random` = 前两档每日随机；`fixed` = 严格按 `tiers` 数组顺序（如 20元 永远优先） |
| `tiers` | 20/10/5 元三档 | 档位关键词，`keywords` 命中权益的面额/金币字段即匹配 |

本轮没有引入新配置项：收取的抖动上限（`COLLECTION_JITTER_MINUTES`）在 `taobao_coin.py`，
收取接口的 spm/金币字段名/载荷常量在 `gateway.py` 顶部，需要调整时直接改常量。

## 注意事项

- 每档**每周限兑 1 次**：本周兑过某档后接口会拒绝，脚本会自动落到其他档位；三档都兑过则当天记为收菜日，不再空转重试，只做一次每日收取。
- 每日收取的任何失败（已签到 / 无可领返币 / 触发风控）只记日志，属正常业务结局，绝不影响兑换结果与退出码。
- 收取路径发现登录态失效时，当日摘要会打出 **🔑** 错误标记——收菜日当天没有兑换请求，这是 cookie 存活的唯一探针，看到就去更新 `config.json` 里的 cookie。
- cookie 有效期通常几天到几周，过期后日志会提示「登录态失效」，重新粘贴即可。
- 兑换涉及资金，淘宝风控偶尔会要求滑块验证，脚本无法过滑块——当天会失败并写日志，第二天自动重试。
- 金币余额不足时兑换接口会返回失败，属于正常情况（先攒金币）。
- 单元测试：`python3 -m unittest discover -p "test_*.py"`（97 个）
