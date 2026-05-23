"""
v15: explore more alpha to approach 3x-HODL's 28x geometric mean.

Hypothesis space:
  H1. Higher leverage cap (3x → 4x or 5x in late bull)
  H2. Aggressive layer growth (1.5 → 2.0 or 2.5) puts more capital at lower prices
  H3. Bigger short allocation (50% → 70%)
  H4. "Confluence concentrate": when Mayer<0.7 AND RSI_w<35 fires (bottom signal),
       deploy 30-40% in single L1 instead of spreading 5 layers
  H5. Adaptive force exit: cover at first of (halving+18, RSI_w>80 sustained, Mayer>1.8)
  H6. Re-entry post force exit if Bull extends beyond +18mo
  H7. Combine best of above

Each tested against v14b+ baseline (12.55x geo).
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v14_hybrid.py').read().split('print(f"\\n{')[0])

INITIAL = 7000.0; FUNDING_DAILY_LONG = 0.0001*3
FUNDING_DAILY_SHORT = -0.0001*3
FEE = 0.0005; MM = 0.005

WINDOWS = {"PW": ("2018-09-01", "2022-06-30"), "W": ("2022-09-01", "2026-05-21")}

# Reuse simulate_v14 from v14 file
def quick_run(label, **cfg):
    """Run v14 with overrides and print results."""
    defaults = dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
        long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
        short_lev=2.0, short_layers=4, short_layer_growth=1.3,
        force_exit=18, blackout_end=24, next_cycle_start=30,
        tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
        trim_during_bull=False, pre_exit_trim_month=14, pre_exit_trim_pct=20)
    defaults.update(cfg)
    results = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate_v14(data, **defaults)
        results[wn] = (r["final"]/INITIAL, r["max_dd"])
    pw, pw_dd = results["PW"]; w, w_dd = results["W"]
    geo = (pw*w)**0.5 if pw>0 and w>0 else 0
    pw_s = f"{pw:>6.2f}x" + ('!' if pw==0 else '')
    w_s = f"{w:>6.2f}x" + ('!' if w==0 else '')
    geo_s = f"{geo:>5.2f}x" if geo>0 else " LIQ"
    print(f"  {label:55}  {pw_s:>8}  {w_s:>8}  {geo_s:>6}  {pw_dd*100:>+6.0f}%  {w_dd*100:>+6.0f}%")

print(f"\n{'='*120}\n{'探索更多 alpha — 各方向单独测试'}\n{'='*120}\n")
print(f"  {'Config':55}  {'PW':>8}  {'W':>8}  {'Geo':>6}  {'PW MDD':>7}  {'W MDD':>7}")
print(f"  {'-'*55}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*7}  {'-'*7}")

print(f"\n  [BASELINE]")
quick_run("v14b+ baseline (+14mo trim 20%, short 50%)")

print(f"\n  [H1: 更高杠杆上限]")
quick_run("phased 1.5→4x (lev_high=4.0)", long_lev_high=4.0)
quick_run("phased 1.5→5x (lev_high=5.0)", long_lev_high=5.0)
quick_run("phased 2x→4x", long_lev_low=2.0, long_lev_high=4.0)
quick_run("phased 2x→4x, upgrade @ 2x", long_lev_low=2.0, long_lev_high=4.0, long_upgrade=2.0)

print(f"\n  [H2: 激进 layer growth]")
quick_run("layer_growth 1.7", layer_growth=1.7)
quick_run("layer_growth 2.0", layer_growth=2.0)
quick_run("layer_growth 2.5", layer_growth=2.5)
quick_run("4 layers, growth 2.0", N=4, layer_growth=2.0)
quick_run("6 layers, growth 1.5", N=6, layer_growth=1.5)

print(f"\n  [H3: 更大 short 配比]")
quick_run("short 70% cash", short_cash_pct=0.70)
quick_run("short 80% cash", short_cash_pct=0.80)
quick_run("short 70% + lev 2.5", short_cash_pct=0.70, short_lev=2.5)

print(f"\n  [组合]")
quick_run("4x lev + growth 2.0", long_lev_high=4.0, layer_growth=2.0)
quick_run("4x lev + short 70%", long_lev_high=4.0, short_cash_pct=0.70)
quick_run("4x lev + growth 2.0 + short 70%", long_lev_high=4.0, layer_growth=2.0, short_cash_pct=0.70)
quick_run("4x lev + growth 1.7 + trim @+12mo 15%", long_lev_high=4.0, layer_growth=1.7,
          pre_exit_trim_month=12, pre_exit_trim_pct=15)
quick_run("5x lev + growth 2.0 + short 70%", long_lev_high=5.0, layer_growth=2.0, short_cash_pct=0.70)

print(f"\n  [边界对照 — 完美底部 3x HODL]")
print(f"  3x HODL 完美底 + halving+18 exit                          52.25x   15.26x  28.24x    -83%     -39%")
print(f"  4x HODL 完美底 + halving+18 exit                          (theoretical, likely LIQ)")
print(f"  2x HODL 完美底 + halving+18 exit                          35.17x   10.51x  19.22x    -79%     -37%")
