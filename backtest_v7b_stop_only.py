"""
v7b: v6 S/R pyramid + HARD STOP ONLY (no trend filter, no RSI gate).
The user's constraints: 3x lev, no liquidation, max equity DD ≤ 75%
(i.e. price -25% from avg_cost → force close).

We test stop_dd values [0.20, 0.25, 0.30, ∞=no_stop] across the v6
parameter neighborhood, then enforce the constraint filter.
"""
import pandas as pd, numpy as np, itertools

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")

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

SH, SL = detect_swings(weekly, w=2)

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

def simulate(data, tol, b, d, max_layers, cooldown, lifetime_w, stop_dd):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0
    fees_t = funding_t = realized_t = 0.0
    n_trims = 0; n_buys = 0; n_stops = 0
    near_liq = 1.0; liq = False
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0.0); continue
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_t += f
            eq_low = cash + pos * (r.Low - avg); mm_req = pos*r.Low*MM
            near_liq = min(near_liq, eq_low/INITIAL)
            if eq_low <= mm_req:
                liq = True; cash=0; pos=0; avg=0
                eq_curve.append(0.0); continue

        # === HARD STOP ===
        if pos > 0 and avg > 0 and stop_dd < 1.0:
            adverse = (avg - r.Close) / avg
            if adverse >= stop_dd:
                btc_sell = pos
                usd = btc_sell * r.Close; fee = usd * FEE
                realized = btc_sell * (r.Close - avg) - fee
                cash += realized
                realized_t += realized; fees_t += fee
                pos = 0; avg = 0
                n_stops += 1
                layer = 0; l1_size = 0
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash); continue

        # active levels
        cutoff_old = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff_old)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff_old)
        active_supports = SL.loc[mask_l, "price"].values
        active_resists  = SH.loc[mask_h, "price"].values

        # TRIM
        if pos > 0 and r.Close > avg:
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

        # BUY
        if layer < max_layers and not liq:
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
        eq_curve.append(eq_now)

    final = eq_curve[-1] if eq_curve else 0
    if liq: final = 0
    s = pd.Series(eq_curve, index=data.index)
    max_dd = (s/s.cummax() - 1).min() if (s > 0).all() else -1.0
    return dict(final=final, near_liq=near_liq, liq=liq, n_buys=n_buys,
                n_trims=n_trims, n_stops=n_stops, realized=realized_t,
                funding=funding_t, fees=fees_t, max_dd=max_dd)

slices = {name: df.loc[s:e].copy() for name,(s,e) in WINDOWS.items()}

# Compact grid around v6 winner
grid = list(itertools.product(
    [0.01, 0.02, 0.03],    # tol
    [0.5],                  # 1.b = 1.5 (v6 best)
    [20, 33, 50],           # d% trim
    [5, 7],                 # max_layers
    [7, 14],                # cooldown
    [13, 26],               # lifetime
    [0.20, 0.25, 0.30, 1.0], # stop_dd  (1.0 = no stop)
))
print(f"Running {len(grid)} configs × {len(WINDOWS)} = {len(grid)*len(WINDOWS)} sims...")

rows = []
for params in grid:
    tol, b, d, mx, cd, lt, sdd = params
    res_w = {}; info_w = {}
    for wname, data in slices.items():
        r = simulate(data, tol, b, d, mx, cd, lt, sdd)
        res_w[wname] = r["final"] / INITIAL
        info_w[wname] = r
    any_liq = any(info_w[k]["liq"] for k in WINDOWS)
    max_dd_any = max((-info_w[k]["max_dd"]) for k in WINDOWS)
    mults = [res_w[k] for k in ["W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL"]]
    if any(m <= 0 for m in mults):
        geo = 0; worst = 0
    else:
        geo = np.prod(mults)**(1/4); worst = min(mults)
    rows.append({
        "tol%": tol*100, "d%": d, "N": mx, "cd": cd, "lt_w": lt, "stop": sdd,
        **res_w, "geo_4w": geo, "worst_4w": worst,
        "any_liq": any_liq, "max_dd_any%": max_dd_any*100,
        "stops": sum(info_w[k]["n_stops"] for k in WINDOWS),
    })

res = pd.DataFrame(rows)

# constraint filter
safe = res[(~res.any_liq) & (res["max_dd_any%"] <= 75.0)].copy()
print(f"\nConfigs passing 'no liq' + 'maxDD≤75%': {len(safe)}/{len(res)}")

# Top by geo
safe_sorted = safe.sort_values("geo_4w", ascending=False).reset_index(drop=True)
cols = ["tol%","d%","N","cd","lt_w","stop",
        "W1-BEAR","W2-EARLYBULL","W3-MIDCHOP","W4-LATEBULL","W5-FULL",
        "geo_4w","worst_4w","max_dd_any%","stops"]

print(f"\n{'='*120}\nTOP 15 SAFE CONFIGS BY geo_4w")
print(safe_sorted[cols].head(15).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\nTOP 10 SAFE CONFIGS BY W5-FULL")
print(safe.sort_values("W5-FULL", ascending=False)[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

# stop_dd sensitivity
print(f"\n{'='*120}\nSTOP_DD SENSITIVITY (within safe subset)")
g = safe.groupby("stop")[["geo_4w","W5-FULL","worst_4w","max_dd_any%","stops"]].mean()
print(g.to_string(float_format=lambda x: f"{x:.2f}"))

# Compare: v6 winner equivalent (no stop = stop_dd=1.0) — does it still beat with stop added?
print(f"\n{'='*120}\nv6 WINNER PARAM (tol=1%, d=20, N=7, cd=7, lt=13) — STOP_DD SWEEP")
sub = res[(res["tol%"]==1.0) & (res["d%"]==20) & (res["N"]==7) & (res["cd"]==7) & (res["lt_w"]==13)]
print(sub[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))

res.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v7b_results.csv", index=False)
print(f"\nSaved: backtest_v7b_results.csv")
