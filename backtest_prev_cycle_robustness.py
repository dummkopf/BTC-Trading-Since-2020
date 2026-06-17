"""
Stress-test v7b on previous cycle with different stop / leverage settings
to see what parameters survive the high-vol 2018-2021 regime.
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

INITIAL = 7000.0; FUNDING_DAILY = 0.0001*3; FEE = 0.0005; MM = 0.005

def v7b(data, max_lev, stop_dd, tol=0.03, b=0.5, d=20, max_layers=5,
        cooldown=7, lifetime_w=26):
    cash = INITIAL; pos = 0.0; avg = 0.0; layer = 0; l1_size = 0
    n_buys=n_trims=n_stops=0; liq=False; near=1.0
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []
    for i, (dt, r) in enumerate(data.iterrows()):
        if liq: eq_curve.append(0.0); continue
        if pos > 0:
            cash -= pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg); mm_req = pos*r.Low*MM
            near = min(near, eq_low/INITIAL)
            if eq_low <= mm_req: liq=True; cash=0; pos=0; avg=0; eq_curve.append(0.0); continue
        if pos > 0 and avg > 0 and stop_dd < 1.0:
            adverse = (avg - r.Close) / avg
            if adverse >= stop_dd:
                fee = pos * r.Close * FEE
                cash += pos*(r.Close - avg) - fee
                pos = 0; avg = 0; n_stops += 1; layer = 0; l1_size = 0
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash); continue
        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values
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
        if layer < max_layers and not liq:
            equity = cash + pos*(r.Close - avg) if pos > 0 else cash
            if equity > 0:
                for lvl in sups:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = support_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            if layer == 0:
                                geo_sum = sum((1+b)**k for k in range(max_layers))
                                l1_size = equity * max_lev / geo_sum
                                size = l1_size
                            else:
                                size = l1_size * (1+b)**layer
                            size = min(size, max(0, max_lev*equity - pos*r.Close))
                            if size > 1:
                                btc = size / r.Close; fee = size*FEE; cash -= fee
                                new_pos = pos + btc
                                avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                                pos = new_pos
                                layer += 1; n_buys += 1
                                support_last[lvl] = dt
                            break
        eq_curve.append(cash + pos*(r.Close - avg) if pos > 0 else cash)
    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1]
    if liq: final = 0
    return final, s, liq, n_buys, n_trims, n_stops, near

PW5 = df.loc["2018-09-01":"2021-08-31"]
W5  = df.loc["2022-09-01":"2025-08-31"]

print(f"\n{'='*120}")
print(f"v7b STRESS TEST: vary leverage × stop_dd")
print(f"  Previous cycle PW5 (2018-09 → 2021-08): BTC +{PW5.Close.iloc[-1]/PW5.Close.iloc[0]-1:.0%}")
print(f"  Current  cycle W5  (2022-09 → 2025-08): BTC +{W5.Close.iloc[-1]/W5.Close.iloc[0]-1:.0%}")
print(f"{'='*120}")
print(f"\n{'lev':>3} {'stop':>5} | {'PW5 final':>10} {'PW5 mult':>8} {'liq':>3} | {'W5 final':>10} {'W5 mult':>8} {'liq':>3}  buys/trims/stops")
for lev in [1.5, 2.0, 3.0]:
    for sdd in [0.20, 0.30, 0.40, 0.50, 1.0]:
        fp, _, lp, npb, npt, nps, _ = v7b(PW5, lev, sdd)
        fw, _, lw, nwb, nwt, nws, _ = v7b(W5, lev, sdd)
        label = "no" if sdd == 1.0 else f"{sdd:.0%}"
        print(f"  {lev:>3.1f} {label:>5} | ${fp:>8,.0f} {fp/INITIAL:>7.2f}x  {'!' if lp else ' ':>3} | ${fw:>8,.0f} {fw/INITIAL:>7.2f}x  {'!' if lw else ' ':>3}  "
              f"PW5: {npb}/{npt}/{nps}  W5: {nwb}/{nwt}/{nws}")

# benchmarks for context
print(f"\nReference: spot HODL  PW5 = {PW5.Close.iloc[-1]/PW5.Close.iloc[0]:.2f}x   W5 = {W5.Close.iloc[-1]/W5.Close.iloc[0]:.2f}x")
