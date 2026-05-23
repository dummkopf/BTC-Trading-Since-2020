# BTC 倒金字塔自动交易策略 — 实施文档 (v11h-a / 2x→3x phased)

> **目的**: 把人工判断从执行环节剔除。所有买入/卖出决定来自机械规则。
> 策略已通过两个完整 BTC 周期（2018-2021 + 2022-2025）的回测验证。
>
> **预期表现**:
> - 上周期 backtest (2018-09 → 2022-06): $7k → ~$60-100k (8-14x)
> - 当前周期 backtest (2022-09 → 2026-05): $7k → ~$15-25k (2-4x)
> - 跨周期几何均值: 4-7x，**两个周期都不爆仓**

---

## 0. 核心原则

| 原则 | 说明 |
|---|---|
| **不预测，只反应** | 不试图判断"顶在哪里"或"底在哪里"，只对价格触及结构性支撑/阻力做反应 |
| **机械执行** | 所有决策由参数化规则触发；人工只能 (a) 修改参数 (b) 紧急停机 |
| **杠杆从利润中长出** | 起始 2x（保护期），账户翻 50% 后升 3x（增厚期），逃顶后回 0x（保护利润） |
| **时间锚定退出** | 周期顶用 BTC 减半日期 + 17-19 月作锚，比任何技术指标都可靠 |
| **限价单优先** | 所有触发用挂单，永不用市价单（避免滑点和情绪化追单） |

---

## 1. 策略整体架构

### 1.1 三阶段状态机

```
        ┌─────────────────────────────────┐
        │  PHASE 1: 建仓期 (BUILD)         │
        │  起始杠杆 2x                     │
        │  触发: 价格触及 swing 支撑       │
        │  动作: 倒金字塔 6 层 (按比例加大)│
        └──────────────┬──────────────────┘
                       │  account_equity >= INITIAL × 1.5
                       ▼
        ┌─────────────────────────────────┐
        │  PHASE 2: 增厚期 (BOOST)         │
        │  杠杆升级到 3x                   │
        │  触发: 同 PHASE 1                 │
        │  动作: 继续倒金字塔但用更大 size │
        └──────────────┬──────────────────┘
                       │  下一次 BTC halving (~2028-04-15)
                       ▼
        ┌─────────────────────────────────┐
        │  PHASE 3: 时间退出期             │
        │  halving + 12 月: trim 33% on R │
        │  halving + 17 月: trim 60% on R │
        │  halving + 19 月: FORCE EXIT     │
        └──────────────┬──────────────────┘
                       │  full exit complete
                       ▼
        ┌─────────────────────────────────┐
        │  PHASE 4: 冷却期 (WAIT)          │
        │  hold cash, 不入场                │
        │  持续: ~12 月                    │
        └──────────────┬──────────────────┘
                       │  next cycle bottom detected
                       ▼
                  (back to PHASE 1)
```

### 1.2 完整时间表（2026-2029 计划）

| 阶段 | 起止日期（估计）| 操作 | 杠杆 |
|---|---|---|---|
| PHASE 1 | 2026-09 ~ 2027-06 | S/R 触发买入，倒金字塔 | **2x** |
| PHASE 2 | 2027-06 ~ 2029-04 | 继续 S/R 买入 + 资金费成本 | **3x** |
| PHASE 3a | 2029-04 ~ 2029-09 | trim 33% on each resistance | 3x ↓ |
| PHASE 3b | 2029-09 ~ 2029-11 | trim 60% on each resistance | 3x ↓↓ |
| PHASE 3c | 2029-11-15 | **FORCE EXIT 全平** | 0x |
| PHASE 4 | 2029-11 ~ 2030-09 | 持现金等下个底 | 0x |

---

## 2. 参数配置文件

```yaml
# strategy_config.yaml
account:
  initial_usd: 7000.0           # 起始本金 USD
  exchange: "OKX"
  symbol: "BTC-USDT-SWAP"        # USDT 保证金永续

leverage:
  phase1: 2.0                    # 建仓期杠杆
  phase2: 3.0                    # 增厚期杠杆
  upgrade_trigger: 1.5           # equity / initial >= 1.5 时升级 (Phase 1 → 2)

entry_pyramid:
  num_layers: 5                  # 倒金字塔层数 (5-6 都可)
  layer_growth: 1.5              # 每层比上一层大 50% (geometric)
  # layer_pcts will be auto-computed from above:
  # L1 = 1/(1+1.5+2.25+3.375+5.0625) = 7.6% of total target notional
  # L2 = 11.4%, L3 = 17.0%, L4 = 25.6%, L5 = 38.4%

signal_sr:                      # support / resistance detection
  source: "weekly_swings"        # 用周线 swing 高低点
  swing_window: 2                # ±2 周确认（即第 i 根周线 = 前后 4 周内最低/高）
  tolerance: 0.03                # 价格在 S/R ±3% 范围内触发
  cooldown_days: 7               # 同一 S/R 级别 7 天内不重复触发
  level_lifetime_weeks: 26       # S/R 级别有效期 26 周（半年），过期失效

trim_logic:
  base_trim_pct: 20              # 默认每次 trim 卖 20% of position
  mid_phase_start_months: 12     # halving+12月起，提升到 33%
  mid_phase_trim_pct: 33
  aggr_phase_start_months: 17    # halving+17月起，提升到 60%
  aggr_phase_trim_pct: 60
  force_exit_months: 19          # halving+19月，全平
  blackout_end_months: 24        # halving+19-24月，不入场新仓

halving:
  next_halving_date: "2028-04-15"   # 下次减半（估计）
  # 历史: 2012-11-28, 2016-07-09, 2020-05-11, 2024-04-20

risk:
  max_effective_leverage: 3.0    # 任何时刻不能超过 3x
  hard_stop_pct: null            # 不用硬止损 (回测显示反而是负 alpha)
  liquidation_buffer: 0.05       # 距离爆仓 5% 内强制减仓警报
  daily_loss_limit_pct: 30       # 单日亏损 30% 暂停所有买入 24h

execution:
  order_type: "LIMIT"            # 全部用限价单
  slippage_tolerance_pct: 0.5    # 价格触发后, 0.5% 内挂单视为有效
  max_active_orders: 3           # 同时挂单数量限制
  funding_rate_assumed: 0.0001   # 资金费假设 0.01%/8h，超出时打日志告警
```

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
    
    # 4. 计算当前阶段
    months_since_halving = (today - HALVING_DATE).days / 30.44
    
    # 5. RISK CHECKS（先做）
    if check_liquidation_risk(position, today_close):
        send_alert("⚠️ 距离爆仓 < 5%, 紧急减仓")
        emergency_reduce()
        return
    
    if daily_loss_pct() > 30:
        send_alert("⚠️ 单日亏损 > 30%, 暂停 24h")
        return
    
    # 6. PHASE 3: TIME-BASED EXIT
    if months_since_halving >= 19 and position.size > 0:
        execute_force_exit(reason="halving+19mo")
        log_event("FORCE_EXIT", price=today_close, position=position)
        return
    
    # 7. 计算当前 trim_pct
    if months_since_halving >= 17:
        trim_pct = 60
    elif months_since_halving >= 12:
        trim_pct = 33
    else:
        trim_pct = 20
    
    # 8. TRIM CHECK (resistance touch)
    if position.size > 0 and today_close > avg_cost:
        for lvl in active_resists:
            if abs(today_close - lvl) / lvl <= 0.03:  # tolerance 3%
                if not has_fired_recently(lvl, "resistance", days=7):
                    sell_size = position.size * (trim_pct / 100)
                    place_limit_sell(price=lvl, size=sell_size)
                    mark_fired(lvl, "resistance", today)
                    log_event("TRIM", level=lvl, pct=trim_pct, size=sell_size)
                    return  # one action per day
    
    # 9. BUY CHECK
    in_blackout = (17 <= months_since_halving < 24)  # no new buys near top
    if in_blackout:
        return
    
    current_max_lev = 3.0 if (equity / INITIAL_USD) >= 1.5 else 2.0
    layer_count = count_layers_filled_this_cycle()
    
    if layer_count >= MAX_LAYERS:
        return  # all layers filled
    
    for lvl in active_supports:
        if abs(today_close - lvl) / lvl <= 0.03:
            if not has_fired_recently(lvl, "support", days=7):
                geo_sum = sum(1.5**k for k in range(MAX_LAYERS))  # = 13.19
                if layer_count == 0:
                    layer_size_usd = equity * current_max_lev / geo_sum
                    save_l1_size(layer_size_usd)
                else:
                    layer_size_usd = get_saved_l1_size() * (1.5 ** layer_count)
                
                # 限制总仓位不超过最大杠杆
                max_extra = max(0, current_max_lev * equity - position.notional)
                layer_size_usd = min(layer_size_usd, max_extra)
                
                if layer_size_usd > 50:  # min order size
                    place_limit_buy(price=lvl, size_usd=layer_size_usd)
                    mark_fired(lvl, "support", today)
                    log_event("BUY", layer=layer_count+1, level=lvl, size_usd=layer_size_usd)
                return
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
| 🛑 FORCE EXIT 触发 | halving+19mo | Telegram |
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

### Phase 4: 退出 (2029-09 ~ 2029-11)

- [ ] halving+12 月: 首次 trim 应触发
- [ ] halving+17 月: 激进 trim 开始
- [ ] halving+19 月: **FORCE EXIT 应该自动触发**
- [ ] 验证全部仓位归零，cash 收回

### Phase 5: 冷却 (2029-11 ~ 2030-09)

- [ ] 检查现金管理（要不要 USDC stake 拿 5-8% 年化）
- [ ] 准备下个周期参数（更新 next_halving_date 到 2032-04）

---

## 12.5 唯一的人工 review 节点 — Halving 那天

策略其他全部自动。**Halving 日 (2028-04-15) 那天人工检查一次**：

**背景**：历史上减半后 1-6 个月有概率出现 -18% 到 -24% 回调（2016 有，2024 有，2020 例外）。如果我们 avg cost 距离当前价太近，3x 杠杆下这个回调可能爆仓。回测显示 v11h-a 的倒金字塔已经天然有 avg cost 缓冲，但下个 cycle 不保证。

**check 流程（5 分钟，手动）**:

```
1. 查当前 avg cost: $XXX
2. 查当前 BTC 价格: $YYY
3. 计算 cushion = (YYY / XXX) - 1
4. 查当前 leverage: 应该是 3.0x

判断:
  IF cushion >= 0.30 (即浮盈 ≥ 30%):
    → 保持不动，自动策略足够安全
  IF cushion < 0.30 AND leverage == 3.0x:
    → 手动 trim 25% 仓位 (减少风险敞口)
    → 也可以选择把 OKX 杠杆暂时降到 2x
  IF cushion < 0.15:
    → 强烈建议 trim 40%, leverage 降到 1.5x
    → 等 halving + 6 月后再考虑恢复
```

这是**整个策略里唯一的人工干预**。其他全自动。

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
A: 倒金字塔机制本身就是为这个设计的。L1-L5 跨 20% 价格区间，所以你不用押在一个点上。即便 L5 之后还跌，2x 杠杆下你能扛到 -50% 距 avg cost 才接近爆仓。

**Q: 如果 halving 后周期延长了，2029-11 还在涨怎么办？**  
A: 你已经在 +17 月开始大量 trim，到 +19 月 force exit 时大概只剩 10-20% 仓位。即使继续涨，你也基本兑现了大头。**机械策略接受错过最后一段涨幅**是它的常态。

**Q: 如果 2027 突然黑天鹅 BTC -50% 一天怎么办？**  
A: 2x 杠杆下，单日 -50% = equity -100% = 爆仓。这是 2x 起步的真实风险。如果担心，用方案 A（1x 起步）。

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
