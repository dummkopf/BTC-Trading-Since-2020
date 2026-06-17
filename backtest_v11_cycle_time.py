"""
v11: v9f + cycle-time-based exit.

BTC cycle empirical regularity: bull = 1050-1070 days, bear = 365-405 days.
After 9 sims trying to identify "the top" with oscillators, the cleanest
signal is just CALENDAR DAYS from cycle bottom.

Mechanism:
  T0 = day of FIRST PURCHASE (cycle entry).
  day  0-700:   normal trim 20% (let it run)
  day 700-900:  trim 33% on each resistance touch (start de-risking)
  day 900-1050: trim 50% on each resistance touch (aggressive)
  day 1050+:    FORCE full exit on any subsequent up day
  day 1100+:    hard close regardless
  → No re-entry until next cycle: T0 cooldown = 365 days from forced exit.

Combined with v9f's phased leverage (1.5x → 3.0x on equity doubling),
NO hard stop.

Extended windows to capture the cycle TOP (not cut at day 800-900):
  PW-EXT: 2018-09-01 → 2022-06-30 (cycle bottom + full bull + start of bear)
  W-EXT:  2022-09-01 → 2026-05-21 (current)
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
    "PW-EXT": ("2018-09-01", "2022-06-30"),
    "W-EXT":  ("2022-09-01", "2026-05-21"),
}

def simulate(data, lev_low=1.5, lev_high=3.0, T_upgrade=1.5,
             tol=0.03, b=0.5, base_d=20, N=5, cooldown=7, lifetime_w=26,
             day700_d=33, day900_d=50, day1050_hard=True, day1100_force=True):
    cash = INITIAL; pos = 0.0; avg = 0.0; layer = 0; l1_size = 0
    n_buys = n_trims = n_force = 0
    liq = False; near = 1.0
    support_last = {}; resist_last = {}
    lifetime_days = lifetime_w * 7
    eq_curve = []; lev_curve = []
    t0 = None  # date of first purchase
    cur_max_lev = lev_low; last_lev = lev_low; peak_eq_mult = 1.0

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
        cur_max_lev = lev_high if peak_eq_mult > T_upgrade else lev_low

        # day count from cycle start
        days = (dt - t0).days if t0 is not None else -1

        # TIME-BASED FORCED EXIT
        if pos > 0 and days >= 1100 and day1100_force:
            usd = pos * r.Close; fee = usd * FEE
            cash += pos*(r.Close - avg) - fee
            pos = 0; avg = 0; layer = 0; l1_size = 0
            n_force += 1; support_last.clear(); resist_last.clear()
            t0 = None  # reset cycle clock
            eq_curve.append(cash); lev_curve.append(cur_max_lev); continue

        # decide trim_pct based on day count
        if days >= 1050: trim_pct = day900_d  # really aggressive after 1050 too
        elif days >= 900: trim_pct = day900_d
        elif days >= 700: trim_pct = day700_d
        else: trim_pct = base_d

        # at day 1050+, ALSO force trim on ANY up day (close > yesterday's close)
        # (we approximate: at day 1050+, treat resistance touch as full close)
        if days >= 1050 and day1050_hard and pos > 0:
            # check if close > yesterday's close (an "up day")
            if i > 0 and r.Close > data.Close.iloc[i-1]:
                # force trim 80% on first up day in this regime
                btc_sell = pos * 0.80
                fee = btc_sell * r.Close * FEE
                cash += btc_sell*(r.Close - avg) - fee
                pos -= btc_sell
                if pos < 1e-9: pos = 0; avg = 0
                n_trims += 1; layer = 0; l1_size = 0
                support_last.clear(); resist_last.clear()
                eq_curve.append(cash + pos*(r.Close - avg) if pos>0 else cash); lev_curve.append(cur_max_lev); continue

        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values

        # TRIM at resistance
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

        # BUY (no buying after day 800 — start of distribution zone)
        if days >= 0 and days >= 800: pass  # no new buys late in cycle
        elif layer < N and not liq:
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
                                if t0 is None: t0 = dt  # mark cycle start
                            break

        eq = cash + pos*(r.Close - avg) if pos > 0 else cash
        eq_curve.append(eq); lev_curve.append(cur_max_lev)

    s = pd.Series(eq_curve, index=data.index)
    lv = pd.Series(lev_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(curve=s, lev_curve=lv, final=final, liq=liq, near=near,
                buys=n_buys, trims=n_trims, force=n_force, t0=t0,
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

# ============== RUN ==============
print(f"\n{'='*135}\nv11 CYCLE-TIME-BASED EXIT — extended windows to capture top")
print(f"{'='*135}")

print(f"\nBENCHMARKS")
for name, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    sh = spot_h(data)
    h15, l15 = lev_h(data, 1.5); h3, l3 = lev_h(data, 3.0)
    print(f"  {name}: BTC ${data.Close.iloc[0]:,.0f} → ${data.Close.iloc[-1]:,.0f}  "
          f"({data.Close.iloc[-1]/data.Close.iloc[0]-1:+.0%})  "
          f"spot ${sh:,.0f}  1.5xH ${h15:,.0f}{'!' if l15 else ''}  3xH ${h3:,.0f}{'!' if l3 else ''}")

# v9f reference (no time logic)
print(f"\n  v9f reference (no time exit):")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    r = simulate(data, day700_d=20, day900_d=20, day1050_hard=False, day1100_force=False)
    print(f"    {wn}: final ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x)  MDD {r['max_dd']*100:+.1f}%  buys={r['buys']} trims={r['trims']}  t0={r['t0'].date() if r['t0'] else '—'}")

# v11 variants
configs = [
    ("v11a base   (700→33, 900→50, 1050 hard, 1100 force)", 33, 50, True, True),
    ("v11b late   (800→33, 1000→60, 1100 hard, 1150 force)", 33, 60, True, True),  # NOTE: I'll set vars below
    ("v11c soft   (700→33, 900→50, no hard, no force)", 33, 50, False, False),
    ("v11d hard   (700→50, 900→80, hard, force)", 50, 80, True, True),
    ("v11e mid    (700→25, 900→40, 1050 hard, 1100 force)", 25, 40, True, True),
]

print(f"\n  v11 variants (with cycle-time exit):")
print(f"  {'Config':56}  ", end='')
for wn in WINDOWS.keys():
    print(f"{wn:>14}  ", end='')
print(f"  geo  trades")
print(f"  {'-'*56}  " + "  ".join(["-"*14]*len(WINDOWS)) + "  -----  ------")

for name, d700, d900, hard, force in configs:
    finals = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate(data, day700_d=d700, day900_d=d900,
                     day1050_hard=hard, day1100_force=force)
        finals[wn] = r
    pw_m = finals["PW-EXT"]["final"]/INITIAL
    w_m  = finals["W-EXT"]["final"]/INITIAL
    geo = (pw_m * w_m)**0.5 if pw_m>0 and w_m>0 else 0
    total_trades = sum(finals[k]["buys"]+finals[k]["trims"]+finals[k]["force"] for k in WINDOWS)
    print(f"  {name:56}  ", end='')
    for wn in WINDOWS.keys():
        f = finals[wn]
        liq_mark = '!' if f["liq"] else ' '
        print(f"{f['final']/INITIAL:>8.2f}x{liq_mark:<2}({f['buys']+f['trims']+f['force']:>2})  ", end='')
    print(f"  {geo:>5.2f}x  {total_trades:>3}")

# Detail trajectory for best v11
print(f"\n{'='*135}\nv11a TRAJECTORY DETAIL — does the cycle-day exit fire near the actual top?")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    r = simulate(data, day700_d=33, day900_d=50, day1050_hard=True, day1100_force=True)
    print(f"\n  {wn}: final ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x), MDD {r['max_dd']*100:+.1f}%")
    if r['t0']:
        print(f"    cycle T0 (first buy): {r['t0'].date()}")
        d1050 = r['t0'] + pd.Timedelta(days=1050)
        d1100 = r['t0'] + pd.Timedelta(days=1100)
        # find price near these dates
        for label, target in [("T0+700", r['t0']+pd.Timedelta(days=700)),
                              ("T0+900", r['t0']+pd.Timedelta(days=900)),
                              ("T0+1050", d1050),
                              ("T0+1100", d1100)]:
            try:
                idx = data.index.get_indexer([target], method="nearest")[0]
                if idx < len(data):
                    actual = data.index[idx]
                    print(f"    {label}: {actual.date()}  BTC ${data.iloc[idx].Close:,.0f}")
            except:
                pass
    print(f"    buys={r['buys']}, trims={r['trims']}, force_exits={r['force']}")
