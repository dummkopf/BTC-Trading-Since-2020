"""
Signal calibration for BTC inverted-pyramid bottom-fishing strategy.
Backtests common 'oversold/bottom' triggers on daily OHLC 2021-09 to 2026-05.

For each signal we measure:
 1. dates fired
 2. forward returns at +30 / +90 / +180 days
 3. drawdown from fire-date to the lowest subsequent close within 60d (timing risk)
 4. distance to the next 'real' cycle low

Reference lows in this window (visible from data):
 - 2022-11-21  $15,599 (FTX low — THE bear bottom)
 - 2023-09-11  ~$25,162 (mid-2023 chop)
 - 2024-08-05  ~$49,121 (yen carry unwind)
 - 2025-04-08  (TBD from data)
 - current drawdown (peak 2025-10-06 $126k → $77k = ~38%)
"""
import pandas as pd, numpy as np

df = pd.read_csv("/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv",
                 parse_dates=["Date"]).sort_values("Date").reset_index(drop=True)
df = df.set_index("Date")

# --- INDICATORS ---------------------------------------------------------
df["sma200"] = df.Close.rolling(200).mean()
df["dev200_pct"] = (df.Close / df.sma200 - 1) * 100   # % above/below 200DMA

df["sma50"]  = df.Close.rolling(50).mean()
df["sma20"]  = df.Close.rolling(20).mean()
df["bb_std"] = df.Close.rolling(20).std()
df["bb_lo"]  = df.sma20 - 2 * df.bb_std
df["bb_up"]  = df.sma20 + 2 * df.bb_std
df["bb_pos"] = (df.Close - df.bb_lo) / (df.bb_up - df.bb_lo)  # 0=at lower, 1=at upper

def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = up / dn
    return 100 - 100/(1+rs)

df["rsi_d"] = rsi(df.Close)
# weekly + monthly RSI re-indexed back to daily for alignment
w = df.Close.resample("W-MON").last()
df["rsi_w"] = rsi(w).reindex(df.index, method="ffill")
m = df.Close.resample("ME").last()
df["rsi_m"] = rsi(m).reindex(df.index, method="ffill")

# consecutive red candles
red = (df.Close < df.Open).astype(int)
streak = []
c = 0
for x in red.values:
    c = c+1 if x else 0
    streak.append(c)
df["red_streak"] = streak

# Mayer multiple proxy (Close / 200DMA)
df["mayer"] = df.Close / df.sma200

# --- FORWARD RETURNS ----------------------------------------------------
for h in (30, 60, 90, 180, 365):
    df[f"fwd{h}"] = df.Close.shift(-h) / df.Close - 1
# also: min close over next 60d (drawdown from signal)
df["min60_dd"] = df.Close.rolling(60).min().shift(-60) / df.Close - 1

# --- DEFINE TRIGGERS ----------------------------------------------------
# Each trigger: a boolean series. We dedupe consecutive firings (only first day in a streak).
triggers = {
    "200DMA -25% below":     df.dev200_pct < -25,
    "200DMA -35% below":     df.dev200_pct < -35,
    "200DMA -45% below":     df.dev200_pct < -45,
    "Mayer < 0.8":           df.mayer < 0.80,
    "Mayer < 0.7":           df.mayer < 0.70,
    "Mayer < 0.6":           df.mayer < 0.60,
    "RSI daily < 30":        df.rsi_d < 30,
    "RSI daily < 25":        df.rsi_d < 25,
    "RSI weekly < 35":       df.rsi_w < 35,
    "RSI weekly < 30":       df.rsi_w < 30,
    "RSI monthly < 45":      df.rsi_m < 45,
    "RSI monthly < 40":      df.rsi_m < 40,
    "BB%B < 0.05":           df.bb_pos < 0.05,
    "BB%B < 0":              df.bb_pos < 0,
    "Red streak >= 5":       df.red_streak >= 5,
    "Red streak >= 7":       df.red_streak >= 7,
}

def first_in_streak(s):
    """Keep only the first True in each contiguous True run."""
    s = s.fillna(False).astype(bool)
    return s & ~s.shift(1, fill_value=False)

print(f"\n{'='*100}")
print(f"BTC daily OHLC: {df.index.min().date()} → {df.index.max().date()} ({len(df)} bars)")
print(f"{'='*100}")

rows = []
for name, raw in triggers.items():
    sig = first_in_streak(raw)
    sig_dates = df.index[sig]
    if len(sig_dates) == 0:
        rows.append([name, 0, "-", "-", "-", "-", "-", "-"])
        continue
    fwd30 = df.loc[sig_dates, "fwd30"].dropna()
    fwd90 = df.loc[sig_dates, "fwd90"].dropna()
    fwd180 = df.loc[sig_dates, "fwd180"].dropna()
    dd60 = df.loc[sig_dates, "min60_dd"].dropna()
    rows.append([
        name,
        len(sig_dates),
        f"{fwd30.median()*100:+.1f}%" if len(fwd30) else "-",
        f"{fwd90.median()*100:+.1f}%" if len(fwd90) else "-",
        f"{fwd180.median()*100:+.1f}%" if len(fwd180) else "-",
        f"{(fwd180>0).mean()*100:.0f}%" if len(fwd180) else "-",
        f"{dd60.median()*100:+.1f}%" if len(dd60) else "-",
        sig_dates[0].strftime("%Y-%m-%d") + " … " + sig_dates[-1].strftime("%Y-%m-%d"),
    ])

print("\nSIGNAL TRIGGER HIT-RATE TABLE")
print("="*100)
hdr = ["Signal","N fires","med +30d","med +90d","med +180d","+180d win","med drawdown next 60d","first … last fire"]
widths = [22, 7, 9, 9, 10, 9, 21, 26]
print(" | ".join(h.ljust(w) for h,w in zip(hdr,widths)))
print("-"*sum(widths) + "-"*(len(widths)*3))
for r in rows:
    print(" | ".join(str(x).ljust(w) for x,w in zip(r,widths)))

# --- LOOK AT KNOWN LOW (2022-11-21) and what each signal said ----------
print(f"\n{'='*100}\nWHAT EACH SIGNAL SAID AT THE 2022-11-21 BEAR BOTTOM (Close=${df.loc['2022-11-21','Close']:,.0f})\n{'='*100}")
row = df.loc["2022-11-21"]
print(f"  Close vs 200DMA:  {row.dev200_pct:+.1f}%   (Mayer={row.mayer:.2f})")
print(f"  RSI daily / weekly / monthly: {row.rsi_d:.1f} / {row.rsi_w:.1f} / {row.rsi_m:.1f}")
print(f"  BB %B: {row.bb_pos:.2f}   Red streak: {int(row.red_streak)}")

# --- DEDICATED: dates of all local low-water marks in window -----------
print(f"\n{'='*100}\nALL ≥15% DRAWDOWN LOWS IN WINDOW (close basis, peak→trough)\n{'='*100}")
peaks = df.Close.cummax()
dd = df.Close / peaks - 1
df["dd"] = dd
# find each new cum max segment's trough
segs = []
cur_peak_idx = df.index[0]
cur_peak_val = df.Close.iloc[0]
trough_idx = df.index[0]
trough_val = df.Close.iloc[0]
for i, (dt, c) in enumerate(df.Close.items()):
    if c > cur_peak_val:
        if trough_val < cur_peak_val * 0.85:  # >15% dd
            segs.append((cur_peak_idx, cur_peak_val, trough_idx, trough_val))
        cur_peak_idx, cur_peak_val = dt, c
        trough_idx, trough_val = dt, c
    elif c < trough_val:
        trough_idx, trough_val = dt, c
# tail unfinished segment
if trough_val < cur_peak_val * 0.85:
    segs.append((cur_peak_idx, cur_peak_val, trough_idx, trough_val))

print(f"{'Peak':12} {'Peak $':>10}  {'Trough':12} {'Trough $':>10}  {'DD':>7}  {'days':>5}")
for p_dt, p_v, t_dt, t_v in segs:
    print(f"{p_dt.date()!s:12} ${p_v:>9,.0f}  {t_dt.date()!s:12} ${t_v:>9,.0f}  {(t_v/p_v-1)*100:+6.1f}%  {(t_dt-p_dt).days:>5}")

# --- BEST COMBINED FILTER ---------------------------------------------
print(f"\n{'='*100}\nCOMBINED TRIGGERS (require BOTH conditions)\n{'='*100}")
combos = {
    "Mayer<0.8 AND RSI_w<35":    (df.mayer<0.80) & (df.rsi_w<35),
    "Mayer<0.7 AND RSI_w<30":    (df.mayer<0.70) & (df.rsi_w<30),
    "200DMA<-25% AND BB%B<0":    (df.dev200_pct<-25) & (df.bb_pos<0),
    "Mayer<0.8 AND RSI_m<40":    (df.mayer<0.80) & (df.rsi_m<40),
    "Mayer<0.85 AND red>=5 AND RSI_w<40": (df.mayer<0.85) & (df.red_streak>=5) & (df.rsi_w<40),
}
for name, raw in combos.items():
    sig = first_in_streak(raw)
    sd = df.index[sig]
    if len(sd)==0:
        print(f"  {name:42}  N=0")
        continue
    f180 = df.loc[sd,"fwd180"].dropna()
    f90 = df.loc[sd,"fwd90"].dropna()
    print(f"  {name:42}  N={len(sd):2}  med+90d={f90.median()*100:+.1f}%  med+180d={f180.median()*100:+.1f}%  win180={ (f180>0).mean()*100:.0f}%  dates={[d.strftime('%Y-%m-%d') for d in sd]}")

# also dump the current state
print(f"\n{'='*100}\nCURRENT READING (latest bar {df.index[-1].date()})\n{'='*100}")
cur = df.iloc[-1]
print(f"  Close $              {cur.Close:>12,.0f}")
print(f"  Close vs 200DMA      {cur.dev200_pct:>+12.1f}%   (Mayer={cur.mayer:.2f})")
print(f"  RSI d / w / m        {cur.rsi_d:>5.1f} / {cur.rsi_w:>5.1f} / {cur.rsi_m:>5.1f}")
print(f"  BB %B                {cur.bb_pos:>12.2f}")
print(f"  Drawdown from peak   {cur.dd*100:>+12.1f}%")
print(f"  Red streak           {int(cur.red_streak):>12d}")
