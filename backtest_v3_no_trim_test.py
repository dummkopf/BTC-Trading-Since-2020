"""
Test the user's hypothesis: in a bull cycle, 3x leverage doesn't liquidate
because liquidation is measured from entry price, not from peak.

Run several variants:
A) 3x HODL from window start (2022-09-01) — wide-eyed entry
B) 3x HODL from "perfect bottom" (2022-11-21 FTX low)
C) Inverted pyramid + NO trims (pure long-bias, hold to end)
D) Inverted pyramid + late trim only (cycle-top signal, +300% above cost)
E) v2 best (with cost-relative trims)

For each: check (i) final equity, (ii) USD MDD, (iii) BTC-quantity end,
(iv) closest call to liquidation (worst intra-day if using High/Low).

Window: 2022-09-01 → 2025-08-31.  Start: $7,000.
"""
import pandas as pd, numpy as np

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")
df["sma200"] = df.Close.rolling(200).mean()
df["mayer"] = df.Close / df.sma200
def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_w"] = rsi(df.Close.resample("W-MON").last()).reindex(df.index, method="ffill")

START, END = "2022-09-01", "2025-08-31"
d = df.loc[START:END].copy().dropna(subset=["sma200"])

INITIAL = 7000.0
FUNDING_DAILY = 0.0001 * 3
FEE = 0.0005
MAINT_MARGIN = 0.005   # 0.5% maintenance margin on BitMEX

def perp_hodl(start_idx, L, leverage_label=""):
    """Buy at start_idx using leverage L, hold to end. Check intraday liquidation."""
    sub = d.iloc[start_idx:].copy()
    p0 = sub.Close.iloc[0]
    pos = INITIAL * L / p0
    avg = p0
    c = INITIAL
    out = []
    near_liq = 0.0   # closest "min equity" approach (% of initial)
    liq_date = None
    for dt, r in sub.iterrows():
        c -= pos * r.Close * FUNDING_DAILY
        # intra-day check: liquidation if equity_at_low ≤ maintenance margin
        eq_low = c + pos * (r.Low - avg)
        if liq_date is None and eq_low <= pos * r.Low * MAINT_MARGIN:
            liq_date = dt
            pos = 0; c = 0; avg = 0
        eq_close = c + pos * (r.Close - avg) if pos > 0 else c
        if pos > 0:
            min_eq_pct = eq_low / INITIAL
            if min_eq_pct < (near_liq or 1):
                near_liq = min_eq_pct
        out.append(max(eq_close, 0))
    s = pd.Series(out, index=sub.index)
    return s, near_liq, liq_date

def pyramid(trim_mode="cost_rel"):
    """Inverted pyramid with one of: 'none', 'cost_rel', 'late', 'paul_wei_like'."""
    cash = INITIAL
    pos = 0.0; avg = 0.0
    layers = [False]*6
    trims  = [False]*3
    l1p = None
    LAYER_PCT = [0.10, 0.15, 0.20, 0.20, 0.20, 0.15]
    LADDER    = [0.00, -0.03, -0.06, -0.10, -0.15, -0.20]
    L1_MAYER = 0.85
    MIN_DAYS = 14
    MAX_LEV = 3.0
    last_fill = -9999
    if trim_mode == "none":
        TG = None
    elif trim_mode == "cost_rel":      # v2 best
        TG = [0.50, 1.00, 2.00]; TP = [0.15, 0.25, 0.35]
    elif trim_mode == "late":          # only sell on parabolic
        TG = [2.00, 3.50, 5.00]; TP = [0.15, 0.25, 0.30]
    elif trim_mode == "paul_wei_like": # frequent small trims (his median hold = 0.55d)
        TG = [0.30, 0.60, 1.00]; TP = [0.10, 0.10, 0.10]

    fees_total = 0.0; funding_total = 0.0; realized = 0.0
    out = []
    near_liq = 1.0
    liq_date = None

    for i, (dt, r) in enumerate(d.iterrows()):
        # funding
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_total += f
        # liquidation check using daily Low
        if pos > 0:
            eq_low = cash + pos * (r.Low - avg)
            mm = pos * r.Low * MAINT_MARGIN
            if eq_low <= mm and liq_date is None:
                liq_date = dt
                pos = 0; avg = 0; cash = 0
            min_eq_pct = eq_low / INITIAL
            if min_eq_pct < near_liq: near_liq = min_eq_pct

        # trims
        if pos > 0 and TG is not None:
            gain = (r.Close - avg) / avg
            for ti in range(3):
                if trims[ti]: continue
                if gain >= TG[ti]:
                    btc_sell = pos * TP[ti]
                    usd = btc_sell * r.Close
                    fee = usd * FEE
                    realized += btc_sell * (r.Close - avg) - fee
                    cash += btc_sell * (r.Close - avg) - fee
                    fees_total += fee
                    pos -= btc_sell
                    trims[ti] = True
                    layers = [False]*6
                    l1p = None
                    last_fill = -9999
                    if pos < 1e-9:
                        pos = 0; avg = 0; trims = [False]*3
                    break

        # entries
        equity = cash + pos*(r.Close - avg) if pos > 0 else cash
        if equity > 0 and liq_date is None:
            if not layers[0]:
                if r.mayer < L1_MAYER:
                    target = equity * MAX_LEV
                    sz = target * LAYER_PCT[0]
                    sz = min(sz, max(0, MAX_LEV*equity - pos*r.Close))
                    if sz > 1:
                        btc = sz / r.Close
                        fee = sz * FEE; cash -= fee; fees_total += fee
                        new_pos = pos + btc
                        avg = (pos*avg + btc*r.Close)/new_pos if pos>0 else r.Close
                        pos = new_pos
                        layers[0] = True; l1p = r.Close; trims = [False]*3
                        last_fill = i
            else:
                if i - last_fill >= MIN_DAYS:
                    for li in range(1, 6):
                        if layers[li]: continue
                        if r.Close <= l1p * (1 + LADDER[li]):
                            target = equity * MAX_LEV
                            sz = target * LAYER_PCT[li]
                            sz = min(sz, max(0, MAX_LEV*equity - pos*r.Close))
                            if sz > 1:
                                btc = sz / r.Close
                                fee = sz * FEE; cash -= fee; fees_total += fee
                                new_pos = pos + btc
                                avg = (pos*avg + btc*r.Close)/new_pos
                                pos = new_pos
                                layers[li] = True; last_fill = i
                            break

        eq_close = cash + pos*(r.Close - avg) if pos > 0 else cash
        out.append(max(eq_close, 0))

    s = pd.Series(out, index=d.index)
    return s, near_liq, liq_date, pos, avg, cash, realized, funding_total

def summary(name, s, near_liq, liq_date, **extras):
    end = s.iloc[-1]
    total = end/s.iloc[0]-1
    yrs = (s.index[-1]-s.index[0]).days/365.25
    cagr = (end/s.iloc[0])**(1/yrs)-1 if end > 0 else -1
    mdd = (s/s.cummax()-1).min()
    liq_str = liq_date.date() if liq_date else "—"
    print(f"  {name:38}  end=${end:>9,.0f}  ({total*100:>+5.0f}%)  CAGR {cagr*100:>+5.1f}%  USD-MDD {mdd*100:>+5.1f}%  near-liq {near_liq*100:>5.1f}%  liq={liq_str}")

print(f"\n{'='*120}")
print(f"WINDOW {START} → {END}   BTC ${d.Close.iloc[0]:,.0f} → ${d.Close.iloc[-1]:,.0f} (+{d.Close.iloc[-1]/d.Close.iloc[0]-1:.0%})")
print(f"Liquidation model: equity_at_daily_low ≤ position * low * 0.5% maintenance margin")
print(f"{'='*120}")

# A: 3x HODL from window start
sa, na, la = perp_hodl(0, 3.0)
summary("A: 3x HODL from 2022-09-01", sa, na, la)

# A2: 2x HODL from window start (for context)
sa2, na2, la2 = perp_hodl(0, 2.0)
summary("    2x HODL from 2022-09-01", sa2, na2, la2)

# B: 3x HODL from perfect bottom 2022-11-21
ftx_idx = d.index.get_loc(pd.Timestamp("2022-11-21"))
sb, nb, lb = perp_hodl(ftx_idx, 3.0)
sb_full = pd.Series(index=d.index, dtype=float)
sb_full.iloc[:ftx_idx] = INITIAL
sb_full.iloc[ftx_idx:] = sb.values
summary("B: 3x HODL from 2022-11-21 FTX bot", sb_full, nb, lb)

# C: Pyramid + NO trims (long bias, hold)
sc, nc, lc, *_ = pyramid("none")
summary("C: Inverted pyramid + NO trims", sc, nc, lc)

# D: Pyramid + LATE trims only (cycle top)
sd, nd, ld, *_ = pyramid("late")
summary("D: Inverted pyramid + late trims", sd, nd, ld)

# E: v2 best (cost-relative trims)
se, ne, le, *_ = pyramid("cost_rel")
summary("E: v2 best (cost-rel trims)", se, ne, le)

# F: Paul-Wei-like (frequent small trims)
sf, nf, lf, *_ = pyramid("paul_wei_like")
summary("F: Frequent small trims (PW-like)", sf, nf, lf)

# Spot HODL
spot = INITIAL * d.Close / d.Close.iloc[0]
summary("X: Spot HODL", spot, 1.0, None)

# Key debug: when did A get closest to liquidation?
print(f"\nLIQUIDATION RISK PROBE FOR 3x HODL FROM WINDOW START (2022-09-01)")
p0 = d.Close.iloc[0]
pos_a = INITIAL * 3 / p0
avg_a = p0
worst = (1.0, None, None)
for dt, r in d.iterrows():
    if dt > pd.Timestamp("2023-06-01"): break  # only check first 9 months
    eq_low = INITIAL + pos_a * (r.Low - avg_a)
    pct = eq_low / INITIAL
    if pct < worst[0]: worst = (pct, dt, r.Low)
print(f"  Closest call (using daily Low): equity at {worst[0]*100:.1f}% of initial on {worst[1].date()}, BTC ${worst[2]:,.0f}")
print(f"  Liquidation line ≈ avg × (1 - 1/3 + 0.5%) = ${avg_a*(1-1/3+0.005):,.0f}")
print(f"  Actual worst BTC Low in first 9m = ${d.loc[:'2023-06-01'].Low.min():,.0f} on {d.loc[:'2023-06-01'].Low.idxmin().date()}")
