# BTC 倒金字塔自动交易策略 — 实施文档 (v16 final / 长倒金字塔 + 牛市HODL + 减半逃顶 + 熊市做空)

> **目的**: 把人工判断从执行环节剔除。所有买入/卖出决定来自机械规则。
> 策略已通过两个完整 BTC 周期（2018-2021 + 2022-2025）的回测验证。
>
> **演进说明**: 本文档从 v11h-a（牛市多次 trim）演进到 v16。关键修正见
> 第 0.5 节"版本演进与已修复的错误"。**早期版本的高收益数字含 bug，已作废。**
>
> **诚实预期表现**（v16，从 cycle 底部入场，bug 修复后）:
> - 纯多头倒金字塔（无 short）: 几何均值 **11.6x**
> - + 谨慎 short (30% 现金/1.5x): 几何均值 **~13.5x**
> - + 中等 short (50% 现金/2x): 几何均值 **~16x**
> - 对照: 2x HODL 完美底 = 19x（需完美择时）, 3x HODL = 28x（需完美择时 + 扛 -83% 回撤）
> - **倒金字塔的核心价值 = ±3 个月择时容错**，代价是放弃约 20% 上行
> - 最大回撤约 **-76%**（PW 周期，COVID 闪崩造成）, **-40%**（W 周期）
> - 加上执行摩擦（滑点/资金费 2-5x）后，**真实预期 ~10-14x**

---

## 0. 核心原则

| 原则 | 说明 |
|---|---|
| **不预测，只反应** | 不试图判断"顶在哪里"或"底在哪里"，只对价格触及结构性支撑/阻力做反应 |
| **机械执行** | 所有决策由参数化规则触发；人工只能 (a) 修改参数 (b) 紧急停机 |
| **杠杆从利润中长出** | 起始 **1.5x**（保护期，扛得过 COVID-style 闪崩），账户翻 50% 后升 3x（增厚期），逃顶后回 0x |
| **长用倒金字塔，空用前置** | **多头**先小后大（低价吃便宜筹码）；**空头**先大后小/平均（高价早做空）— 两者结构镜像相反 |
| **时间锚定退出** | 周期顶用 BTC 减半日期 + 17-18 月作锚，比任何技术指标都可靠 |
| **限价单优先** | 所有触发用挂单，永不用市价单（避免滑点和情绪化追单） |

---

## 0.5 版本演进与已修复的错误

> 这一节记录关键教训，避免未来重蹈覆辙。

| 版本 | 改动 | 结论 |
|---|---|---|
| v1-v8 | 各种 trim/杠杆/止损组合 | 多次 trim 在牛市是负 alpha；硬止损在波动牛市被反复洗 |
| v9 | 分阶段杠杆无止损 | 1.5→3x phased 是跨周期生存的关键（2x 起步在 COVID 爆仓）|
| v10 | 机械买底卖顶 | 失败：RSI/Mayer 信号假触发几十次，不能定位真顶/底 |
| v11h | **halving 锚定逃顶** | 减半 +17-18 月 = 历史 3 次顶都准，是最可靠的退出信号 |
| v13 | 加熊市做空 | **发现 bug 1**: short 平仓误把名义价值计入现金（应只计 PnL）|
| v14 | 倒金字塔 + 牛市 HODL | 牛市不 trim 收益更高，但**至少需要 1 次 trim** 才能扛过闪崩 |
| v15 | alpha 搜索 | 警告：trim 月份有"悬崖"（+11 月爆仓 / +12 月 20x）= 过拟合，勿追 |
| **v16** | 两层对比 + 核对 | **发现 bug 2**: force exit 必须在 short 现金分配**之前**（顺序错导致 short 只分到零头）|

**两个已修复的关键 bug**：
1. **Short 平仓会计**: `S_cash += pnl`（只加盈亏），不是 `S_cash += 名义 + pnl`。修复前短仓收益被夸大 ~20 倍。
2. **操作顺序**: 减半+18月那天，先 force exit 平多变现金，**再**分配现金给熊市做空。顺序反了会导致做空只拿到很少资金。

**被否决的过拟合诱惑**（两个周期数字不相称 = 危险信号）：
- trim @+12 月（单点 20x，+11月就爆仓）→ 拒绝
- lifetime_w=52 周（PW 暴涨但靠少数老支撑）→ 拒绝
- 5x 杠杆 / 全部峰值参数组合（4257x 几何均值）→ 拒绝
- **只接受邻近参数平滑、两周期相称的改进**

---

## 1. 策略整体架构

### 1.1 五阶段状态机

```
        ┌─────────────────────────────────────────┐
        │  PHASE 1: 建仓期 (BUILD)                 │
        │  起始杠杆 1.5x (扛闪崩)                   │
        │  触发: 价格触及 swing 支撑 ±3%           │
        │  动作: 多头倒金字塔 5 层 (先小后大 1.5x) │
        └──────────────┬──────────────────────────┘
                       │  account_equity >= INITIAL × 1.5
                       ▼
        ┌─────────────────────────────────────────┐
        │  PHASE 2: 增厚期 (BOOST)                 │
        │  杠杆升级到 3x (利润已有缓冲)            │
        │  触发: 同 PHASE 1 (继续 S/R 吃支撑)      │
        │  关键: 牛市中段【不】做 trim — 全程 HODL │
        └──────────────┬──────────────────────────┘
                       │  到达 halving + 14 月
                       ▼
        ┌─────────────────────────────────────────┐
        │  PHASE 3: 逃顶期 (减半时间锚定)          │
        │  halving + 14 月: 触及阻力 trim 20% (1次)│
        │                   (唯一保命 trim, 增加现金)│
        │  halving + 18 月: ★ FORCE EXIT 全平多头  │
        │                   (先平多 → 现金到手)    │
        └──────────────┬──────────────────────────┘
                       │  多头全平, 现金到手
                       ▼
        ┌─────────────────────────────────────────┐
        │  PHASE 4: 熊市做空期 (halving+18 → +30)  │
        │  分配 30-50% 现金做空 (剩余 USDC staking) │
        │  空头【前置/平均】3 层 (先大后小, flat)  │
        │  Entry: 触及 swing 阻力; Cover: 触及支撑 │
        │  止损: 距空头均价 +30% (V反弹保护)       │
        └──────────────┬──────────────────────────┘
                       │  halving + 30 月 (下个底临近)
                       ▼
        ┌─────────────────────────────────────────┐
        │  PHASE 5: 平空 + 重启                    │
        │  平所有空单, 现金回归                    │
        │  回到 PHASE 1 (新一轮, 用更大本金)       │
        └─────────────────────────────────────────┘
```

### 1.2 完整时间表（2026-2029 计划，锚定 2028-04 减半）

| 阶段 | 起止日期（估计）| 操作 | 杠杆 |
|---|---|---|---|
| PHASE 1 | 2026-09 ~ 2027-中 | 多头倒金字塔吃底 (S/R 触发) | **1.5x** |
| PHASE 2 | 2027-中 ~ 2029-04 | 浮盈翻倍升 3x，继续吃支撑，**不 trim** | **3x** |
| PHASE 3a | 2029-06 (halving+14月) | 触及阻力 trim 20%（唯一保命 trim）| 3x ↓ |
| PHASE 3b | 2029-10 (halving+18月) | **★ FORCE EXIT 全平多头** | → 0x |
| PHASE 4 | 2029-10 ~ 2030-10 | 30-50% 现金做空熊市（前置 3 层）| 空 1.5-2x |
| PHASE 5 | 2030-10 之后 | 平空，重启 PHASE 1 | — |

> **注**: 2028-04-15 是下次减半。历史 3 次顶都在减半 +17.3~18.0 月，故 force exit 设
> 在 +18 月。2026-09 见底假设对应减半前约 19 个月，与历史"底在减半前 17-19 月"一致。

---

## 2. 参数配置文件

```yaml
# strategy_config.yaml  (v16 final)
account:
  initial_usd: 7000.0           # 起始本金 USD
  exchange: "OKX"
  symbol: "BTC-USDT-SWAP"        # USDT 保证金永续

leverage:                       # 多头分阶段杠杆 (从利润中长出)
  phase1: 1.5                    # 建仓期杠杆 (扛得过 COVID-style 闪崩; 2x 会爆)
  phase2: 3.0                    # 增厚期杠杆
  upgrade_trigger: 1.5           # equity / initial >= 1.5 时 phase1 → phase2
  # 注: 2x 起步在 PW(2018-19) 的 -55% 下跌中爆仓; 1.5x 是生存下限

entry_pyramid_long:             # 多头倒金字塔 (先小后大)
  num_layers: 5
  layer_growth: 1.5              # 每层比上层大 50% (geometric)
  # L1=7.6%  L2=11.4%  L3=17.0%  L4=25.6%  L5=38.4%  (大仓位落在低价 = 便宜)

signal_sr:                      # support / resistance detection
  source: "weekly_swings"        # 周线 swing 高低点
  swing_window: 2                # ±2 周确认 (第 i 根周线 = 前后 4 周内最低/高)
  tolerance: 0.03                # 价格在 S/R ±3% 范围内触发
  cooldown_days: 3               # 同一 S/R 级别 3 天内不重复触发 (auto, 无操作成本)
  level_lifetime_weeks: 26       # S/R 级别有效期 26 周, 过期失效

trim_logic:                     # 牛市【不】做多次 trim — 只保留 1 次保命 trim
  trim_during_bull: false        # ★ 关键: 牛市中段不 trim (回测: 不trim收益高得多)
  pre_exit_trim_month: 14        # 唯一的 trim: halving + 14 月
  pre_exit_trim_pct: 20          # 触及阻力时卖 20% (提供闪崩 cushion + 锁部分利润)
  force_exit_months: 18          # ★ halving + 18 月全平多头 (历史顶在 +17.3~18.0月)
  blackout_end_months: 24        # 多头不入场窗口 (halving +17 ~ +24)
  # 警告: 完全不 trim (pre_exit_trim_pct=0) 在 PW 会爆仓; 至少留这 1 次

short_logic:                    # 熊市做空 — 前置/平均, 不是倒金字塔!
  enable: true
  bear_window: [18, 30]          # halving +18 ~ +30 月 (force exit 后到下个底)
  cash_pct: 0.30                 # 投入 30% 现金做空 (保守; 50% 中等; 70% 激进但危险)
  leverage: 1.5                  # 空头杠杆 (1.5 保守 / 2.0 中等)
  num_layers: 3                  # ★ 空头 3 层 (少于多头; 越少越好)
  layer_growth: 1.0              # ★ 平均分配 (空头【不】用倒金字塔!)
  #   原因: 熊市阻力依次下降, "后层更大" 会把大空单放在低价 = 差的做空点
  stop_pct: 0.30                 # 距空头均价 +30% 强平 (V反弹保护)
  # ★ 顺序: force exit (平多) 必须在分配空头现金之前!

halving:
  next_halving_date: "2028-04-15"   # 下次减半 (估计)
  # 历史: 2012-11-28, 2016-07-09, 2020-05-11, 2024-04-20

risk:
  max_long_leverage: 3.0         # 多头任何时刻不超 3x
  hard_stop_long: null           # 多头不用硬止损 (回测显示是负 alpha, 被反复洗)
  liquidation_buffer: 0.05       # 距离爆仓 5% 内强制减仓警报
  daily_loss_limit_pct: 30       # 单日亏损 30% 暂停所有买入 24h

execution:
  order_type: "LIMIT"            # 全部用限价单
  slippage_tolerance_pct: 0.5    # 价格触发后, 0.5% 内挂单视为有效
  max_active_orders: 3
  funding_rate_assumed: 0.0001   # 资金费假设 0.01%/8h
  # 压力测试: 资金费 2x → 收益 -17%; 5x → -59%; 10x → -86%. 真实预期 2-5x baseline
```

### 2.1 为什么多头用倒金字塔、空头用前置加权？

| | 多头 (牛市底) | 空头 (熊市顶后) |
|---|---|---|
| 结构 | 倒金字塔 (先小后大, growth 1.5) | 前置/平均 (3 层, growth 1.0) |
| 逻辑 | 价格越跌加越大 → 大仓位在低价 = **便宜筹码** | 高价早做空 → 别等跌下去再加大 |
| 实测 geo | 5 层 1.5x 最优 | 3 层 flat 最优 (1.6% 优于 4 层 1.3x) |
| 为何镜像 | 底部你想吃尽量低的价 | 熊市阻力依次下降, 后层落在低价 = 差的空点 |

这也印证 Paul Wei 实战: 他在 2024-03 顶部附近就大量做空, 不是等跌下来才加。

---

## 3. 信号检测算法

### 3.1 周线 Swing High/Low 检测

```python
def detect_swings(daily_ohlc_df, swing_window=2):
    """
    检测周线 swing 高低点（用于 S/R 级别）。
    
    返回:
      swing_highs: DataFrame[confirm_date, price] — 阻力位
      swing_lows:  DataFrame[confirm_date, price] — 支撑位
    
    confirm_date = swing 发生后 swing_window 周
    （这是该 swing 真正可以使用的最早日期，避免 lookahead bias）
    """
    # 1. 把日线数据 resample 成周线 (Mon-Mon)
    weekly = daily_ohlc_df.resample("W-MON").agg(
        Open=("Open","first"), High=("High","max"),
        Low=("Low","min"), Close=("Close","last")
    ).dropna()
    
    swing_highs = []
    swing_lows = []
    
    for i in range(swing_window, len(weekly) - swing_window):
        bar = weekly.iloc[i]
        window = weekly.iloc[i-swing_window : i+swing_window+1]
        
        # 这一周的 High 是 ±N 周窗口内的最高
        if bar.High == window.High.max():
            confirm_date = weekly.index[i + swing_window]
            swing_highs.append((confirm_date, bar.High))
        
        # 这一周的 Low 是 ±N 周窗口内的最低
        if bar.Low == window.Low.min():
            confirm_date = weekly.index[i + swing_window]
            swing_lows.append((confirm_date, bar.Low))
    
    return pd.DataFrame(swing_highs, columns=["confirm","price"]), \
           pd.DataFrame(swing_lows, columns=["confirm","price"])
```

### 3.2 当前 active 支撑/阻力

```python
def get_active_levels(swings_df, current_date, lifetime_weeks=26):
    """获取 current_date 时刻仍然 active 的 S/R 级别。"""
    lifetime_days = lifetime_weeks * 7
    cutoff_old = current_date - pd.Timedelta(days=lifetime_days)
    
    mask = (swings_df.confirm <= current_date) & \
           (swings_df.confirm > cutoff_old)
    return swings_df.loc[mask, "price"].values
```

---

## 4. 决策主循环 (每日 06:00 UTC 执行)

```python
def daily_decision_loop():
    """
    每天执行一次, BTC 收盘后。
    """
    # 1. 拉取最新数据
    daily_ohlc = fetch_okx_daily_ohlc("BTC-USDT-SWAP", lookback_days=500)
    today = daily_ohlc.index[-1]
    today_close = daily_ohlc.Close.iloc[-1]
    
    # 2. 更新 S/R 级别
    swing_highs, swing_lows = detect_swings(daily_ohlc, swing_window=2)
    active_supports = get_active_levels(swing_lows, today, lifetime_weeks=26)
    active_resists  = get_active_levels(swing_highs, today, lifetime_weeks=26)
    
    # 3. 拉取当前账户状态
    account = okx_get_account()
    equity = account.total_usd
    position = okx_get_position("BTC-USDT-SWAP")
    avg_cost = position.avg_entry_price
    
    # 4. 计算当前阶段 (用入场后的下一个 halving 作锚)
    mh = months_since_last_halving(today)        # 距最近 halving 的月数
    in_bear_window = (18 <= mh < 30)             # PHASE 4 熊市做空窗口

    # 5. RISK CHECKS（先做）
    if check_liquidation_risk(long_position, short_position, today_close):
        send_alert("⚠️ 距离爆仓 < 5%, 紧急减仓")
        emergency_reduce(); return
    if daily_loss_pct() > 30:
        send_alert("⚠️ 单日亏损 > 30%, 暂停 24h"); return

    # ============ 多头 (LONG BOOK) ============
    # 6. ★ FORCE EXIT (halving + 18 月) — 必须在分配空头现金之前!
    if mh >= 18 and not force_exit_done_this_halving() and long_position.size > 0:
        execute_force_exit_long(reason="halving+18mo")   # 平多 → 现金到手
        mark_force_exit_done(current_halving())
        log_event("FORCE_EXIT_LONG", price=today_close)
        # 不 return — 同一天可以继续走 PHASE 4 分配空头

    # 7. ★ 牛市【不】做 trim. 只在 halving+14 月做唯一一次 20% 保命 trim
    if (long_position.size > 0 and today_close > long_avg_cost
        and mh >= 14 and not pre_exit_trim_done_this_halving()):
        for lvl in active_resists:
            if abs(today_close - lvl)/lvl <= 0.03:
                place_limit_sell(price=lvl, size=long_position.size * 0.20)
                mark_pre_exit_trim_done(current_halving())
                log_event("PRE_EXIT_TRIM", level=lvl, pct=20)
                return

    # 8. LONG BUY (倒金字塔, 先小后大). 临近顶部不买
    in_long_blackout = (17 <= mh < 30)
    if not in_long_blackout and long_position.size_ok_to_add():
        current_max_lev = 3.0 if (total_equity / INITIAL_USD) >= 1.5 else 1.5
        layer = count_long_layers_this_cycle()
        if layer < 5:
            for lvl in active_supports:
                if abs(today_close - lvl)/lvl <= 0.03 and not fired_recently(lvl,"sup",3):
                    geo_sum = sum(1.5**k for k in range(5))   # = 13.19
                    if layer == 0:
                        l1 = total_equity * current_max_lev / geo_sum; save_l1(l1); size = l1
                    else:
                        size = get_l1() * (1.5 ** layer)        # 先小后大
                    size = min(size, max(0, current_max_lev*total_equity - long_notional()))
                    if size > 50:
                        place_limit_buy(price=lvl, size_usd=size)
                        mark_fired(lvl,"sup",today); log_event("LONG_BUY", layer=layer+1, size=size)
                    return

    # ============ 空头 (SHORT BOOK), 仅 PHASE 4 ============
    # 9. 进入熊市窗口时分配现金 (force exit 之后, 所以现金充足)
    if in_bear_window and not short_book_active():
        allocate_to_short(pct=0.30)               # 30% 现金; 其余 USDC staking
        # 注意: 此时 force exit (步骤 6) 已把多头变现金, 分配的是真实权益

    # 10. SHORT ENTRY (前置/平均, 3 层 flat — 不是倒金字塔!)
    if short_book_active() and short_layer() < 3:
        for lvl in active_resists:
            if abs(today_close - lvl)/lvl <= 0.03 and today_close <= lvl*1.03 \
               and not fired_recently(lvl,"res_short",3):
                geo_sum = sum(1.0**k for k in range(3))   # = 3 (flat)
                if short_layer() == 0:
                    s1 = short_cash * 1.5 / geo_sum; save_s1(s1); size = s1   # 空头杠杆 1.5x
                else:
                    size = get_s1() * (1.0 ** short_layer())   # flat: 各层相等
                size = min(size, max(0, 1.5*short_cash - short_notional()))
                if size > 50:
                    place_limit_short(price=lvl, size_usd=size)
                    mark_fired(lvl,"res_short",today); log_event("SHORT_ENTRY", size=size)
                return

    # 11. SHORT COVER (触及支撑, 每次平 1/3) + 硬止损 +30%
    if short_book_active() and short_position.size > 0:
        if (today_high - short_avg_cost)/short_avg_cost >= 0.30:   # V反弹止损
            cover_all_short(reason="stop +30%"); return
        if today_close < short_avg_cost:
            for lvl in active_supports:
                if abs(today_close - lvl)/lvl <= 0.03 and not fired_recently(lvl,"sup_short",3):
                    place_limit_cover(price=lvl, size=short_position.size * 0.33)
                    mark_fired(lvl,"sup_short",today); log_event("SHORT_COVER", pct=33)
                    return

    # 12. PHASE 5: 熊市窗口结束 (halving+30月), 平所有空, 现金回归
    if mh >= 30 and short_book_active():
        cover_all_short(reason="cycle reset"); return_short_cash_to_main()
```

---

## 5. OKX 实现细节

### 5.1 账户设置

```
1. 登录 OKX → 衍生品交易 → BTC-USDT 永续
2. 持仓模式: 单向持仓 (long-short mode)
3. 保证金模式: 全仓 (cross-margin) — 这样所有保证金共享
   备选: 逐仓 (isolated) — 每个仓位独立，但策略每次只有一个仓位，差异不大
4. 杠杆: 先设 2x（PHASE 1）
5. API key 权限: 
   - 交易权限 ✓
   - 提币权限 ✗（安全考虑）
   - IP 白名单: 服务器 IP
```

### 5.2 API endpoints 使用

| 操作 | OKX REST endpoint |
|---|---|
| 拉取日线 K 线 | `GET /api/v5/market/history-candles?instId=BTC-USDT-SWAP&bar=1D&limit=300` |
| 拉取账户余额 | `GET /api/v5/account/balance` |
| 拉取当前持仓 | `GET /api/v5/account/positions?instId=BTC-USDT-SWAP` |
| 修改杠杆 | `POST /api/v5/account/set-leverage` |
| 下限价单 | `POST /api/v5/trade/order` (ordType=limit) |
| 取消订单 | `POST /api/v5/trade/cancel-order` |
| 查活动订单 | `GET /api/v5/trade/orders-pending` |

### 5.3 杠杆切换逻辑

```python
def upgrade_leverage_if_needed():
    """检查是否应该把 OKX 杠杆从 2x 升到 3x。"""
    equity = okx_get_account().total_usd
    if equity / INITIAL_USD >= 1.5:
        current_lev = okx_get_leverage("BTC-USDT-SWAP")
        if current_lev < 3.0:
            okx_set_leverage("BTC-USDT-SWAP", 3.0)
            log_event("LEVERAGE_UPGRADE", from=current_lev, to=3.0)
            send_notification("🚀 杠杆升级到 3x")
```

注意: OKX 修改杠杆要求 **当前没有挂单**。修改前先取消所有未成交订单。

---

## 6. 数据流和持久化

### 6.1 必须持久化的状态

```sql
-- state.db (SQLite or Postgres)

-- 1. S/R 级别表（已触发过的）
CREATE TABLE fired_levels (
    level_price REAL,
    level_type TEXT,         -- 'support' or 'resistance'
    fired_date DATE,
    PRIMARY KEY (level_price, level_type, fired_date)
);

-- 2. 层级填充状态（本 cycle 已填几层）
CREATE TABLE pyramid_state (
    cycle_id TEXT PRIMARY KEY,    -- 例: 'cycle_2026_2029'
    layer_count INTEGER,
    l1_size_usd REAL,
    started_at DATE
);

-- 3. 决策日志
CREATE TABLE decision_log (
    timestamp TIMESTAMP,
    event_type TEXT,         -- 'BUY' | 'TRIM' | 'FORCE_EXIT' | 'LEVERAGE_UPGRADE' | 'ALERT'
    payload JSON,
    btc_price REAL,
    account_equity REAL,
    position_size REAL,
    avg_cost REAL
);

-- 4. 订单跟踪
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    intent TEXT,             -- 'L1', 'L2', ..., 'L6', 'TRIM_T1', 'TRIM_T2', 'FORCE_EXIT'
    side TEXT,
    price REAL,
    size REAL,
    status TEXT,             -- 'pending' | 'filled' | 'cancelled'
    created_at TIMESTAMP,
    filled_at TIMESTAMP
);
```

### 6.2 历史数据缓存

```
data/
├── btc_daily_ohlc.parquet      # 历史日线 (从 2018 起)
├── btc_weekly_ohlc.parquet     # 历史周线 (自动从日线 resample)
├── swing_levels.parquet        # 检测出的 S/R 级别
└── halving_dates.json          # 减半日期表
```

每日更新流程:
```python
# 每天 00:30 UTC 跑
def daily_data_refresh():
    last_close_yesterday = okx_get_close_for(yesterday)
    append_to_parquet("btc_daily_ohlc.parquet", new_row)
    
    # 重算周线（最近 12 周即可）
    weekly = resample_to_weekly(daily_df.tail(180))
    update_weekly_parquet(weekly)
    
    # 重新检测 swings（重算后 30 天即可，因为更早的已经稳定）
    swings_h, swings_l = detect_swings(weekly)
    upsert_levels(swings_h, swings_l)
```

---

## 7. 监控和告警

### 7.1 必须的告警

| 事件 | 触发条件 | 通知方式 |
|---|---|---|
| 🟢 BUY 成交 | 限价买单 filled | Telegram / email |
| 🟢 TRIM 成交 | 限价卖单 filled | Telegram / email |
| 🚀 杠杆升级 | equity / initial 首次 ≥ 1.5 | Telegram |
| ⚠️ 距爆仓 < 5% | 实时价格越线 | Telegram + SMS |
| ⚠️ 单日亏损 > 30% | 实时 | Telegram + SMS |
| ⚠️ 资金费率异常 | > 0.05%/8h 持续 24h | Telegram |
| 🛑 FORCE EXIT 触发 | halving+18mo (多头全平) | Telegram |
| 🔻 熊市做空激活 | halving+18mo (分配 30% 现金) | Telegram |
| 🔺 熊市平空重启 | halving+30mo | Telegram |
| ❌ API 错误 | 任何 4xx/5xx | Telegram |
| ❌ 订单卡住 | 限价单 > 7 天未成交 | Telegram |

### 7.2 每周报告（自动生成）

```
=== BTC Strategy Weekly Report (周日 22:00 UTC) ===

账户:    $XX,XXX (起始 $7,000, +XXX%)
杠杆:    XXX (当前)
仓位:    X.XXX BTC @ avg $XX,XXX
浮盈:    $XX,XXX (XXX%)

本周:
- 买入: X 笔, 总额 $XXX
- 卖出: X 笔, 总额 $XXX
- 资金费: -$XXX

下周关注:
- 最近上方阻力 (尚未触发): $XX,XXX
- 最近下方支撑 (尚未触发): $XX,XXX
- Halving + N 月: 当前 N 月 (距 force exit 还有 Y 月)
```

---

## 8. 失败模式和恢复

### 8.1 已知失败模式

| 场景 | 检测 | 自动响应 |
|---|---|---|
| OKX API 短暂宕机 | 连续 3 次 timeout | 退避重试 (5s, 30s, 5min) |
| OKX API 长期宕机 (>1h) | timeout 持续 | Telegram 告警, 暂停决策 |
| 网络分区导致状态不一致 | 本地 vs 远程持仓不同 | 信任远程，更新本地 |
| 限价单错过价格（价格快速穿过）| 7 天未 fill | 取消并重新挂在当前价 |
| BTC 一日 -50% 闪崩 | 实时价格 | 暂停所有买入 48h，重新评估 |
| 资金费率异常飙升 | > 0.1%/8h | 减少仓位 50%, 告警 |
| 月线没有进入冷却期 | halving+24 月后还在持仓 | 强制减仓告警 |

### 8.2 紧急停机

```bash
# 一个 kill switch 文件
touch /var/run/btc-strategy/PAUSE

# 主循环每次开始前检查
if [ -f /var/run/btc-strategy/PAUSE ]; then
    log "Paused by kill switch"
    exit 0
fi
```

按下 PAUSE 后:
- 不再开新仓
- 已有挂单保持（手动取消的话另说）
- 已有持仓保持（不会自动平仓）
- 告警继续发

恢复: `rm /var/run/btc-strategy/PAUSE`

---

## 9. 测试计划

### 9.1 单元测试

- [ ] `detect_swings()` 在合成数据上的正确性
- [ ] `get_active_levels()` 在过期 / 跨年时的边界
- [ ] 月份计算正确（处理 30/31 天月份）
- [ ] 杠杆升级触发逻辑（边界 equity = INITIAL × 1.5）
- [ ] Force exit 只触发一次（不重复）

### 9.2 集成测试

- [ ] OKX testnet 完整跑一周
- [ ] 模拟价格跳跃 ±20% 的应对
- [ ] 模拟 API 宕机的恢复
- [ ] 杠杆切换在持仓状态下的正确性

### 9.3 回放测试

```python
# 用历史数据完整模拟
# 喂入 2018-2026 真实日线 + OKX 真实订单簿
# 验证策略行为与 backtest 一致
def replay_test():
    sim = StrategySimulator()
    for date in historical_dates:
        daily_data = load_historical_bar(date)
        sim.daily_decision_loop(daily_data)
    
    # 验证最终 equity 在 ±10% 之内匹配 backtest
    assert sim.final_equity > 50_000  # 跨周期至少 7x
```

### 9.4 实盘试运行

- 先用 **$700** 跑 3 个月（10% 资金验证）
- 监控:
  - 信号触发频率是否符合 backtest 预期
  - 资金费实际累积 vs 假设
  - 滑点实际 vs 假设
- 满意后再上 $7,000 全仓

---

## 10. 备用方案 A: 1x → 3x phased (最保守)

如果你怕 2x 起步在意外大跌（macro 黑天鹅）中受伤：

```yaml
leverage:
  phase1: 1.0           # 起步用现货模式
  phase2: 3.0           # 翻倍后切换永续
  upgrade_trigger: 1.5
```

OKX 实现:
- PHASE 1: 在**现货账户**操作，USDT 买 BTC 现货
- 触发升级时: 现货卖光 → 转账到合约账户 → 开 3x 永续多
- 优点: 完全无爆仓风险（在 PHASE 1）
- 缺点: 切换有摩擦（10-20 USDT 手续费 + 跨账户转账时间）

预期表现:
- 上周期 backtest: 12.98x (vs v11h-a 2x 起步会爆仓)
- 当前周期 backtest: 2.43x

---

## 11. 备用方案 B: 1.5x → 3x phased (历史最优)

如果你能在 OKX 上精确控制有效杠杆：

```yaml
leverage:
  phase1: 1.5           # 通过 size 控制实现
  phase2: 3.0
  upgrade_trigger: 1.5
```

OKX 实现:
- 杠杆滑块设 3x，但每个仓位 size 计算时用 1.5x 上限
- 例: equity $7000, max notional position = $7000 × 1.5 = $10,500

预期表现:
- 上周期 backtest: 14.44x
- 当前周期 backtest: 2.98x

---

## 12. 实施 checklist

### Phase 0: 准备 (现在 ~ 2026-08)

- [ ] OKX 账户设置（KYC, API key）
- [ ] 服务器部署（推荐: AWS Tokyo / Singapore 低延迟）
- [ ] 数据库 + 监控搭建（Telegram bot, Grafana）
- [ ] 代码实现 + 单元测试
- [ ] 在 OKX testnet 跑通完整流程
- [ ] 历史回放测试通过
- [ ] 监控告警全部到位

### Phase 1: 试运行 (2026-09 见底前后)

- [ ] $700 (10%) 实盘验证 1 个月
- [ ] 信号触发与 backtest 误差 < 10%
- [ ] 滑点 + 资金费实际成本 < 假设的 1.2 倍
- [ ] 没出现意外 bug 或 race condition

### Phase 2: 全资金 (Phase 1 验证通过后)

- [ ] 上 $7,000 真金
- [ ] 持续监控，每周 review
- [ ] 不要手动干预，除非:
  - 黑天鹅事件（监管禁令等）
  - 系统 bug
- [ ] 准备好心理预期: PHASE 1 期间可能 -40% ~ -60% drawdown

### Phase 3: 持有 (2027 ~ 2029-09)

- [ ] 不动手. 看不看都行.
- [ ] 每月查一次告警没漏
- [ ] 2028-04-15 halving 那天确认参数无误

### Phase 4: 逃顶 + 转空 (2029-06 ~ 2030-10)

- [ ] halving+14 月: 唯一一次保命 trim 20% 应触发
- [ ] halving+18 月: **FORCE EXIT 多头全平应自动触发** (先平多变现金)
- [ ] force exit 后: 自动分配 30% 现金做空 (前置 3 层 flat)
- [ ] 熊市期间: 空头触阻力进/触支撑平, +30% 硬止损
- [ ] halving+30 月: 平所有空, 现金回归, 准备重启

### Phase 5: 冷却 / 重启 (2030-10 之后)

- [ ] 检查现金管理（要不要 USDC stake 拿 5-8% 年化）
- [ ] 准备下个周期参数（更新 next_halving_date 到 2032-04）

---

## 12.5 唯一的人工 review 节点 — Halving 那天

> **核心：策略其他全部自动，halving 当天人工 5 分钟 review 一次。**

### 12.5.1 为什么需要这次 review？

历史 3 次减半中 2 次出现 **post-halving 1-6 个月回调 -18% 到 -24%**：

| 减半 | 减半后最深底 | 何时 | 跌幅 |
|---|---:|---|---:|
| 2016-07-09 | $531 | 减半当月 | **-18%** |
| 2020-05-11 | $8,374 | 减半当月 | -3% （例外，COVID 已提前出血）|
| 2024-04-20 | $49,121 | 减半 +4 月 | **-24%** |

### 12.5.2 这个回调真的危险吗？拆开来看

**风险点 A：策略会不会在 post-halving 浅回调里"乱买"？**

→ **不会**。S/R 检测已经天然防住了。证据（backtest 实测）：

| 减半 | 6 月最深 dip | 当时 active 支撑（pre-halving 半年内最高 swing low）| 是否在 ±3% 范围内 | v11h-a 行为 |
|---|---:|---:|:---:|---|
| 2016 | $531 | $437 | ❌（差 22%）| 不买（自然 blackout）|
| 2020 | $8,374 | $6,617 | ❌（差 27%）| 不买 |
| 2024 | $49,121 | $38,522 | ❌（差 27%）| 不买 |

→ **历史 3 次 halving，v11h-a 都不会在 post-halving dip 中误触买入信号**。  
因为 post-halving dip 距离"上次低点"还差 20-30%，够不到我们的"耐心买价"。所以**显式的 6 月 blackout 是多余的**，v12 加了反而拖累收益（PW 14.44x → 9.99x, W 2.98x → 1.90x）。

**风险点 B：已有持仓在 post-halving 回调中爆仓？**

→ **这才是真正的风险**。到 halving 那天，账户大概率：
- 已经升级到 3x 杠杆
- 已经累积大仓位
- 浮盈可能 +50% 到 +300%

如果 BTC -25% 回调，3x 杠杆吃 -75% equity。是否爆仓取决于**avg cost cushion**：

```
cushion ratio = current_price / avg_cost

if cushion = 2.0 (即浮盈 100%):  BTC -25% 后还有 +50% 浮盈, 远离爆仓
if cushion = 1.3 (即浮盈 30%):   BTC -25% 后还剩 ~ -2% 浮亏, 安全
if cushion = 1.0 (即浮盈 0%):    BTC -25% 后浮亏 -25%, 3x 杠杆 = -75% equity, 接近爆仓
```

→ **风险等级完全由 cushion 决定**。

### 12.5.3 backtest 显示我们 cushion 一般有多大？

v11h-a 在 2024-04 halving 时的实际状态（backtest 数据）：

```
2024-04-20 halving:
  BTC 价: $65,000
  avg_cost: ~$35,000 (从 2022-09 起累积)
  cushion: 1.86x (浮盈 86%)
  leverage: 3x (已升级)

→ 2024-08 dip 到 $49k:
  距 avg cost: $49k/$35k = 1.40x (浮盈仍 40%)
  equity 损失: 从 cushion 1.86 跌到 1.40 = 损失 ~24% equity
  → 平稳度过, 远未爆仓
```

但**下个 cycle 不保证同样 cushion**。如果你 2026-09 入场判断略晚（实际底是 2027-03），到 2028-04 halving 时只累积 13 个月而非 19 个月，cushion 可能小很多。

### 12.5.4 Review 流程（5 分钟手动检查）

**在 2028-04-15 halving 当天**，登录 OKX 看三个数据：

```
1. 查当前 avg cost: $X (合约页面"开仓均价")
2. 查当前 BTC 价格: $Y
3. 算 cushion = Y / X
4. 查当前 leverage: 应该是 3.0x

──────────────────────────────────────────────
判断（基于 cushion）:

  cushion >= 1.50 (浮盈 ≥ 50%):
    → ✅ 保持不动. backtest 验证过这种 cushion 能扛 -25% 回调
    → 自动策略继续

  cushion 1.30 ~ 1.50 (浮盈 30%-50%):
    → ⚠️ 中性. 手动 trim 15-20% 提高 cash 储备
    → 杠杆维持 3x
    → 兑现的 cash 等 6 个月后再考虑加回

  cushion 1.10 ~ 1.30 (浮盈 10%-30%):
    → ⚠️ 偏危险. 手动 trim 30%
    → 把 OKX 杠杆滑块从 3x 降到 2x
    → 等 halving + 6 月后, 如果稳定回到原 cushion 水平, 恢复 3x

  cushion < 1.10 (浮盈 < 10%, 入场太晚):
    → 🚨 高危. trim 50%
    → 杠杆降到 1.5x
    → 等下一个明显的支撑信号再加仓
    → 接受可能错过部分 bull 涨幅作为代价
```

**判断不靠主观，只靠 cushion 数字**。整个流程 5 分钟完成。

### 12.5.5 为什么不直接写成自动规则？

技术上可以。但有两个原因留作人工：

1. **触发频率极低**（每 4 年一次），不值得增加代码复杂度
2. **新 cycle 可能有新信息**（监管变化、宏观环境、ETF 流入）需要人脑综合判断

如果你 2028-04 时已经离开交易行业，可以委托一位你信任的人按上述规则执行（不需要交易经验，只需要会算除法）。或者干脆**默认 trim 25%**作为保险，接受少赚一点换确定性。

### 12.5.6 重要：这不是"逃顶"

这次 review 跟 halving+19 月的 force exit 是**完全不同性质的动作**：

| | Halving 当天 review | Halving +19 月 force exit |
|---|---|---|
| 目的 | 防 post-halving 回调爆仓 | 周期顶完整逃离 |
| 动作 | 视 cushion trim 15-50% | 强制全平 |
| 杠杆 | 可能降到 2x 或 1.5x | 降到 0 |
| 后续 | 继续持有剩余仓位骑 bull | 进入冷却期，不持仓 |

Halving 当天 review = **风险微调**，不是退出。

---

## 13. 心理预期管理

| 阶段 | 心理挑战 | 提前准备 |
|---|---|---|
| PHASE 1 中（2026-12 ~ 2027-03）| 可能 -40% 浮亏 | 知道这是 backtest 里的常态 |
| 杠杆升级时 | "现在升 3x 是不是太晚了" | 规则决定，不思考 |
| 中期假顶（2028 中）| "要不要先逃" | 时间未到 force_exit，按规则 |
| 真正的顶（2029-09 附近）| "再涨一点再卖" | 规则触发 trim, 不犹豫 |
| Force exit 那天 | "万一这不是顶" | 已 backtest 验证, 信号过 17 月已可靠 |
| 退出后 BTC 继续涨 | "卖飞了" | 接受这是机械策略的代价 |
| Phase 4 等下个底 | "无聊, 找新策略" | 不动. 利润已实现. |

---

## 14. 一些重要数字

### 14.1 倒金字塔 5 层的 size 分布（用 1.5 倍增长）

| 层 | 占比 | 累计占比 | $7k × 2x = $14k 总名义下的金额 |
|---:|---:|---:|---:|
| L1 | 7.6% | 7.6% | $1,062 |
| L2 | 11.4% | 19.0% | $1,593 |
| L3 | 17.0% | 36.0% | $2,389 |
| L4 | 25.6% | 61.6% | $3,584 |
| L5 | 38.4% | 100% | $5,376 |
| **总** | **100%** | — | **$14,000** |

升级到 3x 后（PHASE 2），如果当时 equity = $10,500:
- 新总名义 = $10,500 × 3 = $31,500
- 但已经填了几层，要从总目标里减掉已经持有的

### 14.2 资金费率历史范围

| 市场状态 | 8h 资金费 |
|---|---|
| 强 bull (2024 末) | 0.05-0.10% (你付 5-10%/月) |
| 普通 bull | 0.01-0.03% (你付 1-3%/月) |
| Chop / 早期 | 0-0.02% |
| 顶部 / blowoff | 0.10-0.30% (你付 10-30%/月) ⚠️ |
| 熊市 | -0.02 ~ 0.01% (你可能收钱) |

→ 在 PHASE 3a/3b（halving+12-19 月）期间，资金费可能很贵。这也是 force exit 在 +19 月有效的理由之一: 不让你长期在高费率多仓里出血。

### 14.3 历史顶部资金费率

| 顶 | 顶日资金费率 | 持续高位多久 |
|---|---|---|
| 2021-11-08 | 0.10% / 8h | ~2 周 |
| 2024-03-13 | 0.15% / 8h | ~3 周 |
| 2025-10-06 | 0.08% / 8h | ~1 周 |

策略已经在这些日期附近 trim/force_exit，避开了最贵的资金费阶段。

---

## 15. FAQ

**Q: 如果 2026-09 不是真的底，BTC 继续跌到 $40k 怎么办？**  
A: 倒金字塔机制本身就是为这个设计的。L1-L5 跨约 20% 价格区间，所以你不用押在一个点上。这正是它相对"单点 3x HODL"的核心价值（±3 月择时容错）。**起步 1.5x**（不是 2x）让你在 L5 之后继续跌也扛得住——回测中 2x 起步在 2018-19 的 -55% 下跌里爆仓，1.5x 活下来。

**Q: 如果 halving 后周期延长了，2029-10 还在涨怎么办？**  
A: 你在 +14 月已经 trim 20% 锁了部分利润，+18 月 force exit 全平。即使之后继续涨，**机械策略接受错过最后一段涨幅**是它的常态——历史 3 次顶都在 +17.3~18.0 月，赌延长是负期望。

**Q: 如果 2027 突然黑天鹅 BTC -50% 一天怎么办？**  
A: 这是 PHASE 1（1.5x）期间最危险的尾部风险。1.5x 下单日 -50% = equity -75%，痛但**不爆仓**（这正是不用 2x 起步的原因）。倒金字塔的低 avg cost 进一步提供缓冲。如果极度保守，用第 10 节方案 A（1x 现货起步）。

**Q: 熊市做空会不会被 V 反弹打爆？**  
A: 这是 PHASE 4 的真实尾部风险，**回测没覆盖**（2018/2022 都是干净单边熊市，short stop 从未触发）。防护：(1) 只投 30% 现金做空，(2) 空头杠杆仅 1.5x，(3) +30% 硬止损。如果熊市出现 V 形反转，最坏损失约 30% × 30% 现金 = 总权益 -9%。**别把全部身家压在熊市干净下跌上**——这是 70%/2.5x 激进配置被否决的原因。

**Q: 资金费突然飙升怎么办？**  
A: 监控告警 + 减仓 50%（risk 配置里设定的）。极端情况手动 PAUSE。

**Q: 自己监控有压力，能不能托管？**  
A: 全自动一旦上线，监控只需每周看一次告警。如果实在不放心，可以请人 paid monitoring service。

**Q: 我可以中途加钱吗？**  
A: 可以。加钱后重新计算 layer sizes（用新的 equity 重新算 1/13.19 比例）。但**杠杆升级触发是基于回报倍数**而非绝对金额，所以加钱后触发延后。

---

## 文档版本

- **v1.0** (2026-05-23): 初稿，基于 v11h-a 策略 + 2x→3x phased leverage 配置
- 参数基于 PW (2018-2022) + W (2022-2026) 两个完整周期 backtest 调出
- 跨周期几何均值: 5.62x (1x→3x) / 6.55x (1.5x→3x) / 4-7x (2x→3x 推荐)

---

**重要免责**: 策略基于历史数据回测。未来周期可能与历史不同。
- 如果黑天鹅事件 + 高杠杆 → 可能爆仓
- 如果监管禁止永续 → 需要重新评估
- 如果 BTC 4 年周期失效 → halving 锚定逻辑失效

**核心安全垫**:
1. 倒金字塔降低 avg cost（缓冲单边下跌）
2. 杠杆 phased（早期低杠杆，利润中长出来）
3. Halving 时间退出（机械逃顶）
4. Cycle 间冷却（等下个底而不是抄半山腰）
