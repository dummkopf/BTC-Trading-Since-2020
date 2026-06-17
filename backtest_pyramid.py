"""
Inverted-pyramid + signal-trim backtest, $7000 start, 3x max leverage.
Test period: 2022-09-01 → 2026-05-21 (covers a full bottom→top→correction cycle).

Strategy
--------
ENTRY (倒金字塔):  6 layers, sized 10/15/20/20/20/15 % of current deployment target.
                    Each layer fires on EITHER a price-ladder threshold OR a signal.
EXIT  (信号兑现):  3 trim layers, each cashes 25% of position when overbought signal fires.
RECYCLE:            After ≥1 trim, system "re-arms"; next L1 needs Mayer<0.85.
RISK:               Max effective leverage 3x; bankrupt if equity ≤ 0.
FUNDING:            -0.01% / 8h on long notional (≈ -10.95%/yr).

Benchmarks: spot HODL, 3x perp HODL.
"""
import pandas as pd, numpy as np

df = pd.read_csv("/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv",
                 parse_dates=["Date"]).sort_values("Date").set_index("Date")

# indicators (same as calibration)
df["sma200"] = df.Close.rolling(200).mean()
df["dev200"] = (df.Close / df.sma200 - 1) * 100
df["mayer"]  = df.Close / df.sma200
df["sma20"]  = df.Close.rolling(20).mean()
df["bb_std"] = df.Close.rolling(20).std()
df["bb_lo"]  = df.sma20 - 2*df.bb_std
df["bb_up"]  = df.sma20 + 2*df.bb_std
df["bb_pos"] = (df.Close - df.bb_lo)/(df.bb_up - df.bb_lo)

def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_d"] = rsi(df.Close)
w = df.Close.resample("W-MON").last()
df["rsi_w"] = rsi(w).reindex(df.index, method="ffill")

red = (df.Close < df.Open).astype(int).values
streak, c = [], 0
for x in red:
    c = c+1 if x else 0
    streak.append(c)
df["red_streak"] = streak
green = (df.Close > df.Open).astype(int).values
streak, c = [], 0
for x in green:
    c = c+1 if x else 0
    streak.append(c)
df["green_streak"] = streak

START = "2022-09-01"
END   = "2026-05-21"
data = df.loc[START:END].copy().dropna(subset=["sma200"])  # need 200DMA defined

# ====================== STRATEGY PARAMETERS ============================
INITIAL_CASH = 7_000.0
MAX_LEVERAGE = 3.0
FUNDING_RATE_8H = 0.0001            # 0.01% per 8h on long notional
FUNDING_DAILY   = FUNDING_RATE_8H * 3
TAKER_FEE       = 0.0005            # 0.05% per fill

# entry layer sizes (% of full deployment target)
LAYER_PCT      = [0.10, 0.15, 0.20, 0.20, 0.20, 0.15]
# price ladder thresholds (drawdown vs L1 fill price)
PRICE_LADDER   = [0.00, -0.03, -0.06, -0.10, -0.15, -0.20]

def signal_trigger_entry(i, r):
    """Signal-based entry trigger for layer i (0-indexed). r = row."""
    if i == 0: return (r.mayer < 0.85) or (r.rsi_w < 40)
    if i == 1: return r.red_streak >= 5
    if i == 2: return r.dev200 < -25
    if i == 3: return (r.mayer < 0.80) and (r.rsi_w < 35)
    if i == 4: return r.dev200 < -35
    if i == 5: return r.mayer < 0.70
    return False

# trim layers
TRIM_PCT  = [0.25, 0.25, 0.25]
def signal_trigger_trim(i, r):
    if i == 0: return (r.mayer > 1.4) or (r.rsi_w > 70)
    if i == 1: return (r.mayer > 1.6) or (r.rsi_w > 75)
    if i == 2: return r.mayer > 2.0
    return False

# ====================== BACKTEST ENGINE ================================
class Sim:
    def __init__(self, label):
        self.label = label
        self.cash = INITIAL_CASH
        self.pos_btc = 0.0
        self.avg_cost = 0.0
        self.layers_filled = [False]*6
        self.trims_done    = [False]*3
        self.l1_price = None
        self.re_armed = True       # ready to fire L1
        self.total_realized = 0.0
        self.total_funding  = 0.0
        self.total_fees     = 0.0
        self.trades = []
        self.history = []
        self.liquidated = False

    def equity(self, mark):
        if self.pos_btc == 0: return self.cash
        return self.cash + self.pos_btc * (mark - self.avg_cost)

    def leverage(self, mark):
        eq = self.equity(mark)
        if eq <= 0: return float("inf")
        return self.pos_btc * mark / eq

    def buy(self, dt, mark, usd, layer_idx, reason):
        # cap by leverage
        eq = self.equity(mark)
        cur_notional = self.pos_btc * mark
        max_extra = max(0, MAX_LEVERAGE * eq - cur_notional)
        usd = min(usd, max_extra)
        if usd <= 1: return False
        btc = usd / mark
        fee = usd * TAKER_FEE
        self.cash -= fee
        self.total_fees += fee
        new_pos = self.pos_btc + btc
        self.avg_cost = (self.pos_btc * self.avg_cost + btc * mark) / new_pos
        self.pos_btc = new_pos
        self.layers_filled[layer_idx] = True
        if layer_idx == 0:
            self.l1_price = mark
            self.trims_done = [False]*3   # new cycle, re-arm trims
            self.re_armed = False
        self.trades.append((dt, "BUY", layer_idx, mark, usd, btc, reason))
        return True

    def sell(self, dt, mark, frac_of_pos, trim_idx, reason):
        if self.pos_btc <= 0: return False
        btc = self.pos_btc * frac_of_pos
        usd = btc * mark
        fee = usd * TAKER_FEE
        realized = btc * (mark - self.avg_cost) - fee
        self.cash += realized + btc * self.avg_cost  # return invested cost + realized PnL
        # Wait — for a perp: closing returns the unrealized to cash. Simpler:
        # equity unchanged by close; just convert position into cash equiv.
        # Let me redo cleanly: equity = cash + pos*(mark-avg); closing removes that piece
        # and adds it to cash. So: cash += btc*(mark-avg_cost) - fee
        # The cost basis is leverage, not real cash. Correction:
        self.cash -= btc * self.avg_cost  # undo the wrong add above
        # net effect of the two lines: cash += btc*(mark-avg_cost) - fee
        self.total_realized += btc * (mark - self.avg_cost) - fee
        self.total_fees += fee
        self.pos_btc -= btc
        if self.pos_btc < 1e-9:
            self.pos_btc = 0.0
            self.avg_cost = 0.0
            self.layers_filled = [False]*6
            self.l1_price = None
        self.trims_done[trim_idx] = True
        self.re_armed = True
        self.trades.append((dt, "SELL", trim_idx, mark, usd, btc, reason))
        return True

    def apply_funding(self, mark):
        if self.pos_btc > 0:
            f = self.pos_btc * mark * FUNDING_DAILY
            self.cash -= f
            self.total_funding += f

    def step(self, dt, r):
        mark = r.Close
        self.apply_funding(mark)
        # liquidation
        if self.equity(mark) <= 0:
            self.liquidated = True
            self.pos_btc = 0.0; self.avg_cost = 0.0; self.cash = 0.0
            self.history.append((dt, mark, 0.0, 0.0, 0.0, "LIQ"))
            return

        # trims first (lock gains before next dip)
        for i, frac in enumerate(TRIM_PCT):
            if not self.trims_done[i] and signal_trigger_trim(i, r):
                self.sell(dt, mark, frac, i, f"trim{i+1}")

        # entries
        if self.pos_btc == 0 or (self.layers_filled[0] and not self.layers_filled[-1]):
            # re-arm gate: only allow new L1 when re_armed AND mayer<0.85
            if not self.layers_filled[0]:
                if self.re_armed and signal_trigger_entry(0, r):
                    target_notional = self.equity(mark) * MAX_LEVERAGE
                    sz = target_notional * LAYER_PCT[0]
                    self.buy(dt, mark, sz, 0, "L1 signal")
            else:
                # try L2..L6 in order
                for i in range(1, 6):
                    if self.layers_filled[i]: continue
                    price_ok = (mark <= self.l1_price * (1 + PRICE_LADDER[i]))
                    sig_ok = signal_trigger_entry(i, r)
                    if price_ok or sig_ok:
                        # size relative to original target at L1 fill — keep total bounded
                        target_notional = self.equity(mark) * MAX_LEVERAGE
                        sz = target_notional * LAYER_PCT[i]
                        reason = f"L{i+1} {'price' if price_ok else 'signal'}"
                        self.buy(dt, mark, sz, i, reason)
                        break  # only one layer per bar

        eq = self.equity(mark)
        self.history.append((dt, mark, self.cash, self.pos_btc, eq,
                              "x".join([f"{i}" for i,v in enumerate(self.layers_filled) if v]) or "-"))

# ====================== BENCHMARKS =====================================
def spot_hodl(df, cash=INITIAL_CASH):
    p0 = df.Close.iloc[0]
    btc = cash / p0
    return df.Close * btc

def lev_hodl(df, cash=INITIAL_CASH, L=3.0):
    out = []
    pos = cash * L / df.Close.iloc[0]   # BTC
    avg = df.Close.iloc[0]
    c = cash
    liq = False
    for dt, r in df.iterrows():
        if not liq:
            eq = c + pos * (r.Close - avg)
            c -= pos * r.Close * FUNDING_DAILY
            eq = c + pos * (r.Close - avg)
            if eq <= 0:
                liq = True; eq = 0; pos = 0; c = 0
        out.append(eq if not liq else 0)
    return pd.Series(out, index=df.index)

# ====================== RUN ============================================
sim = Sim("PyramidTrimRecycle")
for dt, r in data.iterrows():
    sim.step(dt, r)

equity_strategy = pd.Series([h[4] for h in sim.history], index=[h[0] for h in sim.history])
equity_spot     = spot_hodl(data)
equity_lev      = lev_hodl(data)

# ====================== REPORT =========================================
def stats(s, label):
    s = s.dropna()
    rets = s.pct_change().dropna()
    total = s.iloc[-1] / s.iloc[0] - 1
    years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1/years) - 1 if s.iloc[0] > 0 else float('nan')
    peak = s.cummax()
    mdd = (s/peak - 1).min()
    vol_ann = rets.std() * np.sqrt(365)
    sharpe = (rets.mean()*365) / (rets.std()*np.sqrt(365)) if rets.std() > 0 else 0
    print(f"  {label:30}  end=${s.iloc[-1]:>10,.0f}  total={total*100:>+7.0f}%  CAGR={cagr*100:>+6.1f}%  MDD={mdd*100:>+6.1f}%  Sharpe={sharpe:>4.2f}")

print(f"\n{'='*100}")
print(f"BACKTEST  {data.index[0].date()} → {data.index[-1].date()}   ({(data.index[-1]-data.index[0]).days} days, {(data.index[-1]-data.index[0]).days/365.25:.1f} yrs)")
print(f"  Initial: ${INITIAL_CASH:,.0f}  |  Max leverage {MAX_LEVERAGE}x  |  Funding -{FUNDING_DAILY*100:.3f}%/day  |  Fees {TAKER_FEE*100:.2f}%/fill")
print(f"  BTC start ${data.Close.iloc[0]:,.0f} → end ${data.Close.iloc[-1]:,.0f}  ({data.Close.iloc[-1]/data.Close.iloc[0]-1:+.0%})")
print(f"{'='*100}")
stats(equity_spot, "Spot HODL")
stats(equity_lev, "3x Perp HODL")
stats(equity_strategy, "Pyramid + Trim + Recycle")

print(f"\nStrategy details:")
print(f"  Liquidated: {sim.liquidated}")
print(f"  Final cash: ${sim.cash:,.2f}   Final pos: {sim.pos_btc:.5f} BTC @ avg ${sim.avg_cost:,.0f}")
print(f"  Total realized PnL: ${sim.total_realized:,.0f}")
print(f"  Total funding paid: ${sim.total_funding:,.0f}")
print(f"  Total fees: ${sim.total_fees:,.0f}")
print(f"  Total trades: {len(sim.trades)}  (BUY {sum(1 for t in sim.trades if t[1]=='BUY')}, SELL {sum(1 for t in sim.trades if t[1]=='SELL')})")

print(f"\nTrade log (all {len(sim.trades)} fills):")
print(f"  {'date':10}  {'side':4}  {'L#':2}  {'price':>9}  {'usd':>10}  {'btc':>9}  reason")
for t in sim.trades:
    print(f"  {t[0].date()!s:10}  {t[1]:4}  {t[2]:>2}  ${t[3]:>8,.0f}  ${t[4]:>9,.0f}  {t[5]:>9.5f}  {t[6]}")

# yearly equity snapshots
print(f"\nEquity snapshots (year-end):")
print(f"  {'date':10}  {'spot':>11}  {'3x perp':>11}  {'strategy':>11}  {'lev_eff':>7}")
for yr in [2022, 2023, 2024, 2025, 2026]:
    snap = pd.Timestamp(f"{yr}-12-31") if yr < 2026 else data.index[-1]
    if snap > data.index[-1]: snap = data.index[-1]
    try:
        s = equity_spot.asof(snap); l = equity_lev.asof(snap); k = equity_strategy.asof(snap)
        row = data.asof(snap)
        eff_lev = sim.history[ data.index.get_loc(snap) ][3] * row.Close / k if k > 0 else 0
    except Exception as e:
        s = l = k = eff_lev = float('nan')
    print(f"  {snap.date()!s:10}  ${s:>10,.0f}  ${l:>10,.0f}  ${k:>10,.0f}  {eff_lev:>7.2f}x")

# save equity curve to CSV
out = pd.DataFrame({
    "date": [h[0] for h in sim.history],
    "btc_close": [h[1] for h in sim.history],
    "strategy_equity": [h[4] for h in sim.history],
    "spot_hodl": equity_spot.values,
    "lev3x_hodl": equity_lev.values,
})
out.to_csv("/home/user/BTC-Trading-Since-2020/backtest_equity_curve.csv", index=False)
print(f"\nEquity curve saved: backtest_equity_curve.csv")
