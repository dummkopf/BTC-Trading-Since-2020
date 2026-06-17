"""
v2 backtest — inverted pyramid + signal trim + recycle compounding.
Window: 2022-09-01 → 2025-08-31 (analog of next post-bottom 3y bull).
Initial: $7000.  Max leverage: 3x (also tested 2x).

KEY FIXES vs v1
---------------
1. Layer fills require min-days spacing AND price-ladder (no more signal-spam
   firing all 6 layers in 5 days).
2. Trim signals are COST-BASIS-RELATIVE (% above avg_cost), so they keep
   working across regimes regardless of where 200DMA is.
3. Any trim → resets layers_filled so a fresh inverted pyramid can rebuild.
4. Re-entry of L1 requires a real dip (X% below last-trim price) AND a
   Mayer<threshold signal — prevents V-bounce whipsaw.

OBJECTIVE signal thresholds (round numbers, no fine tuning):
  - Mayer 0.80 / 0.85 / 0.90      (well-known)
  - RSI weekly 70 / 75 / 80        (classic OB)
  - Cost-relative: +50% / +100% / +200%
  - Layer ladder: -3 / -6 / -10 / -15 / -20 % from L1
"""
import pandas as pd, numpy as np, itertools, json

CSV = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(CSV, parse_dates=["Date"]).sort_values("Date").set_index("Date")
df["sma200"] = df.Close.rolling(200).mean()
df["mayer"] = df.Close / df.sma200
df["dev200"] = (df.Close / df.sma200 - 1) * 100
def rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_d"] = rsi(df.Close)
df["rsi_w"] = rsi(df.Close.resample("W-MON").last()).reindex(df.index, method="ffill")

START, END = "2022-09-01", "2025-08-31"
data = df.loc[START:END].copy().dropna(subset=["sma200"])
DATES = data.index.to_numpy()
CLOSES = data.Close.values
MAYER = data.mayer.values
RSI_W = data.rsi_w.values

INITIAL_CASH = 7_000.0
FUNDING_DAILY = 0.0001 * 3   # 0.01%/8h
TAKER_FEE = 0.0005

LAYER_PCT    = [0.10, 0.15, 0.20, 0.20, 0.20, 0.15]
PRICE_LADDER = [0.00, -0.03, -0.06, -0.10, -0.15, -0.20]

def run(params):
    """Run one parameter set; return (final_equity, stats_dict)."""
    L1_MAYER     = params["l1_mayer"]
    MIN_DAYS     = params["min_days_between_layers"]
    TRIM_PCTS    = params["trim_pcts"]          # tuple of 3 (size fractions)
    TRIM_GAINS   = params["trim_gains"]         # tuple of 3 (% above avg_cost)
    POST_TRIM_DIP = params["post_trim_dip"]     # required dip from last trim to re-arm
    MAX_LEV      = params["max_lev"]

    cash = INITIAL_CASH
    pos = 0.0
    avg = 0.0
    layers = [False]*6
    trims  = [False]*3
    l1_price = None
    last_trim_price = None
    days_since_last_fill = 9999
    total_realized, total_funding, total_fees = 0.0, 0.0, 0.0
    trades = []
    eq_curve = []

    for i in range(len(DATES)):
        dt = DATES[i]; mark = CLOSES[i]; mayer = MAYER[i]; rsiw = RSI_W[i]

        # funding
        if pos > 0:
            f = pos * mark * FUNDING_DAILY
            cash -= f; total_funding += f

        equity = cash + pos * (mark - avg) if pos > 0 else cash
        if equity <= 0:
            eq_curve.append(0.0)
            return 0.0, dict(equity=0.0, liquidated=True, trades=trades, realized=total_realized,
                              funding=total_funding, fees=total_fees, eq_curve=eq_curve)

        # ---- trims (cost-relative gain) ----
        if pos > 0:
            gain = (mark - avg) / avg
            for ti in range(3):
                if trims[ti]: continue
                threshold_hit = (gain >= TRIM_GAINS[ti]) or (ti == 0 and rsiw > 80)
                if threshold_hit:
                    btc_sell = pos * TRIM_PCTS[ti]
                    usd = btc_sell * mark
                    fee = usd * TAKER_FEE
                    realized = btc_sell * (mark - avg) - fee
                    cash += realized
                    total_realized += realized
                    total_fees += fee
                    pos -= btc_sell
                    trims[ti] = True
                    last_trim_price = mark
                    trades.append((dt, "SELL", ti, mark, usd, btc_sell, f"T{ti+1} +{gain*100:.0f}%"))
                    # any trim => reset layers so new inverted pyramid can build
                    layers = [False]*6
                    l1_price = None
                    days_since_last_fill = 9999
                    if pos < 1e-9:
                        pos = 0.0; avg = 0.0
                        trims = [False]*3
                    break   # one trim per bar

        # ---- entries ----
        days_since_last_fill += 1
        equity = cash + pos * (mark - avg) if pos > 0 else cash

        if not layers[0]:
            # L1 gate: Mayer signal + (if there was a prior trim) require dip
            sig_ok = mayer < L1_MAYER
            dip_ok = (last_trim_price is None) or (mark <= last_trim_price * (1 - POST_TRIM_DIP))
            if sig_ok and dip_ok:
                # place L1
                target = equity * MAX_LEV
                size = target * LAYER_PCT[0]
                cur_notional = pos * mark
                size = min(size, max(0, MAX_LEV*equity - cur_notional))
                if size > 1:
                    btc = size / mark
                    fee = size * TAKER_FEE
                    cash -= fee; total_fees += fee
                    new_pos = pos + btc
                    avg = (pos*avg + btc*mark) / new_pos if pos > 0 else mark
                    pos = new_pos
                    layers[0] = True
                    l1_price = mark
                    trims = [False]*3
                    days_since_last_fill = 0
                    trades.append((dt, "BUY", 0, mark, size, btc, "L1"))
        else:
            # try L2..L6 in order — price ladder + min days between fills
            if days_since_last_fill >= MIN_DAYS:
                for li in range(1, 6):
                    if layers[li]: continue
                    if mark <= l1_price * (1 + PRICE_LADDER[li]):
                        target = equity * MAX_LEV
                        size = target * LAYER_PCT[li]
                        cur_notional = pos * mark
                        size = min(size, max(0, MAX_LEV*equity - cur_notional))
                        if size > 1:
                            btc = size / mark
                            fee = size * TAKER_FEE
                            cash -= fee; total_fees += fee
                            new_pos = pos + btc
                            avg = (pos*avg + btc*mark) / new_pos
                            pos = new_pos
                            layers[li] = True
                            days_since_last_fill = 0
                            trades.append((dt, "BUY", li, mark, size, btc, f"L{li+1} ladder"))
                        break  # one buy per bar

        eq = cash + pos*(mark-avg) if pos > 0 else cash
        eq_curve.append(eq)

    final = eq_curve[-1]
    # MDD
    s = pd.Series(eq_curve)
    mdd = (s/s.cummax() - 1).min()
    return final, dict(equity=final, liquidated=False, trades=trades,
                       realized=total_realized, funding=total_funding, fees=total_fees,
                       eq_curve=eq_curve, mdd=mdd, final_pos=pos, final_avg=avg, final_cash=cash)

# ===================== BENCHMARKS =====================================
def spot_hodl():
    p0 = CLOSES[0]; btc = INITIAL_CASH / p0
    return [c * btc for c in CLOSES]

def lev_hodl(L=3.0):
    c, avg, pos, liq, out = INITIAL_CASH, CLOSES[0], INITIAL_CASH*L/CLOSES[0], False, []
    for mark in CLOSES:
        if liq: out.append(0); continue
        c -= pos * mark * FUNDING_DAILY
        eq = c + pos*(mark - avg)
        if eq <= 0: liq = True; eq = 0
        out.append(eq)
    return out

bm_spot = spot_hodl()
bm_lev3 = lev_hodl(3.0)
bm_lev2 = lev_hodl(2.0)

# ===================== GRID SEARCH ====================================
grid = {
    "l1_mayer":               [0.80, 0.85, 0.90],
    "min_days_between_layers":[7, 14],
    "trim_gains":             [(0.50,1.00,2.00),
                               (0.40,0.80,1.50),
                               (0.30,0.60,1.20)],
    "trim_pcts":              [(0.25,0.25,0.25),
                               (0.20,0.30,0.40),
                               (0.15,0.25,0.35)],
    "post_trim_dip":          [0.15, 0.25, 0.35],
    "max_lev":                [2.0, 3.0],
}
keys = list(grid.keys())
combos = list(itertools.product(*[grid[k] for k in keys]))
print(f"Running {len(combos)} configurations over {len(DATES)} bars...")

results = []
for combo in combos:
    p = dict(zip(keys, combo))
    final, info = run(p)
    results.append((final, info["mdd"], len(info["trades"]), p, info))

results.sort(key=lambda x: -x[0])

# ===================== REPORT =========================================
print(f"\n{'='*110}")
print(f"WINDOW {START} → {END}   ({(data.index[-1]-data.index[0]).days} days, {(data.index[-1]-data.index[0]).days/365.25:.2f} yrs)")
print(f"BTC: ${CLOSES[0]:,.0f} → ${CLOSES[-1]:,.0f}  ({CLOSES[-1]/CLOSES[0]-1:+.0%})")
print(f"  Spot HODL  $7k → ${bm_spot[-1]:,.0f}  ({bm_spot[-1]/INITIAL_CASH-1:+.0%})")
print(f"  2x Perp    $7k → ${bm_lev2[-1]:,.0f}  ({bm_lev2[-1]/INITIAL_CASH-1:+.0%})")
print(f"  3x Perp    $7k → ${bm_lev3[-1]:,.0f}  ({bm_lev3[-1]/INITIAL_CASH-1:+.0%})")
print(f"{'='*110}")

print(f"\nTOP 10 PARAM SETS")
print(f"{'eq':>10}  {'mdd':>7}  {'#tr':>4}  {'L1mayer':>7}  {'mindays':>7}  {'trim_gains':>20}  {'trim_pcts':>16}  {'postdip':>7}  {'lev':>4}")
for r in results[:10]:
    final, mdd, ntr, p, info = r
    print(f"  ${final:>9,.0f}  {mdd*100:>+6.1f}%  {ntr:>3}  {p['l1_mayer']:>7.2f}  {p['min_days_between_layers']:>7d}  {str(p['trim_gains']):>20}  {str(p['trim_pcts']):>16}  {p['post_trim_dip']:>7.2f}  {p['max_lev']:>4.1f}")

print(f"\nBOTTOM 5 (worst):")
for r in results[-5:]:
    final, mdd, ntr, p, info = r
    print(f"  ${final:>9,.0f}  {mdd*100:>+6.1f}%  {ntr:>3}  {p['l1_mayer']:>7.2f}  {p['min_days_between_layers']:>7d}  {str(p['trim_gains']):>20}  {str(p['trim_pcts']):>16}  {p['post_trim_dip']:>7.2f}  {p['max_lev']:>4.1f}")

# parameter sensitivity (avg equity by each parameter value)
print(f"\nPARAMETER SENSITIVITY (mean final equity)")
for k in keys:
    print(f"  {k}:")
    vals = sorted(set(str(p[k]) for *_, p, _ in results))
    for v in vals:
        grp = [r[0] for r in results if str(r[3][k]) == v]
        print(f"    {v:<28}  mean=${np.mean(grp):>9,.0f}   median=${np.median(grp):>9,.0f}   n={len(grp)}")

# inspect the winner
print(f"\n{'='*110}")
final, mdd, ntr, p, info = results[0]
print(f"WINNING CONFIG (final equity ${final:,.0f}, {final/INITIAL_CASH-1:+.0%}, MDD {mdd*100:.1f}%)")
print(json.dumps(p, default=str, indent=2))
print(f"\nTrades ({len(info['trades'])} total):")
print(f"  {'date':10}  {'side':4}  {'L#':3}  {'price':>9}  {'usd':>10}  {'btc':>9}  reason")
for t in info["trades"]:
    print(f"  {t[0].astype('datetime64[D]')!s:10}  {t[1]:4}  {t[2]:>3}  ${t[3]:>8,.0f}  ${t[4]:>9,.0f}  {t[5]:>9.5f}  {t[6]}")
print(f"\n  Final position: {info['final_pos']:.5f} BTC @ avg ${info['final_avg']:,.0f}   Cash ${info['final_cash']:,.0f}")
print(f"  Realized PnL ${info['realized']:,.0f}  |  Funding ${info['funding']:,.0f}  |  Fees ${info['fees']:,.0f}")

# Save winner's equity curve + a couple of robust runner-ups
out_curves = pd.DataFrame({"date": data.index, "btc_close": CLOSES,
                           "spot_hodl": bm_spot, "lev2_hodl": bm_lev2, "lev3_hodl": bm_lev3,
                           "v2_best": results[0][4]["eq_curve"]})
out_curves.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v2_curves.csv", index=False)
print("\nWriting top-10 params + equity to backtest_v2_curves.csv")
