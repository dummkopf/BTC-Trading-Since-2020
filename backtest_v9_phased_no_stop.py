"""
v9: Phased leverage without hard stop.

The lesson from v7b/v8: hard stops whipsaw the strategy in high-vol
bulls (PW5). The lesson from v8: phased leverage alone doesn't help
because stops kill equity before it can grow into upgraded leverage.

v9 = v8 phased leverage + REMOVE stop entirely (rely on exchange
liquidation only). At 1.5x leverage during Phase 1, the buffer to
liquidation is ~67% — survives any single drawdown short of 60%+.
Once equity doubles (Phase 2 trigger), leverage scales up because
the position has cost-basis cushion baked in.

Tested on PW5 (2018-21 high-vol) and W5 (2022-25 moderate-vol).
"""
import pandas as pd, numpy as np

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
    "PW1-BEAR":       ("2017-12-17", "2018-12-15"),
    "W1-BEAR":        ("2021-11-08", "2022-11-21"),
}

def simulate(data, lev_low, lev_mid, lev_high, T1, T2,
             tol=0.03, b=0.5, d=20, N=5, cooldown=7, lifetime_w=26):
    cash = INITIAL; pos = 0.0; avg = 0.0; layer = 0; l1_size = 0
    n_buys = n_trims = 0; n_lev_up = n_lev_down = 0
    liq = False; near = 1.0
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []; lev_curve = []
    last_lev = lev_low; peak_eq_mult = 1.0

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0.0); lev_curve.append(0); continue
        if pos > 0:
            cash -= pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg); mm_req = pos*r.Low*MM
            near = min(near, eq_low/INITIAL)
            if eq_low <= mm_req: liq=True; cash=0; pos=0; avg=0; eq_curve.append(0.0); lev_curve.append(0); continue

        equity = cash + pos*(r.Close - avg) if pos > 0 else cash
        mult = equity / INITIAL
        peak_eq_mult = max(peak_eq_mult, mult)
        # leverage decided by PEAK equity (don't downgrade on dips — sticky ratchet)
        if peak_eq_mult <= T1: cur_max_lev = lev_low
        elif peak_eq_mult <= T2: cur_max_lev = lev_mid
        else: cur_max_lev = lev_high
        if cur_max_lev > last_lev: n_lev_up += 1
        elif cur_max_lev < last_lev: n_lev_down += 1
        last_lev = cur_max_lev

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
                buys=n_buys, trims=n_trims,
                lev_ups=n_lev_up, peak_lev=lv.max() if len(lv) else 0,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)

def spot_h(data): return INITIAL * data.Close.iloc[-1]/data.Close.iloc[0]
def lev_h(data, L):
    p0 = data.Close.iloc[0]; pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq=False
    for _, r in data.iterrows():
        if liq: continue
        c -= pos*r.Close*FUNDING_DAILY
        el = c + pos*(r.Low - avg); mm_r = pos*r.Low*MM
        if el <= mm_r: liq=True; c=0;pos=0;avg=0
    return (c + pos*(data.Close.iloc[-1]-avg) if pos>0 else c), liq

# ===== CONFIGS =====
configs = [
    # name, lev_low, lev_mid, lev_high, T1, T2
    ("v9a (1.5→2→3 @ 2x/4x)",  1.5, 2.0, 3.0, 2.0, 4.0),
    ("v9b (1.5→3 @ 2x; one-step)", 1.5, 3.0, 3.0, 2.0, 2.0),
    ("v9c (1.5→3 @ 3x; conservative)", 1.5, 3.0, 3.0, 3.0, 3.0),
    ("v9d (1.0→2→3 @ 2x/4x)",  1.0, 2.0, 3.0, 2.0, 4.0),
    ("v9e (1.5→2.5 @ 2x; cap 2.5)", 1.5, 2.5, 2.5, 2.0, 2.0),
    ("v9f (1.5→3 @ 1.5x; aggressive)", 1.5, 3.0, 3.0, 1.5, 1.5),
    # baselines
    ("REF: v7b 1.5x flat no stop", 1.5, 1.5, 1.5, 999, 999),
    ("REF: v7b 2.0x flat no stop", 2.0, 2.0, 2.0, 999, 999),
    ("REF: v7b 3.0x flat no stop", 3.0, 3.0, 3.0, 999, 999),
]

print(f"\n{'='*135}\nv9 PHASED LEVERAGE — NO HARD STOP")
print(f"{'='*135}")
print(f"\nBENCHMARKS")
print(f"  {'Window':18}  {'BTC':>20}  {'Spot':>9}  {'1.5x HODL':>11}  {'2x HODL':>9}  {'3x HODL':>9}")
for name, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    sh = spot_h(data)
    h15, l15 = lev_h(data, 1.5); h2, l2 = lev_h(data, 2.0); h3, l3 = lev_h(data, 3.0)
    print(f"  {name:18}  ${data.Close.iloc[0]:>5,.0f}→${data.Close.iloc[-1]:>7,.0f}  "
          f"${sh:>8,.0f}  ${h15:>10,.0f}{'!' if l15 else ' '} ${h2:>8,.0f}{'!' if l2 else ' '} ${h3:>8,.0f}{'!' if l3 else ' '}")

print(f"\nSTRATEGY MATRIX")
print(f"  {'Config':36}  ", end='')
for wn in WINDOWS.keys():
    print(f"{wn:>13}  ", end='')
print(f"  geo(PW5,W5)")
print(f"  {'-'*36}  " + "  ".join(["-"*13]*len(WINDOWS)) + "  -----------")
for cfg in configs:
    name, ll, lm, lh, t1, t2 = cfg
    finals = {}; peak_levs = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate(data, ll, lm, lh, t1, t2)
        liq_mark = '!' if r["liq"] else ''
        finals[wn] = (r["final"], r["peak_lev"], r["max_dd"], liq_mark)
    print(f"  {name:36}  ", end='')
    for wn in WINDOWS.keys():
        f, pl, dd, lm_mark = finals[wn]
        mult = f/INITIAL
        print(f"{mult:>6.2f}x@{pl:>3.1f}{lm_mark:<2}  ", end='')
    pw5_m = finals["PW5-PREV-CYCLE"][0] / INITIAL
    w5_m = finals["W5-CURR-CYCLE"][0] / INITIAL
    if pw5_m > 0 and w5_m > 0:
        geo = (pw5_m * w5_m) ** 0.5
        print(f"  {geo:>5.2f}x")
    else:
        print(f"  LIQ")

# Detail: show leverage trajectory for v9a in PW5
print(f"\n{'='*135}\nLEVERAGE TRAJECTORY DETAIL (v9a in both cycles)")
for wn, dates in [("PW5-PREV-CYCLE", WINDOWS["PW5-PREV-CYCLE"]),
                   ("W5-CURR-CYCLE", WINDOWS["W5-CURR-CYCLE"])]:
    data = df.loc[dates[0]:dates[1]]
    r = simulate(data, 1.5, 2.0, 3.0, 2.0, 4.0)
    lv = r["lev_curve"]
    eq = r["curve"]
    # find when leverage upgrades happened
    upgrades = []
    prev = lv.iloc[0]
    for dt, l in lv.items():
        if l > prev:
            upgrades.append((dt, l, eq.loc[dt], eq.loc[dt]/INITIAL))
        prev = l
    print(f"\n  {wn}: final ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x), peak_lev {r['peak_lev']:.1f}x, MDD {r['max_dd']*100:+.1f}%")
    print(f"    leverage upgrades ({len(upgrades)} events):")
    for dt, l, e, m in upgrades:
        print(f"      {dt.date()}  → {l:.1f}x   (equity ${e:,.0f}, {m:.2f}x init)")

# save curves
data = df.loc[WINDOWS["PW5-PREV-CYCLE"][0]:WINDOWS["PW5-PREV-CYCLE"][1]]
r = simulate(data, 1.5, 2.0, 3.0, 2.0, 4.0)
out = pd.DataFrame({"date": data.index, "btc": data.Close.values,
                     "v9a_eq": r["curve"].values, "v9a_lev": r["lev_curve"].values})
out.to_csv("/home/user/BTC-Trading-Since-2020/v9_PW5_curve.csv", index=False)
data = df.loc[WINDOWS["W5-CURR-CYCLE"][0]:WINDOWS["W5-CURR-CYCLE"][1]]
r = simulate(data, 1.5, 2.0, 3.0, 2.0, 4.0)
out = pd.DataFrame({"date": data.index, "btc": data.Close.values,
                     "v9a_eq": r["curve"].values, "v9a_lev": r["lev_curve"].values})
out.to_csv("/home/user/BTC-Trading-Since-2020/v9_W5_curve.csv", index=False)
print(f"\nSaved curves.")
