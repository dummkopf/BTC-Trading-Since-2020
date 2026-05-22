"""
v11h: v9f + halving-anchored time exit.

BTC halving dates (deterministic, knowable):
  2016-07-09  →  top 2017-12-17  (17.3 mo after halving)
  2020-05-11  →  top 2021-11-08  (18.0 mo after halving)
  2024-04-20  →  top 2025-10-06  (17.6 mo after halving)
  2028-04-15  →  top 2029-10 expected (~17.5 mo)

All 3 historical post-halving tops landed within 17-18 months. This
is the deepest cycle signal we have. Anchor trim aggression and the
forced exit to MONTHS-AFTER-HALVING, not arbitrary day counters.

Schedule:
  halving + 0-12 mo:    normal (trim 20% on resistance, accumulate freely)
  halving + 12-17 mo:   medium trim 33%
  halving + 17-19 mo:   aggressive trim 60%
  halving + 19 mo:      FORCE FULL EXIT, no new buys
  halving + 24-30 mo:   wait — bear bottoming
  halving + 30+ mo:     ok to re-enter on bottom signals
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

HALVINGS = [pd.Timestamp(d) for d in
            ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20", "2028-04-15"]]

def months_since_last_halving(dt):
    past = [h for h in HALVINGS if h <= dt]
    if not past: return -999
    return (dt - past[-1]).days / 30.44

WINDOWS = {
    "PW-EXT": ("2018-09-01", "2022-06-30"),   # spans pre-halving bear + 2020 halving + 2021 top + early bear
    "W-EXT":  ("2022-09-01", "2026-05-21"),   # spans pre-halving + 2024 halving + 2025 top + early bear
}

def simulate(data, lev_low=1.5, lev_high=3.0, T_upgrade=1.5,
             tol=0.03, b=0.5, N=5, cooldown=7, lifetime_w=26,
             trim_normal=20, trim_mid=33, trim_aggr=60,
             mid_start=12, aggr_start=17, force_exit=19, blackout_end=30):
    cash = INITIAL; pos = 0.0; avg = 0.0; layer = 0; l1_size = 0
    n_buys = n_trims = n_force = 0
    liq = False; near = 1.0
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []; mhal_curve = []
    cur_max_lev = lev_low; peak_eq_mult = 1.0
    last_halving_dt = None       # which halving's cycle we're currently in
    force_exit_done_for = None   # halving date for which force exit already fired

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0.0); mhal_curve.append(0); continue
        if pos > 0:
            cash -= pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg); mm_req = pos*r.Low*MM
            near = min(near, eq_low/INITIAL)
            if eq_low <= mm_req: liq=True; cash=0; pos=0; avg=0; eq_curve.append(0.0); mhal_curve.append(0); continue

        equity = cash + pos*(r.Close - avg) if pos > 0 else cash
        peak_eq_mult = max(peak_eq_mult, equity/INITIAL)
        cur_max_lev = lev_high if peak_eq_mult > T_upgrade else lev_low

        mh = months_since_last_halving(dt)
        mhal_curve.append(mh)
        # track which halving cycle we're in
        past_h = [h for h in HALVINGS if h <= dt]
        cur_halving = past_h[-1] if past_h else None
        if cur_halving != last_halving_dt:
            # new halving — reset force-exit lockout
            last_halving_dt = cur_halving
            # don't reset force_exit_done_for; that's the safety lockout per cycle

        # Decide trim_pct by months-since-halving
        if mh >= aggr_start: trim_pct = trim_aggr
        elif mh >= mid_start: trim_pct = trim_mid
        else: trim_pct = trim_normal

        # FORCE EXIT (only ONCE per halving cycle, only within exit window)
        if pos > 0 and force_exit <= mh < blackout_end and force_exit_done_for != cur_halving:
            usd = pos * r.Close; fee = usd * FEE
            cash += pos*(r.Close - avg) - fee
            pos = 0; avg = 0; layer = 0; l1_size = 0
            n_force += 1
            force_exit_done_for = cur_halving  # lock until next halving
            support_last.clear(); resist_last.clear()
            eq_curve.append(cash); continue

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
                        btc_sell = pos * (trim_pct/100); fee = btc_sell*r.Close*FEE
                        cash += btc_sell*(r.Close - avg) - fee
                        pos -= btc_sell
                        if pos < 1e-9: pos = 0; avg = 0
                        n_trims += 1; resist_last[lvl] = dt
                        layer = 0; l1_size = 0
                        break

        # BUY (blackout from aggr_start to blackout_end)
        in_blackout = (mh >= aggr_start) and (mh < blackout_end)
        if not in_blackout and layer < N and not liq:
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
        eq_curve.append(eq)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(curve=s, final=final, liq=liq, near=near,
                buys=n_buys, trims=n_trims, force=n_force,
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

# ===== RUN =====
print(f"\n{'='*135}\nv11h HALVING-ANCHORED CYCLE-TIME STRATEGY")
print(f"{'='*135}")

print(f"\nBENCHMARKS")
for name, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    sh = spot_h(data); h15,l15 = lev_h(data, 1.5); h3,l3 = lev_h(data, 3.0)
    print(f"  {name}: BTC ${data.Close.iloc[0]:,.0f}→${data.Close.iloc[-1]:,.0f} ({data.Close.iloc[-1]/data.Close.iloc[0]-1:+.0%})  "
          f"spot ${sh:,.0f}  1.5xH ${h15:,.0f}{'!' if l15 else ''}  3xH ${h3:,.0f}{'!' if l3 else ''}")

print(f"\nv9f reference (no time exit):")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    r = simulate(data, trim_normal=20, trim_mid=20, trim_aggr=20, mid_start=999, aggr_start=999, force_exit=999)
    print(f"  {wn}: ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x)  MDD {r['max_dd']*100:+.1f}%")

# variants
configs = [
    ("v11h-a base   (mid12, aggr17, exit19, blackout24)", 20, 33, 60, 12, 17, 19, 24),
    ("v11h-b early  (mid10, aggr15, exit18, blackout30)", 20, 33, 60, 10, 15, 18, 30),
    ("v11h-c late   (mid14, aggr18, exit20, blackout30)", 20, 33, 60, 14, 18, 20, 30),
    ("v11h-d aggr   (mid12, aggr16, exit18, blackout30)", 20, 50, 80, 12, 16, 18, 30),
    ("v11h-e soft   (mid13, aggr18, exit20, blackout30)", 20, 25, 40, 13, 18, 20, 30),
]

print(f"\n  {'Config':54}  ", end='')
for wn in WINDOWS.keys():
    print(f"{wn:>14}  ", end='')
print(f"  geo  trades")
print(f"  {'-'*54}  " + "  ".join(["-"*14]*len(WINDOWS)) + "  -----  ------")

for cfg in configs:
    name, tn, tm, ta, ms, asr, fe, bo = cfg
    finals = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate(data, trim_normal=tn, trim_mid=tm, trim_aggr=ta,
                     mid_start=ms, aggr_start=asr, force_exit=fe, blackout_end=bo)
        finals[wn] = r
    pw = finals["PW-EXT"]["final"]/INITIAL
    w  = finals["W-EXT"]["final"]/INITIAL
    geo = (pw*w)**0.5 if pw>0 and w>0 else 0
    total = sum(finals[k]["buys"]+finals[k]["trims"]+finals[k]["force"] for k in WINDOWS)
    print(f"  {name:54}  ", end='')
    for wn in WINDOWS.keys():
        f = finals[wn]
        print(f"{f['final']/INITIAL:>8.2f}x{'!' if f['liq'] else ' ':<2}(f={f['force']})  ", end='')
    print(f"  {geo:>5.2f}x  {total:>3}")

# detail trajectory of best
print(f"\n{'='*135}\nv11h-a TRAJECTORY DETAIL")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    r = simulate(data, trim_normal=20, trim_mid=33, trim_aggr=60,
                  mid_start=12, aggr_start=17, force_exit=19, blackout_end=24)
    halving_in_window = [h for h in HALVINGS if data.index[0] <= h <= data.index[-1]]
    print(f"\n  {wn}: final ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x)  MDD {r['max_dd']*100:+.1f}%  buys={r['buys']} trims={r['trims']} force_exits={r['force']}")
    if halving_in_window:
        h = halving_in_window[0]
        idx0 = data.index.get_indexer([h], method='nearest')[0]
        print(f"    Halving:        {data.index[idx0].date()}  BTC ${data.iloc[idx0].Close:,.0f}")
        for mo, label in [(12, "mid trim start"), (17, "aggr trim start"), (19, "force exit"), (24, "blackout end")]:
            target = h + pd.Timedelta(days=mo*30.44)
            if target <= data.index[-1]:
                idx = data.index.get_indexer([target], method="nearest")[0]
                print(f"    Halving+{mo:>2}mo: {data.index[idx].date()}  BTC ${data.iloc[idx].Close:,.0f}  ({label})")
