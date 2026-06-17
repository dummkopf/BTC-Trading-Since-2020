"""
Build the apples-to-apples Paul Wei benchmark for window 2022-09-01 → 2025-08-31:
- Take his daily XBT-equivalent adjusted wealth
- Renormalize so window-start = $7,000 USD-equivalent
- Carry through using his actual XBT-eq multiplier, converted at BTC spot

This isolates HIS TRADING ALPHA from BTC price exposure: if his XBT-equivalent
wealth multiplier goes up, he's making BTC quantity (real alpha); if it stays
flat, he's just riding price; if it goes down, he's losing to HODL.
"""
import pandas as pd, numpy as np

EC = "/home/user/BTC-Trading-Since-2020/derived-equity-curve.csv"
OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"

ec = pd.read_csv(EC, parse_dates=["timestamp"], usecols=["timestamp", "adjustedWealthXBT", "adjustedWealthMultipleVsBaseline"])
ec = ec.sort_values("timestamp").reset_index(drop=True)
ec["date"] = ec.timestamp.dt.tz_convert("UTC").dt.date
# last value per day
daily_xbt = ec.groupby("date").adjustedWealthXBT.last()
daily_xbt.index = pd.to_datetime(daily_xbt.index)
print(f"Paul Wei daily equity rows: {len(daily_xbt)}, {daily_xbt.index.min().date()} → {daily_xbt.index.max().date()}")

# BTC USD price
px = pd.read_csv(OHLC, parse_dates=["Date"]).set_index("Date").Close

# Window
START, END = pd.Timestamp("2022-09-01"), pd.Timestamp("2025-08-31")

# Align to daily within window
idx = pd.date_range(START, END, freq="D")
xbt_eq = daily_xbt.reindex(idx, method="ffill")
btc_usd = px.reindex(idx, method="ffill")

# USD-equivalent equity (his XBT * spot)
usd_eq_raw = xbt_eq * btc_usd
# rescale to $7,000 at window start
usd_eq = usd_eq_raw / usd_eq_raw.iloc[0] * 7000

# XBT-denominated equity (pure trading alpha — strip out BTC price effect)
xbt_eq_norm = xbt_eq / xbt_eq.iloc[0]

# benchmarks
spot_hodl = 7000 * btc_usd / btc_usd.iloc[0]
def perp_hodl(L):
    pos = 7000 * L / btc_usd.iloc[0]
    avg = btc_usd.iloc[0]
    c = 7000.0
    out = []
    for p in btc_usd.values:
        c -= pos * p * 0.0001 * 3   # funding
        eq = c + pos * (p - avg)
        out.append(max(eq, 0))
    return pd.Series(out, index=btc_usd.index)
lev2 = perp_hodl(2)
lev3 = perp_hodl(3)

# v2 winning strategy curve (read from saved file)
v2 = pd.read_csv("/home/user/BTC-Trading-Since-2020/backtest_v2_curves.csv", parse_dates=["date"]).set_index("date").v2_best
v2 = v2.reindex(idx, method="ffill")

# ---- print summary ----
print(f"\n{'='*100}")
print(f"WINDOW {START.date()} → {END.date()}  |  BTC {btc_usd.iloc[0]:,.0f} → {btc_usd.iloc[-1]:,.0f}  ({btc_usd.iloc[-1]/btc_usd.iloc[0]-1:+.0%})")
print(f"{'='*100}")
print(f"{'Strategy':30}  {'Start':>9}  {'End':>10}  {'Total':>7}  {'CAGR':>6}  {'MDD':>6}")
for name, s in [("Spot HODL $7k", spot_hodl),
                ("2x Perp HODL $7k", lev2),
                ("3x Perp HODL $7k", lev3),
                ("v2 strategy (this work)", v2),
                ("Paul Wei (USD-equiv, rescaled)", usd_eq)]:
    total = s.iloc[-1]/s.iloc[0]-1
    yrs = (s.index[-1]-s.index[0]).days/365.25
    cagr = (s.iloc[-1]/s.iloc[0])**(1/yrs)-1
    mdd = (s/s.cummax()-1).min()
    print(f"  {name:30}  ${s.iloc[0]:>8,.0f}  ${s.iloc[-1]:>9,.0f}  {total*100:>+6.0f}%  {cagr*100:>+5.1f}%  {mdd*100:>+5.1f}%")

# Paul's XBT-denominated multiplier — this is his REAL trading alpha
xbt_total = xbt_eq_norm.iloc[-1] - 1
xbt_mdd = (xbt_eq_norm/xbt_eq_norm.cummax()-1).min()
print(f"\n  Paul Wei XBT-equiv multiplier (pure alpha):")
print(f"    Start XBT-eq: {xbt_eq.iloc[0]:.4f} XBT   End: {xbt_eq.iloc[-1]:.4f} XBT")
print(f"    Multiplier:  {xbt_eq_norm.iloc[-1]:.3f}x   ({xbt_total*100:+.1f}% in BTC-quantity terms)")
print(f"    XBT-MDD:     {xbt_mdd*100:+.1f}%")
print(f"  → Interpretation: if multiplier >1.0, he generated BTC quantity (alpha vs HODL).")
print(f"                    if ≈1.0, he just rode BTC price (no alpha).")
print(f"                    if <1.0, he lost to HODL despite USD gains.")

# breakdown by year
print(f"\nYearly XBT-equiv multiplier (relative to window start):")
for yr in [2022, 2023, 2024, 2025]:
    yend = pd.Timestamp(f"{yr}-12-31")
    if yend < START or yend > END: yend = END if yr == 2025 else yend
    if yend > END: continue
    try:
        v = xbt_eq_norm.loc[yend.strftime("%Y-%m-%d")]
        print(f"  {yend.date()}: {v:.3f}x  (USD-eq ${usd_eq.loc[yend.strftime('%Y-%m-%d')]:,.0f})")
    except KeyError:
        pass
print(f"  {END.date()}: {xbt_eq_norm.iloc[-1]:.3f}x  (USD-eq ${usd_eq.iloc[-1]:,.0f})")

# Save curves for plotting
out = pd.DataFrame({
    "date": idx,
    "btc_close": btc_usd.values,
    "paul_usd_eq": usd_eq.values,
    "paul_xbt_mult": xbt_eq_norm.values,
    "spot_hodl": spot_hodl.values,
    "lev2_hodl": lev2.values,
    "lev3_hodl": lev3.values,
    "v2_strategy": v2.values,
})
out.to_csv("/home/user/BTC-Trading-Since-2020/paulwei_vs_strategies.csv", index=False)
print(f"\nSaved comparison: paulwei_vs_strategies.csv")
