"""
Parameter sensitivity analysis for v13b (50% short cash overlay).

Tests ±1-2 step perturbations on key parameters to detect single-point
fragility. Each cell = geometric mean across PW + W windows.

If heatmap shows smooth gradients → robust strategy.
If best cell is a sharp peak surrounded by cliffs → overfit.
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
# import simulate_v13
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v13_bear_short.py').read().split('print(f"\\n{')[0])

WINDOWS = {
    "PW": ("2018-09-01", "2022-06-30"),
    "W":  ("2022-09-01", "2026-05-21"),
}

def eval_config(**kw):
    """Run simulate_v13 with overrides, return geo mean across PW+W."""
    results = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate_v13(data, **kw)
        results[wn] = r["final"] / INITIAL
    pw, w = results["PW"], results["W"]
    if pw <= 0 or w <= 0:
        return 0, pw, w
    return (pw * w)**0.5, pw, w

# Baseline v13b config
BASELINE = dict(
    enable_short=True, short_cash_pct=0.50, short_stop_pct=0.15,
    long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
    short_lev=1.5, short_layers=4, short_layer_growth=1.3,
    mid_start=12, aggr_start=17, force_exit=19, blackout_end=24,
    next_cycle_start=30, tol=0.03, cooldown=7, lifetime_w=26,
)

print(f"\n{'='*120}")
print(f"PARAMETER SENSITIVITY — v13b baseline (geo mean PW+W = ", end="")
geo_base, pw_base, w_base = eval_config(**BASELINE)
print(f"{geo_base:.2f}x, PW {pw_base:.2f}x, W {w_base:.2f}x)")
print(f"{'='*120}")

# === Study 1: 1D sensitivity for key params ===
print(f"\n{'='*120}\nSTUDY 1: 1D Sensitivity around baseline\n{'='*120}")

studies = [
    ("tol (S/R tolerance %)",      "tol",            [0.015, 0.020, 0.025, 0.030, 0.035, 0.040, 0.050]),
    ("cooldown_days",              "cooldown",       [3, 5, 7, 10, 14, 21]),
    ("lifetime_w (weeks)",         "lifetime_w",     [8, 13, 20, 26, 39, 52]),
    ("N (layers)",                 "short_layers",   [3, 4, 5, 6]),  # for short
    ("layer_growth (1.b)",         "short_layer_growth", [1.0, 1.2, 1.3, 1.5, 1.7, 2.0]),
    ("long_upgrade trigger",       "long_upgrade",   [1.2, 1.3, 1.5, 1.7, 2.0, 2.5]),
    ("mid_start (months)",         "mid_start",      [8, 10, 12, 14, 16]),
    ("aggr_start (months)",        "aggr_start",     [14, 15, 16, 17, 18, 19]),
    ("force_exit (months)",        "force_exit",     [17, 18, 19, 20, 21, 22]),
    ("short_cash_pct",             "short_cash_pct", [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7]),
    ("short_stop_pct",             "short_stop_pct", [0.08, 0.10, 0.15, 0.20, 0.25, 0.30]),
    ("short_lev",                  "short_lev",      [1.0, 1.5, 2.0, 2.5, 3.0]),
]

for label, key, values in studies:
    print(f"\n  {label}  (baseline = {BASELINE[key]}):")
    for v in values:
        cfg = {**BASELINE, key: v}
        # special: changing aggr_start/force_exit/mid_start needs consistency
        if key == "mid_start" and v >= BASELINE["aggr_start"]: continue
        if key == "aggr_start":
            if v <= BASELINE["mid_start"]: continue
            if v >= BASELINE["force_exit"]: continue
        if key == "force_exit":
            if v <= BASELINE["aggr_start"]: continue
            if v >= BASELINE["blackout_end"]: continue
        geo, pw, w = eval_config(**cfg)
        marker = " ⬅ baseline" if v == BASELINE[key] else ""
        delta = (geo / geo_base - 1) * 100 if geo > 0 else -100
        print(f"    {v!s:>8}  geo={geo:>6.2f}x  ({delta:>+5.1f}%)  PW={pw:>5.2f}x  W={w:>5.2f}x{marker}")
