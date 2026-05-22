"""
Compare v4 mechanical vs v7b balanced on the PREVIOUS BTC cycle (2017-2021).

Previous cycle analog windows (mirror current-cycle W1-W5):
  PW1-BEAR        2017-12-17 → 2018-12-15  ($19,498 → $3,200, -84%)
  PW2-EARLYBULL   2018-12-15 → 2021-04-14  ($3,200 → $63,558, +1886%)
  PW3-MIDCHOP     2021-04-14 → 2021-07-20  ($63,558 → $29,807, -53%)
  PW4-LATEBULL    2021-07-20 → 2021-11-08  ($29,807 → $67,567, +127%)
  PW5-FULL        2018-09-01 → 2021-08-31  (3-year analog to 2022-09 → 2025-08)

Strategies:
  v4 mechanical:  a=3% drop, 1.b=1.5×, c=40% trim trigger, d=20%, N=5
                  NO stop, NO signal gates
  v7b balanced:   tol=3% from swing S/R, 1.b=1.5×, d=20%, N=5,
                  cooldown=7d, lifetime=26w, stop_dd=0.20
"""
import pandas as pd, numpy as np

OHLC = "/tmp/btc_long.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")
print(f"Data range: {df.index.min().date()} → {df.index.max().date()} ({len(df)} bars)")

INITIAL = 7000.0
FUNDING_DAILY = 0.0001 * 3
FEE = 0.0005
MAX_LEV = 3.0
MM = 0.005

WINDOWS = {
    "PW1-BEAR":      ("2017-12-17", "2018-12-15"),
    "PW2-EARLYBULL": ("2018-12-15", "2021-04-14"),
    "PW3-MIDCHOP":   ("2021-04-14", "2021-07-20"),
    "PW4-LATEBULL":  ("2021-07-20", "2021-11-08"),
    "PW5-FULL":      ("2018-09-01", "2021-08-31"),
}

# ======== v4 mechanical ========
def v4_mechanical(data, a=3, b=0.5, c=40, d=20, max_layers=5):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0; last_buy_price = None
    realized_t = funding_t = fees_t = 0.0
    n_buys = n_trims = 0
    near_liq = 1.0; liq = False
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

        # trim
        if pos > 0 and r.Close >= avg * (1 + c/100):
            btc_sell = pos * (d/100)
            fee = btc_sell * r.Close * FEE
            realized = btc_sell * (r.Close - avg) - fee
            cash += realized
            realized_t += realized; fees_t += fee
            pos -= btc_sell
            if pos < 1e-9: pos = 0; avg = 0
            n_trims += 1; layer = 0; last_buy_price = r.Close; l1_size = 0
            eq_curve.append(cash + pos*(r.Close - avg) if pos > 0 else cash); continue

        # buy
        equity = cash + pos * (r.Close - avg) if pos > 0 else cash
        if equity > 0:
            if layer == 0:
                if last_buy_price is None or r.Close <= last_buy_price * (1 - a/100):
                    geo_sum = sum((1+b)**k for k in range(max_layers))
                    l1_size = equity * MAX_LEV / geo_sum
                    size = min(l1_size, max(0, MAX_LEV*equity - pos*r.Close))
                    if size > 1:
                        btc = size / r.Close
                        fee = size * FEE; cash -= fee; fees_t += fee
                        new_pos = pos + btc
                        avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                        pos = new_pos
                        layer = 1; n_buys += 1; last_buy_price = r.Close
            elif layer < max_layers:
                if r.Close <= last_buy_price * (1 - a/100):
                    size = l1_size * (1+b)**layer
                    size = min(size, max(0, MAX_LEV*equity - pos*r.Close))
                    if size > 1:
                        btc = size / r.Close
                        fee = size * FEE; cash -= fee; fees_t += fee
                        new_pos = pos + btc
                        avg = (pos*avg + btc*r.Close)/new_pos
                        pos = new_pos
                        layer += 1; n_buys += 1; last_buy_price = r.Close
        eq_curve.append(cash + pos*(r.Close - avg) if pos > 0 else cash)

    s = pd.Series(eq_curve, index=data.index)
    return dict(curve=s, final=s.iloc[-1], liq=liq, near_liq=near_liq,
                n_buys=n_buys, n_trims=n_trims, n_stops=0,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0,
                realized=realized_t, funding=funding_t)

# ======== v7b balanced ========
def detect_swings(weekly_bars, w):
    highs, lows = [], []
    for i in range(w, len(weekly_bars) - w):
        wnd = weekly_bars.iloc[i-w:i+w+1]
        if weekly_bars.iloc[i].High == wnd.High.max():
            highs.append((weekly_bars.index[i+w], weekly_bars.iloc[i].High))
        if weekly_bars.iloc[i].Low == wnd.Low.min():
            lows.append((weekly_bars.index[i+w], weekly_bars.iloc[i].Low))
    return pd.DataFrame(highs, columns=["confirm","price"]), pd.DataFrame(lows, columns=["confirm","price"])

# Pre-compute swings on the full dataset
weekly = df.resample("W-MON").agg(Open=("Open","first"), High=("High","max"),
                                    Low=("Low","min"), Close=("Close","last")).dropna()
SH, SL = detect_swings(weekly, w=2)
print(f"Swing detection: {len(SH)} highs, {len(SL)} lows over full history")

def v7b_balanced(data, tol=0.03, b=0.5, d=20, max_layers=5,
                 cooldown=7, lifetime_w=26, stop_dd=0.20):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; l1_size = 0
    realized_t = funding_t = fees_t = 0.0
    n_buys = n_trims = n_stops = 0
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

        # hard stop
        if pos > 0 and avg > 0:
            adverse = (avg - r.Close) / avg
            if adverse >= stop_dd:
                btc_sell = pos
                fee = btc_sell * r.Close * FEE
                realized = btc_sell * (r.Close - avg) - fee
                cash += realized
                realized_t += realized; fees_t += fee
                pos = 0; avg = 0
                n_stops += 1; layer = 0; l1_size = 0
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash); continue

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
                        fee = btc_sell * r.Close * FEE
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
                                l1_size = equity * MAX_LEV / geo_sum
                                size = l1_size
                            else:
                                size = l1_size * (1+b)**layer
                            size = min(size, max(0, MAX_LEV*equity - pos*r.Close))
                            if size > 1:
                                btc = size / r.Close
                                fee = size * FEE; cash -= fee; fees_t += fee
                                new_pos = pos + btc
                                avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                                pos = new_pos
                                layer += 1; n_buys += 1
                                support_last[lvl] = dt
                            break

        eq_curve.append(cash + pos*(r.Close - avg) if pos > 0 else cash)

    s = pd.Series(eq_curve, index=data.index)
    return dict(curve=s, final=s.iloc[-1], liq=liq, near_liq=near_liq,
                n_buys=n_buys, n_trims=n_trims, n_stops=n_stops,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0,
                realized=realized_t, funding=funding_t)

# ======== HODL benchmarks ========
def spot_hodl(data):
    p0 = data.Close.iloc[0]
    return INITIAL * data.Close.iloc[-1] / p0

def lev_hodl(data, L):
    p0 = data.Close.iloc[0]; pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq=False
    near = 1.0
    for _, r in data.iterrows():
        if liq: continue
        c -= pos * r.Close * FUNDING_DAILY
        el = c + pos*(r.Low - avg); mm_r = pos*r.Low*MM
        near = min(near, el/INITIAL)
        if el <= mm_r: liq=True; c=0;pos=0;avg=0
    return (c + pos*(data.Close.iloc[-1]-avg) if pos > 0 else c), liq, near

# ============ RUN ============
print(f"\n{'='*125}")
print(f"PREVIOUS-CYCLE COMPARISON  (v4 vs v7b vs HODL benchmarks)")
print(f"{'='*125}")
print(f"  Strategy params (frozen from earlier optimization):")
print(f"    v4 mechanical: a=3%, 1.b=1.5×, c=40%, d=20%, N=5,  NO stop, NO gates")
print(f"    v7b balanced:  tol=3%, 1.b=1.5×, d=20%, N=5,  cooldown=7d, lifetime=26w, stop_dd=20%")
print(f"")
print(f"  {'Window':14}  {'BTC':>23}  {'Spot':>10}  {'2x HODL':>10}  {'3x HODL':>10}  {'v4 mech':>11}  {'v7b balcd':>11}")
results = {}
for name, (s, e) in WINDOWS.items():
    data = df.loc[s:e].copy()
    if len(data) < 30: continue
    spot = spot_hodl(data)
    h2, l2, _ = lev_hodl(data, 2.0)
    h3, l3, _ = lev_hodl(data, 3.0)
    v4 = v4_mechanical(data)
    v7b = v7b_balanced(data)
    results[name] = dict(spot=spot, h2=h2, h3=h3, v4=v4, v7b=v7b, data=data)
    print(f"  {name:14}  ${data.Close.iloc[0]:>5,.0f}→${data.Close.iloc[-1]:>7,.0f}  ${spot:>9,.0f}  "
          f"${h2:>9,.0f}{'!' if l2 else ' '} ${h3:>9,.0f}{'!' if l3 else ' '} "
          f"${v4['final']:>10,.0f}{'!' if v4['liq'] else ' '} ${v7b['final']:>10,.0f}{'!' if v7b['liq'] else ' '}")

# multiples
print(f"\n  {'Window':14}  {'BTC return':>10}  {'Spot':>7}  {'2x':>7}  {'3x':>7}  {'v4 mech':>9}  {'v7b balcd':>10}")
for name, r in results.items():
    btc_ret = r["data"].Close.iloc[-1] / r["data"].Close.iloc[0]
    print(f"  {name:14}  {btc_ret:>9.2f}x  {r['spot']/INITIAL:>6.2f}x  "
          f"{r['h2']/INITIAL:>6.2f}x  {r['h3']/INITIAL:>6.2f}x  "
          f"{r['v4']['final']/INITIAL:>8.2f}x  {r['v7b']['final']/INITIAL:>9.2f}x")

# detail trades for PW5-FULL
print(f"\n{'='*125}")
print(f"PW5-FULL DETAILS  ($7000 → final)")
print(f"{'='*125}")
for label, info in [("v4 mechanical", results["PW5-FULL"]["v4"]),
                    ("v7b balanced", results["PW5-FULL"]["v7b"])]:
    print(f"\n  {label}:")
    print(f"    final ${info['final']:,.0f}   max DD {info['max_dd']*100:+.1f}%   near-liq {info['near_liq']*100:.1f}%")
    print(f"    buys {info['n_buys']}, trims {info['n_trims']}, stops {info['n_stops']}")
    print(f"    realized ${info['realized']:,.0f}, funding ${info['funding']:,.0f}, liquidated={info['liq']}")

# Save PW5 equity curves
out = pd.DataFrame({
    "date": results["PW5-FULL"]["data"].index,
    "btc": results["PW5-FULL"]["data"].Close.values,
    "v4_mechanical": results["PW5-FULL"]["v4"]["curve"].values,
    "v7b_balanced":  results["PW5-FULL"]["v7b"]["curve"].values,
})
out.to_csv("/home/user/BTC-Trading-Since-2020/prev_cycle_comparison.csv", index=False)
print(f"\nSaved: prev_cycle_comparison.csv")
