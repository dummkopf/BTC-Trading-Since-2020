"""
v5: RSI-driven rolling pyramid + cross-window validation.

Replaces v4's fixed a%/c% thresholds with daily-RSI triggers:
- BUY layer when RSI_d < buy_rsi (oversold). N layers, 1.b× growth, cooldown.
- TRIM d% when RSI_d > sell_rsi AND price > avg_cost.
- After trim → reset layer count → repeat cycle.

To attack overfitting we evaluate each config across 4 distinct regimes
and rank by AVERAGE (and worst-case) performance, not single-window peak.

Windows:
  W1 BEAR        2021-11-08 → 2022-11-21  (-77% drawdown)
  W2 EARLY BULL  2022-11-21 → 2024-03-13  (+469%)
  W3 MID/CHOP    2024-03-13 → 2024-09-06  (-26% then chop)
  W4 LATE BULL   2024-09-06 → 2025-08-31  (+101%)
  W5 FULL POST   2022-09-01 → 2025-08-31  (the v4 window, for compare)
"""
import pandas as pd, numpy as np, itertools

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")
def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_d"] = rsi(df.Close)

INITIAL = 7000.0
FUNDING_DAILY = 0.0001 * 3
FEE = 0.0005
MAX_LEV = 3.0
MM = 0.005

WINDOWS = {
    "W1-BEAR":      ("2021-11-08", "2022-11-21"),
    "W2-EARLYBULL": ("2022-11-21", "2024-03-13"),
    "W3-MIDCHOP":   ("2024-03-13", "2024-09-06"),
    "W4-LATEBULL":  ("2024-09-06", "2025-08-31"),
    "W5-FULL":      ("2022-09-01", "2025-08-31"),
}

def simulate(data, buy_rsi, sell_rsi, b, d, max_layers, cooldown_buy, cooldown_trim):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0
    days_since_buy = 9999; days_since_trim = 9999
    fees_t = funding_t = realized_t = 0.0
    n_trims = 0; n_buys = 0
    near_liq = 1.0; liq = False

    for i, (dt, r) in enumerate(data.iterrows()):
        days_since_buy += 1; days_since_trim += 1
        if liq: continue
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_t += f
            # intraday liquidation
            eq_low = cash + pos * (r.Low - avg); mm_req = pos*r.Low*MM
            near_liq = min(near_liq, eq_low/INITIAL)
            if eq_low <= mm_req:
                liq = True; cash = 0; pos = 0; avg = 0; continue

        # TRIM
        if (pos > 0 and r.rsi_d > sell_rsi and r.Close > avg
            and days_since_trim >= cooldown_trim):
            btc_sell = pos * (d/100)
            usd = btc_sell * r.Close; fee = usd * FEE
            realized = btc_sell * (r.Close - avg) - fee
            cash += realized
            realized_t += realized; fees_t += fee
            pos -= btc_sell
            if pos < 1e-9: pos = 0; avg = 0
            n_trims += 1
            days_since_trim = 0
            layer = 0   # reset pyramid
            l1_size = 0
            continue

        # BUY
        if r.rsi_d < buy_rsi and days_since_buy >= cooldown_buy and layer < max_layers:
            equity = cash + pos*(r.Close - avg) if pos > 0 else cash
            if equity <= 0:
                liq = True; continue
            if layer == 0:
                geo_sum = sum((1+b)**k for k in range(max_layers))
                l1_size = (equity * MAX_LEV) / geo_sum
                size = l1_size
            else:
                size = l1_size * (1+b)**layer
            cur_notional = pos * r.Close
            size = min(size, max(0, MAX_LEV*equity - cur_notional))
            if size > 1:
                btc = size / r.Close
                fee = size * FEE; cash -= fee; fees_t += fee
                new_pos = pos + btc
                avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                pos = new_pos
                layer += 1; n_buys += 1
                days_since_buy = 0

    # final equity (use last close)
    last = data.iloc[-1]
    final = cash + pos*(last.Close - avg) if pos > 0 else cash
    if liq: final = 0
    return dict(final=final, near_liq=near_liq, liq=liq, n_buys=n_buys,
                n_trims=n_trims, realized=realized_t, funding=funding_t, fees=fees_t)

# Pre-slice windows
slices = {name: df.loc[s:e].copy() for name,(s,e) in WINDOWS.items()}

# Spot HODL benchmarks per window
def spot_ret(data): return data.Close.iloc[-1]/data.Close.iloc[0]
def hodl_perp(data, L):
    p0 = data.Close.iloc[0]; pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq = False
    for _, r in data.iterrows():
        if liq: continue
        c -= pos*r.Close*FUNDING_DAILY
        el = c + pos*(r.Low - avg); mm_r = pos*r.Low*MM
        if el <= mm_r: liq=True; c=0;pos=0;avg=0
    final = c + pos*(data.Close.iloc[-1]-avg) if pos > 0 else c
    return max(final, 0), liq

print(f"\n{'='*120}\nBENCHMARKS PER WINDOW")
print(f"{'window':14}  {'days':>4}  {'BTC':>9}  {'spot':>9}  {'2x HODL':>10}  {'3x HODL':>10}")
for name, data in slices.items():
    s = INITIAL * spot_ret(data)
    h2,l2 = hodl_perp(data, 2.0)
    h3,l3 = hodl_perp(data, 3.0)
    print(f"  {name:14}  {len(data):>4}  ${data.Close.iloc[0]:>4,.0f}→${data.Close.iloc[-1]:,.0f}  ${s:>8,.0f}  ${h2:>9,.0f}{'!' if l2 else ' '}  ${h3:>9,.0f}{'!' if l3 else ' '}")

# ============= GRID =============
grid = list(itertools.product(
    [30, 35, 40],   # buy_rsi
    [60, 65, 70],   # sell_rsi
    [0.0, 0.5],     # 1.b (1.0 = flat, 1.5 = geo)
    [20, 33, 50],   # d% trim
    [5, 7],         # max_layers
    [3, 7],         # cooldown buy
    [3, 7],         # cooldown trim
))
print(f"\nRunning {len(grid)} configs × {len(WINDOWS)} windows = {len(grid)*len(WINDOWS)} simulations...")

rows = []
for params in grid:
    buy_rsi, sell_rsi, b, d, mx, cb, ct = params
    results_per_window = {}
    for wname, data in slices.items():
        r = simulate(data, buy_rsi, sell_rsi, b, d, mx, cb, ct)
        # report as multiplier vs initial (so we can compare across windows fairly)
        results_per_window[wname] = r["final"] / INITIAL
    # ranking metric: GEOMETRIC mean across W1..W4 (excluding W5 to avoid double-counting)
    mults = [results_per_window[w] for w in ["W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL"]]
    if any(m <= 0 for m in mults):
        geo = 0
        worst = 0
    else:
        geo = np.prod(mults)**(1/4)
        worst = min(mults)
    rows.append({
        "buy_rsi": buy_rsi, "sell_rsi": sell_rsi, "1.b": 1+b,
        "d%": d, "N": mx, "cb": cb, "ct": ct,
        **{w: results_per_window[w] for w in WINDOWS},
        "geo_4w": geo,
        "worst_4w": worst,
    })

res = pd.DataFrame(rows)
res = res.sort_values("geo_4w", ascending=False).reset_index(drop=True)

print(f"\n{'='*120}\nTOP 10 BY GEOMETRIC MEAN ACROSS 4 WINDOWS (W1+W2+W3+W4 — robust selection)")
print(res.head(10).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\nTOP 5 BY 'WORST WINDOW' SAFETY (max-min — most defensive)")
res_safe = res.sort_values("worst_4w", ascending=False).head(5)
print(res_safe.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\nTOP 5 BY W5-FULL (the single window v4 was optimized on — sanity check)")
res_w5 = res.sort_values("W5-FULL", ascending=False).head(5)
print(res_w5.to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\n{'='*120}\nPARAM SENSITIVITY (mean of geo_4w by parameter value)")
for col in ["buy_rsi","sell_rsi","1.b","d%","N","cb","ct"]:
    vals = sorted(res[col].unique())
    summary = res.groupby(col)["geo_4w"].agg(["mean","median","count"])
    print(f"\n  {col}:")
    print(summary.to_string(float_format=lambda x: f"{x:.2f}"))

# also note overfitting check: compare W5-FULL top vs robust top
print(f"\n{'='*120}\nOVERFITTING CHECK")
top_w5 = res.sort_values("W5-FULL", ascending=False).iloc[0]
top_geo = res.iloc[0]
print(f"  Best on W5 only:    {top_w5['W5-FULL']:.1f}x   (geo_4w score {top_w5['geo_4w']:.2f})")
print(f"  Best by geo_4w:     {top_geo['W5-FULL']:.1f}x   (geo_4w score {top_geo['geo_4w']:.2f})")
print(f"  → If big gap, the W5 winner is overfit; if small, robust.")

# save
res.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v5_rsi_results.csv", index=False)
print(f"\nSaved: backtest_v5_rsi_results.csv ({len(res)} rows)")
