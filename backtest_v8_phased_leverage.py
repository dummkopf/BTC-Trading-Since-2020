"""
v8: Phased dynamic leverage.

Leverage scales with cumulative equity multiple — the position
"earns" its leverage from realized + unrealized profit, not from
initial principal:

  equity / initial ≤ T1  → max_lev = L_low   (build phase)
  T1 < equity/init ≤ T2  → max_lev = L_mid   (confirmation phase)
  equity / initial > T2  → max_lev = L_high  (ride phase)

Everything else from v7b balanced:
  S/R triggered entries (tol=3%, swing_w=2, lifetime=26w, cooldown=7d)
  Geometric layers (1.b=1.5x, N=5)
  Trim 20% on resistance touch
  Hard stop at -25% from avg_cost
"""
import pandas as pd, numpy as np, itertools

df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
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
FUNDING_DAILY = 0.0001*3; FEE = 0.0005; MM = 0.005

WINDOWS = {
    "PW5-PREV-CYCLE": ("2018-09-01", "2021-08-31"),
    "W5-CURR-CYCLE":  ("2022-09-01", "2025-08-31"),
}

def simulate(data, lev_low, lev_mid, lev_high, T1, T2,
             tol=0.03, b=0.5, d=20, N=5, cooldown=7, lifetime_w=26, stop_dd=0.25):
    cash = INITIAL; pos = 0.0; avg = 0.0; layer = 0; l1_size = 0
    n_buys = n_trims = n_stops = n_lev_up = n_lev_down = 0
    liq = False; near = 1.0
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []; lev_curve = []
    cur_max_lev = lev_low
    last_lev = lev_low

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0.0); lev_curve.append(0); continue
        if pos > 0:
            cash -= pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg); mm_req = pos*r.Low*MM
            near = min(near, eq_low/INITIAL)
            if eq_low <= mm_req: liq=True; cash=0; pos=0; avg=0; eq_curve.append(0.0); lev_curve.append(0); continue

        # hard stop
        if pos > 0 and avg > 0:
            adverse = (avg - r.Close) / avg
            if adverse >= stop_dd:
                fee = pos * r.Close * FEE
                cash += pos*(r.Close - avg) - fee
                pos = 0; avg = 0; n_stops += 1; layer = 0; l1_size = 0
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash); lev_curve.append(0); continue

        # update dynamic leverage based on current equity multiple
        equity = cash + pos*(r.Close - avg) if pos > 0 else cash
        mult = equity / INITIAL
        if mult <= T1: cur_max_lev = lev_low
        elif mult <= T2: cur_max_lev = lev_mid
        else: cur_max_lev = lev_high
        if cur_max_lev > last_lev: n_lev_up += 1
        elif cur_max_lev < last_lev: n_lev_down += 1
        last_lev = cur_max_lev

        # S/R levels
        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values

        # TRIM
        if pos > 0 and r.Close > avg:
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = resist_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        btc_sell = pos * (d/100); fee = btc_sell*r.Close*FEE
                        cash += btc_sell*(r.Close - avg) - fee
                        pos -= btc_sell
                        if pos < 1e-9: pos = 0; avg = 0
                        n_trims += 1; resist_last[lvl] = dt
                        layer = 0; l1_size = 0
                        break

        # BUY
        if layer < N and not liq:
            if equity > 0:
                for lvl in sups:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = support_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            if layer == 0:
                                geo_sum = sum((1+b)**k for k in range(N))
                                l1_size = equity * cur_max_lev / geo_sum
                                size = l1_size
                            else:
                                size = l1_size * (1+b)**layer
                            size = min(size, max(0, cur_max_lev*equity - pos*r.Close))
                            if size > 1:
                                btc = size / r.Close; fee = size*FEE; cash -= fee
                                new_pos = pos + btc
                                avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                                pos = new_pos
                                layer += 1; n_buys += 1
                                support_last[lvl] = dt
                            break

        eq = cash + pos*(r.Close - avg) if pos > 0 else cash
        eq_curve.append(eq); lev_curve.append(cur_max_lev)

    s = pd.Series(eq_curve, index=data.index)
    lv = pd.Series(lev_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(curve=s, lev_curve=lv, final=final, liq=liq, near=near,
                buys=n_buys, trims=n_trims, stops=n_stops,
                lev_ups=n_lev_up, lev_downs=n_lev_down,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)

# fixed-leverage v7b for comparison (re-implement here as same code path with all 3 lev_X equal)
def v7b_fixed(data, lev, stop_dd):
    return simulate(data, lev, lev, lev, 999, 999, stop_dd=stop_dd)

def spot_h(data): return INITIAL * data.Close.iloc[-1]/data.Close.iloc[0]
def lev_h(data, L):
    p0 = data.Close.iloc[0]; pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq=False
    for _, r in data.iterrows():
        if liq: continue
        c -= pos*r.Close*FUNDING_DAILY
        el = c + pos*(r.Low - avg); mm_r = pos*r.Low*MM
        if el <= mm_r: liq=True; c=0;pos=0;avg=0
    return (c + pos*(data.Close.iloc[-1]-avg) if pos>0 else c), liq

# Configs to test
configs = [
    # (name, lev_low, lev_mid, lev_high, T1, T2, stop_dd)
    ("v8a (1.5→2→3 @ 1.5/3x)",  1.5, 2.0, 3.0, 1.5, 3.0, 0.25),
    ("v8b (1.5→2→3 @ 2/4x)",    1.5, 2.0, 3.0, 2.0, 4.0, 0.25),
    ("v8c (1.0→2→3 @ 2/4x)",    1.0, 2.0, 3.0, 2.0, 4.0, 0.25),
    ("v8d (1.5→2.5→3 @ 2/5x)",  1.5, 2.5, 3.0, 2.0, 5.0, 0.25),
    ("v8e (1.0→1.5→3 @ 1.5/4x)",1.0, 1.5, 3.0, 1.5, 4.0, 0.25),
    ("v8f (slow: 1.0→2→3 @ 3/6x)", 1.0, 2.0, 3.0, 3.0, 6.0, 0.25),
    # benchmarks
    ("v7b 1.5x flat (no stop)", None, None, None, None, None, 1.0),
    ("v7b 1.5x flat 25% stop",  None, None, None, None, None, 0.25),
    ("v7b 3.0x flat 25% stop",  None, None, None, None, None, 0.25),
]

print(f"\n{'='*125}\nv8 PHASED LEVERAGE — TWO-CYCLE COMPARISON")
print(f"  PW5 (2018-09→2021-08, BTC +556%): high vol, COVID -64% mid-cycle")
print(f"  W5  (2022-09→2025-08, BTC +438%): moderate vol, max -28% mid-cycle")
print(f"{'='*125}")

print(f"\n  Benchmarks:")
for name, win, dates in [("PW5", "PW5-PREV-CYCLE", WINDOWS["PW5-PREV-CYCLE"]),
                          ("W5",  "W5-CURR-CYCLE",  WINDOWS["W5-CURR-CYCLE"])]:
    data = df.loc[dates[0]:dates[1]]
    sh = spot_h(data)
    h15, l15 = lev_h(data, 1.5)
    h2, l2 = lev_h(data, 2.0)
    h3, l3 = lev_h(data, 3.0)
    print(f"    {name}: Spot {sh/INITIAL:.2f}x | 1.5x HODL {h15/INITIAL:.2f}x{'!' if l15 else ''} | "
          f"2x HODL {h2/INITIAL:.2f}x{'!' if l2 else ''} | 3x HODL {h3/INITIAL:.2f}x{'!' if l3 else ''}")

print(f"\n  {'Strategy':32}  {'PW5 final':>11}  {'PW5 mult':>8}  {'PW5 MDD':>8}  {'PW5 stops':>9}  | "
      f"{'W5 final':>10}  {'W5 mult':>8}  {'W5 MDD':>8}  {'W5 stops':>9}")
print(f"  {'-'*32}  {'-'*11}  {'-'*8}  {'-'*8}  {'-'*9}  | {'-'*10}  {'-'*8}  {'-'*8}  {'-'*9}")

results_table = []
for cfg in configs:
    name, ll, lm, lh, t1, t2, sdd = cfg
    res_pw5 = None; res_w5 = None
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        if "v7b" in name:
            if "1.5x" in name: lev = 1.5
            elif "2x" in name: lev = 2.0
            else: lev = 3.0
            r = v7b_fixed(data, lev, sdd)
        else:
            r = simulate(data, ll, lm, lh, t1, t2, stop_dd=sdd)
        if wn == "PW5-PREV-CYCLE": res_pw5 = r
        else: res_w5 = r
    liq_pw5_str = '!' if res_pw5["liq"] else ' '
    liq_w5_str = '!' if res_w5["liq"] else ' '
    print(f"  {name:32}  ${res_pw5['final']:>9,.0f}{liq_pw5_str}  {res_pw5['final']/INITIAL:>7.2f}x  "
          f"{res_pw5['max_dd']*100:>+7.1f}%  {res_pw5['stops']:>9}  | "
          f"${res_w5['final']:>8,.0f}{liq_w5_str}  {res_w5['final']/INITIAL:>7.2f}x  "
          f"{res_w5['max_dd']*100:>+7.1f}%  {res_w5['stops']:>9}")
    results_table.append((name, res_pw5, res_w5))

# Detail the best v8 — pick one that survives both with highest combined score
print(f"\n{'='*125}\nDETAIL: BEST v8 CONFIG (geometric mean across both cycles)")
v8s = [(name, p, w) for name, p, w in results_table if "v8" in name and not p["liq"] and not w["liq"]]
v8s.sort(key=lambda x: -np.sqrt(x[1]["final"]/INITIAL * x[2]["final"]/INITIAL))
for name, p, w in v8s[:3]:
    geo = np.sqrt(p["final"]/INITIAL * w["final"]/INITIAL)
    print(f"  {name}: geo_mean {geo:.2f}x   PW5 {p['final']/INITIAL:.2f}x   W5 {w['final']/INITIAL:.2f}x   "
          f"PW5_MDD {p['max_dd']*100:+.1f}%   W5_MDD {w['max_dd']*100:+.1f}%   "
          f"lev_changes: PW5 {p['lev_ups']}↑/{p['lev_downs']}↓, W5 {w['lev_ups']}↑/{w['lev_downs']}↓")

# Save winner curve
if v8s:
    winner_name, p, w = v8s[0]
    out = pd.DataFrame({
        "date": p["curve"].index,
        "pw5_btc": df.loc[WINDOWS["PW5-PREV-CYCLE"][0]:WINDOWS["PW5-PREV-CYCLE"][1]].Close.values,
        "pw5_strategy": p["curve"].values,
        "pw5_lev": p["lev_curve"].values,
    })
    out.to_csv("/home/user/BTC-Trading-Since-2020/v8_winner_PW5.csv", index=False)

    out2 = pd.DataFrame({
        "date": w["curve"].index,
        "w5_btc": df.loc[WINDOWS["W5-CURR-CYCLE"][0]:WINDOWS["W5-CURR-CYCLE"][1]].Close.values,
        "w5_strategy": w["curve"].values,
        "w5_lev": w["lev_curve"].values,
    })
    out2.to_csv("/home/user/BTC-Trading-Since-2020/v8_winner_W5.csv", index=False)
    print(f"\n  Saved curves: v8_winner_PW5.csv  v8_winner_W5.csv")
