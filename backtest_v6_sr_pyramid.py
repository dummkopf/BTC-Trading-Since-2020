"""
v6: Support/Resistance driven rolling pyramid + multi-window validation.

S/R detection (weekly bars, lookback-confirmed to avoid lookahead bias):
- swing low  = weekly Low is lowest in ±W window  → SUPPORT level
- swing high = weekly High is highest in ±W window → RESISTANCE level
- Levels become "active" W weeks after they form (confirmation lag).
- Levels expire after `level_lifetime_weeks` weeks if untouched.

Strategy:
- BUY a layer when daily Close is within `tol%` of an active SUPPORT,
  that support hasn't fired in `cooldown_days`, we hold <max_layers.
- TRIM `d%` when daily Close is within `tol%` of an active RESISTANCE,
  pos > 0, price > avg_cost, cooldown respected.
- Trim → resets layer counter (compound cycle).

Multi-window evaluation: W1 bear, W2 early bull, W3 mid chop, W4 late bull,
W5 full bull. Rank by geometric mean across W1-W4 (regime-robust).
"""
import pandas as pd, numpy as np, itertools

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")

# Build weekly bars
weekly = df.resample("W-MON").agg(Open=("Open","first"), High=("High","max"),
                                    Low=("Low","min"), Close=("Close","last"))
weekly = weekly.dropna()

def detect_swings(weekly_bars, w):
    """Return DataFrames of swing highs and lows, indexed by CONFIRMATION date
    (the date `w` weeks AFTER the swing — i.e. when we could have known it)."""
    highs, lows = [], []
    for i in range(w, len(weekly_bars) - w):
        wnd = weekly_bars.iloc[i-w:i+w+1]
        if weekly_bars.iloc[i].High == wnd.High.max():
            confirm_date = weekly_bars.index[i+w]
            highs.append((confirm_date, weekly_bars.iloc[i].High))
        if weekly_bars.iloc[i].Low == wnd.Low.min():
            confirm_date = weekly_bars.index[i+w]
            lows.append((confirm_date, weekly_bars.iloc[i].Low))
    return pd.DataFrame(highs, columns=["confirm","price"]), pd.DataFrame(lows, columns=["confirm","price"])

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

def simulate(data, swings_h, swings_l, tol, b, d, max_layers, cooldown, lifetime_w):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0
    fees_t = funding_t = realized_t = 0.0
    n_trims = 0; n_buys = 0
    near_liq = 1.0; liq = False
    # track last-fire date for each unique level
    support_last_fire = {}   # price -> last fire date
    resist_last_fire  = {}
    lifetime_days = lifetime_w * 7

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq: continue
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_t += f
            eq_low = cash + pos * (r.Low - avg); mm_req = pos*r.Low*MM
            near_liq = min(near_liq, eq_low/INITIAL)
            if eq_low <= mm_req:
                liq = True; cash=0; pos=0; avg=0; continue

        # Active levels at time dt
        active_supports = swings_l[(swings_l.confirm <= dt) &
                                    (swings_l.confirm > dt - pd.Timedelta(days=lifetime_days))].price.values
        active_resists  = swings_h[(swings_h.confirm <= dt) &
                                    (swings_h.confirm > dt - pd.Timedelta(days=lifetime_days))].price.values

        # TRIM: price near any active resistance
        if pos > 0 and r.Close > avg:
            for lvl in active_resists:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = resist_last_fire.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        btc_sell = pos * (d/100)
                        usd = btc_sell * r.Close; fee = usd * FEE
                        realized = btc_sell * (r.Close - avg) - fee
                        cash += realized
                        realized_t += realized; fees_t += fee
                        pos -= btc_sell
                        if pos < 1e-9: pos = 0; avg = 0
                        n_trims += 1
                        resist_last_fire[lvl] = dt
                        layer = 0; l1_size = 0
                        break  # one trim per bar

        # BUY: price near any active support
        if layer < max_layers and not liq:
            equity = cash + pos*(r.Close - avg) if pos > 0 else cash
            if equity > 0:
                for lvl in active_supports:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = support_last_fire.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
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
                                support_last_fire[lvl] = dt
                            break  # one buy per bar

    last = data.iloc[-1]
    final = cash + pos*(last.Close - avg) if pos > 0 else cash
    if liq: final = 0
    return dict(final=final, near_liq=near_liq, liq=liq, n_buys=n_buys,
                n_trims=n_trims, realized=realized_t, funding=funding_t, fees=fees_t)

# Multi-window slices
slices = {name: df.loc[s:e].copy() for name,(s,e) in WINDOWS.items()}

# We compute swings on the FULL weekly history (so we have priors for early windows)
# but we will only use those CONFIRMED before each daily date.
print(f"\n{'='*120}\nSWING DETECTION SAMPLE (w=4 weeks)")
sh, sl = detect_swings(weekly, w=4)
print(f"  weekly bars: {len(weekly)}  swing_highs: {len(sh)}  swing_lows: {len(sl)}")
print(f"\n  Latest 5 swing supports (confirmation date, price):")
for _, row in sl.tail(5).iterrows():
    print(f"    {row.confirm.date()!s:12}  ${row.price:>9,.0f}")
print(f"  Latest 5 swing resistances:")
for _, row in sh.tail(5).iterrows():
    print(f"    {row.confirm.date()!s:12}  ${row.price:>9,.0f}")

# ============= GRID =============
grid = list(itertools.product(
    [2, 4],            # swing detection window (weeks)
    [0.01, 0.02, 0.03], # tol (1/2/3% near level)
    [0.0, 0.5],        # 1.b
    [20, 33, 50],      # d% trim
    [5, 7],            # max_layers
    [7, 14],           # cooldown days
    [13, 26],          # lifetime weeks (3/6 months)
))
print(f"\nRunning {len(grid)} configs × {len(WINDOWS)} windows = {len(grid)*len(WINDOWS)} simulations...")

# Pre-compute swings for each detection window
swings_cache = {w: detect_swings(weekly, w=w) for w in [2, 4]}

rows = []
for params in grid:
    w, tol, b, d, mx, cd, lt = params
    sh, sl = swings_cache[w]
    res_w = {}
    for wname, data in slices.items():
        r = simulate(data, sh, sl, tol, b, d, mx, cd, lt)
        res_w[wname] = r["final"] / INITIAL
    mults = [res_w[k] for k in ["W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL"]]
    if any(m <= 0 for m in mults):
        geo = 0; worst = 0
    else:
        geo = np.prod(mults)**(1/4)
        worst = min(mults)
    rows.append({
        "swing_w": w, "tol%": tol*100, "1.b": 1+b, "d%": d, "N": mx,
        "cooldown": cd, "lifetime_w": lt,
        **res_w, "geo_4w": geo, "worst_4w": worst
    })

res = pd.DataFrame(rows).sort_values("geo_4w", ascending=False).reset_index(drop=True)

print(f"\n{'='*120}\nTOP 10 BY GEOMETRIC MEAN (W1-W4) — REGIME ROBUST")
print(res.head(10).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\nTOP 5 BY 'WORST OF 4 WINDOWS'")
print(res.sort_values("worst_4w", ascending=False).head(5).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\nTOP 5 BY W5-FULL (single window optimum, for comparison)")
print(res.sort_values("W5-FULL", ascending=False).head(5).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\n{'='*120}\nPARAM SENSITIVITY (mean of geo_4w)")
for col in ["swing_w","tol%","1.b","d%","N","cooldown","lifetime_w"]:
    g = res.groupby(col)["geo_4w"].agg(["mean","median","count"])
    print(f"\n  {col}:"); print(g.to_string(float_format=lambda x: f"{x:.2f}"))

print(f"\n{'='*120}\nCROSS-STRATEGY COMPARISON SUMMARY (geo_4w of best params)")
print(f"  Spot HODL                     geo_4w ≈ 0.74 (gets crushed in bear)")
print(f"  v5 RSI robust (best geo_4w)   geo_4w = 1.16   (W5 = 2.07x)")
print(f"  v6 S/R robust (best geo_4w)   geo_4w = {res.iloc[0]['geo_4w']:.2f}   (W5 = {res.iloc[0]['W5-FULL']:.2f}x)")

res.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v6_sr_results.csv", index=False)
print(f"\nSaved: backtest_v6_sr_results.csv")
