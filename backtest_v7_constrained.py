"""
v7: S/R pyramid + trend filter + RSI confirmation + hard stop-loss.

Hard constraints
----------------
1. Max leverage 3x (existing).
2. NEVER liquidated by exchange (intra-day low check, 0.5% maint margin).
3. Self-imposed stop: if (avg_cost - close)/avg_cost > stop_dd
   (default 25%), force-close ALL position. This guarantees equity
   drawdown stays within stop_dd × leverage ≈ 75%.

Signal stack (all gates must align):
- BUY layer:   price near active SUPPORT (±tol%)
            +  trend filter: close > 200DMA × allow_below
                              OR close < 200DMA × deep_oversold
                              (deep oversold is the cycle-bottom override)
            +  RSI_d < buy_rsi_max
- TRIM:        price near active RESISTANCE (±tol%)
            +  price > avg_cost (already in profit)
            +  RSI_d > trim_rsi_min
- STOP:        (avg_cost - close)/avg_cost > stop_dd → close all

Same multi-window protocol as v5/v6.
"""
import pandas as pd, numpy as np, itertools

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")
df["sma200"] = df.Close.rolling(200).mean()
def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_d"] = rsi(df.Close)

weekly = df.resample("W-MON").agg(Open=("Open","first"), High=("High","max"),
                                    Low=("Low","min"), Close=("Close","last")).dropna()
def detect_swings(weekly_bars, w):
    highs, lows = [], []
    for i in range(w, len(weekly_bars) - w):
        wnd = weekly_bars.iloc[i-w:i+w+1]
        if weekly_bars.iloc[i].High == wnd.High.max():
            highs.append((weekly_bars.index[i+w], weekly_bars.iloc[i].High))
        if weekly_bars.iloc[i].Low == wnd.Low.min():
            lows.append((weekly_bars.index[i+w], weekly_bars.iloc[i].Low))
    return pd.DataFrame(highs, columns=["confirm","price"]), pd.DataFrame(lows, columns=["confirm","price"])

# Pre-compute swings (we keep w=2 from v6 winner)
SH, SL = detect_swings(weekly, w=2)
# pre-index by confirm for speed
SH_sorted = SH.sort_values("confirm").reset_index(drop=True)
SL_sorted = SL.sort_values("confirm").reset_index(drop=True)

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

def simulate(data, tol, b, d, max_layers, cooldown, lifetime_w,
             buy_rsi_max, trim_rsi_min, allow_below_200, deep_oversold, stop_dd):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0
    fees_t = funding_t = realized_t = 0.0
    n_trims = 0; n_buys = 0; n_stops = 0
    near_liq = 1.0; liq = False
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []
    peak_equity = INITIAL

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0.0); continue
        # funding
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_t += f
            eq_low = cash + pos * (r.Low - avg); mm_req = pos*r.Low*MM
            near_liq = min(near_liq, eq_low/INITIAL)
            if eq_low <= mm_req:
                liq = True; cash=0; pos=0; avg=0
                eq_curve.append(0.0); continue

        # === HARD STOP ===
        if pos > 0 and avg > 0:
            adverse = (avg - r.Close) / avg
            if adverse >= stop_dd:
                # force close
                btc_sell = pos
                usd = btc_sell * r.Close; fee = usd * FEE
                realized = btc_sell * (r.Close - avg) - fee
                cash += realized
                realized_t += realized; fees_t += fee
                pos = 0; avg = 0
                n_stops += 1
                layer = 0; l1_size = 0
                # also cool down everything so we don't immediately re-enter
                # (we reset all level memory so any next signal can fire fresh)
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash); continue

        # active levels
        cutoff_old = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL_sorted.confirm <= dt) & (SL_sorted.confirm > cutoff_old)
        mask_h = (SH_sorted.confirm <= dt) & (SH_sorted.confirm > cutoff_old)
        active_supports = SL_sorted.loc[mask_l, "price"].values
        active_resists  = SH_sorted.loc[mask_h, "price"].values

        # === TRIM ===
        if pos > 0 and r.Close > avg and r.rsi_d > trim_rsi_min:
            for lvl in active_resists:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = resist_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        btc_sell = pos * (d/100)
                        usd = btc_sell * r.Close; fee = usd * FEE
                        realized = btc_sell * (r.Close - avg) - fee
                        cash += realized
                        realized_t += realized; fees_t += fee
                        pos -= btc_sell
                        if pos < 1e-9: pos = 0; avg = 0
                        n_trims += 1
                        resist_last[lvl] = dt
                        layer = 0; l1_size = 0
                        break

        # === BUY ===
        if layer < max_layers and not liq and r.rsi_d < buy_rsi_max:
            sma = r.sma200 if not np.isnan(r.sma200) else r.Close
            # trend filter: allow if uptrend, OR if deep oversold (cycle-bottom override)
            trend_ok = (r.Close > sma * allow_below_200) or (r.Close < sma * deep_oversold)
            if trend_ok:
                equity = cash + pos*(r.Close - avg) if pos > 0 else cash
                if equity > 0:
                    for lvl in active_supports:
                        if abs(r.Close - lvl)/lvl <= tol:
                            last = support_last.get(lvl)
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
                                    support_last[lvl] = dt
                                break

        eq_now = cash + pos*(r.Close - avg) if pos > 0 else cash
        peak_equity = max(peak_equity, eq_now)
        eq_curve.append(eq_now)

    final = eq_curve[-1] if eq_curve else 0
    if liq: final = 0
    s = pd.Series(eq_curve, index=data.index)
    max_dd = (s/s.cummax() - 1).min() if (s > 0).all() else -1.0
    return dict(final=final, near_liq=near_liq, liq=liq, n_buys=n_buys,
                n_trims=n_trims, n_stops=n_stops, realized=realized_t,
                funding=funding_t, fees=fees_t, max_dd=max_dd)

slices = {name: df.loc[s:e].copy() for name,(s,e) in WINDOWS.items()}

# ============ GRID ============
# Fixed from v6 best: swing_w=2, 1.b=1.5, N=7, cooldown=14, lifetime_w=13
# Vary: tol, d%, buy_rsi, trim_rsi, trend filter, stop_dd
grid = list(itertools.product(
    [0.01, 0.02, 0.03],  # tol
    [33, 50],            # d% trim
    [50, 55, 100],       # buy_rsi_max (100 = no gate)
    [0, 50],             # trim_rsi_min (0 = no gate)
    [0.95, 1.00],        # allow_below_200 (uptrend gate strictness)
    [0.70, 0.80],        # deep_oversold override (allow buy at -30/-20% below 200DMA)
    [0.20, 0.25, 0.30],  # stop_dd
))
print(f"Running {len(grid)} configs × {len(WINDOWS)} windows = {len(grid)*len(WINDOWS)} sims...")

rows = []
for params in grid:
    tol, d, brsi, trsi, ab200, deep, sdd = params
    res_w = {}
    info_w = {}
    for wname, data in slices.items():
        r = simulate(data, tol=tol, b=0.5, d=d, max_layers=7,
                      cooldown=14, lifetime_w=13,
                      buy_rsi_max=brsi, trim_rsi_min=trsi,
                      allow_below_200=ab200, deep_oversold=deep,
                      stop_dd=sdd)
        res_w[wname] = r["final"] / INITIAL
        info_w[wname] = r
    mults = [res_w[k] for k in ["W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL"]]
    any_liq = any(info_w[k]["liq"] for k in WINDOWS)
    max_dd_any = max((-info_w[k]["max_dd"]) for k in WINDOWS)
    worst = min(mults) if all(m > 0 for m in mults) else 0
    geo = np.prod(mults)**(1/4) if all(m > 0 for m in mults) else 0
    rows.append({
        "tol%": tol*100, "d%": d, "buy_rsi": brsi, "trim_rsi": trsi,
        "ab200": ab200, "deep_OS": deep, "stop_dd": sdd,
        **res_w,
        "geo_4w": geo, "worst_4w": worst,
        "any_liq": any_liq, "max_dd_any%": max_dd_any*100,
        "stops_total": sum(info_w[k]["n_stops"] for k in WINDOWS),
    })

res = pd.DataFrame(rows)

# Apply HARD CONSTRAINTS
safe = res[(~res.any_liq) & (res["max_dd_any%"] <= 75.0)].copy()
print(f"\n{'='*120}\nCONSTRAINT FILTER")
print(f"  Total configs: {len(res)}")
print(f"  After 'no liquidation': {(~res.any_liq).sum()}")
print(f"  After 'max DD ≤ 75%': {len(safe)}")
print(f"  Constraint-failing: {len(res)-len(safe)} configs ({(len(res)-len(safe))/len(res)*100:.0f}%)")

if len(safe) > 0:
    safe_sorted = safe.sort_values("geo_4w", ascending=False).reset_index(drop=True)
    print(f"\n{'='*120}\nTOP 10 CONSTRAINT-SAFE CONFIGS (ranked by geo_4w)")
    cols = ["tol%","d%","buy_rsi","trim_rsi","ab200","deep_OS","stop_dd",
            "W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL","W5-FULL",
            "geo_4w","worst_4w","max_dd_any%","stops_total"]
    print(safe_sorted[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    print(f"\nTOP 5 BY W5-FULL (single-window best within constraints)")
    print(safe.sort_values("W5-FULL", ascending=False)[cols].head(5).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

    print(f"\nWORST-CASE METRIC (max max_dd across all windows) — top 5 safest:")
    print(safe.sort_values("max_dd_any%")[cols].head(5).to_string(index=False, float_format=lambda x: f"{x:.2f}"))
else:
    print("\nNO CONFIGS PASS CONSTRAINTS — relax stop or lower leverage.")

# What about failed configs — what did they fail on?
fail = res[res.any_liq | (res["max_dd_any%"] > 75.0)]
if len(fail) > 0:
    print(f"\n{'='*120}\nFAILED CONFIGS BREAKDOWN")
    print(f"  Liquidated in any window: {fail.any_liq.sum()}")
    print(f"  Max DD > 75% (but no liq): {((~fail.any_liq) & (fail['max_dd_any%']>75.0)).sum()}")

res.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v7_results.csv", index=False)
print(f"\nSaved: backtest_v7_results.csv ({len(res)} rows total, {len(safe)} safe)")
