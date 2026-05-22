"""
v10: "Buy cycle bottom 3x, escape cycle top" — tested with mechanical
signals (no hindsight) on both PW5 and W5.

The point: prove or disprove user's intuition that simple
bottom-buy + top-sell beats the elaborate v9f rolling pyramid.

Entry signal candidates (joint, must all be true):
  Mayer < X_buy AND RSI_w < Y_buy   (deep oversold confluence)
Optional extra: 30/60-day return is very negative
Exit signal candidates (joint OR):
  Mayer > X_sell OR RSI_w > Y_sell  (overheated)

Tested fully mechanical — first trigger fires action.

Compared with v9f (cross-cycle 8.43x geo).
"""
import pandas as pd, numpy as np, itertools

df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
df["sma200"] = df.Close.rolling(200).mean()
df["mayer"] = df.Close / df.sma200
def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_d"] = rsi(df.Close)
df["rsi_w"] = rsi(df.Close.resample("W-MON").last()).reindex(df.index, method="ffill")
df["r30d"] = df.Close.pct_change(30) * 100
df["r60d"] = df.Close.pct_change(60) * 100

INITIAL = 7000.0
FUNDING_DAILY = 0.0001*3; FEE = 0.0005; MM = 0.005

WINDOWS = {
    "PW5-PREV":  ("2018-09-01", "2021-08-31"),
    "W5-CURR":   ("2022-09-01", "2025-08-31"),
    "PW1-BEAR":  ("2017-12-17", "2018-12-15"),
    "W1-BEAR":   ("2021-11-08", "2022-11-21"),
}

def bottom_top_strategy(data, lev, mayer_buy, rsi_w_buy, mayer_sell, rsi_w_sell,
                        partial_sell_pct=1.0, deep_oversold_extra=False):
    """
    Bottom-buy + top-sell: enter ALL-IN at lev when bottom signal fires,
    fully (or partial) exit when top signal fires. Cycle repeats.
    """
    cash = INITIAL; pos = 0.0; avg = 0.0
    in_position = False
    liq = False; near = 1.0
    n_entries = 0; n_exits = 0; fees_t = funding_t = realized_t = 0
    eq_curve = []
    cooldown_after_exit = 30  # days
    last_exit_date = None

    for dt, r in data.iterrows():
        if liq:
            eq_curve.append(0); continue
        if pos > 0:
            cash -= pos * r.Close * FUNDING_DAILY
            funding_t += pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg); mm_req = pos*r.Low*MM
            near = min(near, eq_low/INITIAL)
            if eq_low <= mm_req:
                liq=True; cash=0; pos=0; avg=0
                eq_curve.append(0); continue

        # EXIT: any top signal triggers
        if pos > 0:
            top_signal = ((not np.isnan(r.mayer) and r.mayer > mayer_sell)
                           or (not np.isnan(r.rsi_w) and r.rsi_w > rsi_w_sell))
            if top_signal:
                btc_sell = pos * partial_sell_pct
                usd = btc_sell * r.Close; fee = usd * FEE
                realized = btc_sell*(r.Close - avg) - fee
                cash += realized; realized_t += realized; fees_t += fee
                pos -= btc_sell
                if pos < 1e-9: pos = 0; avg = 0; in_position = False
                n_exits += 1; last_exit_date = dt
                eq_curve.append(cash + pos*(r.Close - avg) if pos>0 else cash); continue

        # ENTRY: bottom signal
        if not in_position and (last_exit_date is None or (dt - last_exit_date).days >= cooldown_after_exit):
            buy_signal = (not np.isnan(r.mayer) and r.mayer < mayer_buy
                          and not np.isnan(r.rsi_w) and r.rsi_w < rsi_w_buy)
            if deep_oversold_extra:
                buy_signal = buy_signal and (not np.isnan(r.r60d) and r.r60d < -20)
            if buy_signal:
                equity = cash
                size = equity * lev
                btc = size / r.Close
                fee = size * FEE; cash -= fee; fees_t += fee
                pos = btc; avg = r.Close
                in_position = True; n_entries += 1

        eq_curve.append(cash + pos*(r.Close - avg) if pos > 0 else cash)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(curve=s, final=final, liq=liq, near=near,
                n_entries=n_entries, n_exits=n_exits,
                realized=realized_t, funding=funding_t,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)

def spot_h(data): return INITIAL * data.Close.iloc[-1]/data.Close.iloc[0]

# Configs to test
configs = [
    # name, lev, mb, rb, ms, rs, partial, deep_oversold
    ("v10a 3x  Mayer<0.8/RSIw<35  exit M>1.4 OR RSIw>75",     3.0, 0.80, 35, 1.4, 75, 1.0, False),
    ("v10b 3x  Mayer<0.7/RSIw<30  exit M>1.4 OR RSIw>75",     3.0, 0.70, 30, 1.4, 75, 1.0, False),
    ("v10c 3x  Mayer<0.85/RSIw<40 exit M>1.5 OR RSIw>75",     3.0, 0.85, 40, 1.5, 75, 1.0, False),
    ("v10d 3x  + r60d<-20 confirm",                            3.0, 0.85, 40, 1.5, 75, 1.0, True),
    ("v10e 2x  Mayer<0.85/RSIw<40 exit M>1.4 OR RSIw>70",     2.0, 0.85, 40, 1.4, 70, 1.0, False),
    ("v10f 3x  partial 50% exit on each top sig",              3.0, 0.85, 40, 1.4, 70, 0.5, False),
    ("v10g 1.5x Mayer<0.85/RSIw<40 exit M>1.4 OR RSIw>70",    1.5, 0.85, 40, 1.4, 70, 1.0, False),
    ("v10h 3x  STRICT bot 0.6/25  exit M>1.5 OR RSIw>75",     3.0, 0.60, 25, 1.5, 75, 1.0, False),
]

print(f"\n{'='*135}\nv10 'BUY BOTTOM, ESCAPE TOP' VS v9f")
print(f"{'='*135}")

print(f"\n  {'Config':52}  ", end='')
for wn in WINDOWS.keys():
    print(f"{wn:>14}  ", end='')
print(f"  geo(PW5,W5)  n_trades")
print(f"  {'-'*52}  " + "  ".join(["-"*14]*len(WINDOWS)) + "  -----------  --------")

for cfg in configs:
    name, lev, mb, rb, ms, rs, partial, deep = cfg
    finals = {}; n_t = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]].copy()
        r = bottom_top_strategy(data, lev, mb, rb, ms, rs, partial, deep)
        finals[wn] = r
        n_t[wn] = r["n_entries"] + r["n_exits"]
    pw5 = finals["PW5-PREV"]["final"]/INITIAL
    w5  = finals["W5-CURR"]["final"]/INITIAL
    geo = (pw5*w5)**0.5 if pw5>0 and w5>0 else 0
    total_trades = sum(n_t.values())
    print(f"  {name:52}  ", end='')
    for wn in WINDOWS.keys():
        f = finals[wn]
        liq_mark = '!' if f["liq"] else ' '
        print(f"{f['final']/INITIAL:>8.2f}x{liq_mark:<2}({n_t[wn]:>2})  ", end='')
    print(f"  {geo:>8.2f}x   {total_trades:>3}")

# Spot baselines
print(f"\n  {'BENCHMARKS':52}  ", end='')
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    sh = spot_h(data) / INITIAL
    print(f"  spot:{sh:>5.2f}x   ", end='')
print()

# v9f reference (from previous run, known values)
print(f"  {'v9f (1.5→3x phased no stop) — for reference':52}    {7.65:>5.2f}x       {9.30:>5.2f}x       {0.19:>5.2f}x       {0.34:>5.2f}x        {8.43:>5.2f}x")

# Detail best v10 vs v9f on PW5
print(f"\n{'='*135}\nDETAIL — Best v10 vs v9f trajectory on PW5")
# Pick best v10 by geo
best_cfg = None; best_geo = 0
for cfg in configs:
    name, lev, mb, rb, ms, rs, partial, deep = cfg
    p = bottom_top_strategy(df.loc[WINDOWS["PW5-PREV"][0]:WINDOWS["PW5-PREV"][1]].copy(),
                              lev, mb, rb, ms, rs, partial, deep)
    w = bottom_top_strategy(df.loc[WINDOWS["W5-CURR"][0]:WINDOWS["W5-CURR"][1]].copy(),
                              lev, mb, rb, ms, rs, partial, deep)
    pw5 = p["final"]/INITIAL; w5 = w["final"]/INITIAL
    if pw5 > 0 and w5 > 0:
        g = (pw5*w5)**0.5
        if g > best_geo: best_geo = g; best_cfg = (cfg, p, w)

if best_cfg:
    (cfg_tup, p, w) = best_cfg
    name = cfg_tup[0]
    print(f"\n  BEST v10: {name}")
    print(f"    PW5: final ${p['final']:,.0f} ({p['final']/INITIAL:.2f}x), MDD {p['max_dd']*100:+.1f}%, near-liq {p['near']*100:.1f}%, entries={p['n_entries']}, exits={p['n_exits']}")
    print(f"    W5:  final ${w['final']:,.0f} ({w['final']/INITIAL:.2f}x), MDD {w['max_dd']*100:+.1f}%, near-liq {w['near']*100:.1f}%, entries={w['n_entries']}, exits={w['n_exits']}")
    print(f"    geo: {best_geo:.2f}x  (vs v9f 8.43x)")

# Also: when do bottom/top signals FIRE in each window? show actual dates
print(f"\n{'='*135}\nSIGNAL DATES (Mayer<0.8 AND RSI_w<35 = bottom;  Mayer>1.4 OR RSI_w>75 = top)")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]].copy()
    print(f"\n  {wn}:")
    bot_dates = data[(data.mayer<0.80) & (data.rsi_w<35)].index
    top_dates = data[(data.mayer>1.4) | (data.rsi_w>75)].index
    print(f"    bottom signals ({len(bot_dates)}): " +
          (", ".join(d.strftime('%Y-%m-%d') for d in bot_dates[:5]) + (" ..." if len(bot_dates)>5 else "")))
    print(f"    top signals ({len(top_dates)}): " +
          (", ".join(d.strftime('%Y-%m-%d') for d in top_dates[:5]) + (" ..." if len(top_dates)>5 else "")))
    if len(bot_dates) > 0 and len(top_dates) > 0:
        print(f"    first bottom: {bot_dates[0].date()} BTC ${data.loc[bot_dates[0],'Close']:,.0f}")
        print(f"    first top after first bottom: ", end='')
        post = [d for d in top_dates if d > bot_dates[0]]
        if post:
            print(f"{post[0].date()} BTC ${data.loc[post[0],'Close']:,.0f}")
        else:
            print("none in window")
