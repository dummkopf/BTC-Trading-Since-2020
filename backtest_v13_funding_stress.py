"""
Funding rate stress test for v13c-safe.

Baseline assumed: 0.01% / 8h = 0.03% / day = ~11% annualized
Historical reality:
  2018 bear:  -0.005 ~ +0.01% / 8h (low/negative)
  2020-21 bull: 0.01-0.05% / 8h (1-5x baseline)
  2024 bull:  0.02-0.04% / 8h (2-4x baseline)
  Top blowoffs: 0.10-0.30% / 8h (10-30x baseline!)

Test multipliers on FUNDING_DAILY:
  0.5x (calm bull, like 2018-19 recovery)
  1.0x (baseline assumption)
  2.0x (mid bull)
  3.0x (active bull)
  5.0x (heated bull)
  10x (top blowoff zone — only during halving + 15-19mo)

Each multiplier applied uniformly across long position.
Short position funding also scaled (we conservatively assumed half rate
for shorts; will scale that too).
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v13_bear_short.py').read().split('print(f"\\n{')[0])

WINDOWS = {"PW": ("2018-09-01", "2022-06-30"), "W": ("2022-09-01", "2026-05-21")}

# v13c-safe baseline config
V13C_SAFE = dict(
    enable_short=True, short_cash_pct=0.50, short_stop_pct=0.15,
    long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
    short_lev=2.0, short_layers=4, short_layer_growth=1.3,
    mid_start=12, aggr_start=17, force_exit=18, blackout_end=24,
    next_cycle_start=30, tol=0.03, cooldown=3, lifetime_w=26,
)

def run_with_funding_mult(mult, **cfg):
    """Run simulate_v13 with funding rate multiplier."""
    # Modify globals before calling
    import backtest_v13_bear_short as m
    orig_long = 0.0001 * 3
    orig_short = -0.0001 * 3
    m.FUNDING_DAILY_LONG = orig_long * mult
    m.FUNDING_DAILY_SHORT = orig_short * mult

    # We need to re-exec or modify in current namespace
    global FUNDING_DAILY_LONG, FUNDING_DAILY_SHORT
    FUNDING_DAILY_LONG = orig_long * mult
    FUNDING_DAILY_SHORT = orig_short * mult

    results = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate_v13(data, **cfg)
        results[wn] = (r["final"]/INITIAL, r["funding_long"], r["funding_short"], r["realized_long"], r["realized_short"], r["L_buys"]+r["L_trims"]+r["S_shorts"]+r["S_covers"])
    return results

# Since the simulate function uses module-level FUNDING_DAILY_LONG/SHORT,
# we need to patch them. Let me rewrite the test to override via a wrapper.

# Actually simpler: re-define simulate locally with funding as parameter
print(f"\n{'='*135}\nFUNDING RATE STRESS TEST (v13c-safe baseline)")
print(f"{'='*135}")
print(f"\nBaseline assumption: 0.01% / 8h on long, ~neutral on short")
print(f"Multipliers applied to BOTH long-paid and short-paid/received funding\n")

multipliers = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]

# Rewrite simulate with explicit funding params
def simulate_v13_with_funding(data, fund_long_daily, fund_short_daily, **kw):
    """Same as simulate_v13 but with explicit funding rates."""
    enable_short = kw.get('enable_short', True)
    long_lev_low = kw.get('long_lev_low', 1.5)
    long_lev_high = kw.get('long_lev_high', 3.0)
    long_upgrade = kw.get('long_upgrade', 1.5)
    short_lev = kw.get('short_lev', 1.5)
    short_cash_pct = kw.get('short_cash_pct', 0.30)
    short_layers = kw.get('short_layers', 4)
    short_layer_growth = kw.get('short_layer_growth', 1.3)
    short_stop_pct = kw.get('short_stop_pct', 0.15)
    mid_start = kw.get('mid_start', 12)
    aggr_start = kw.get('aggr_start', 17)
    force_exit = kw.get('force_exit', 19)
    blackout_end = kw.get('blackout_end', 24)
    next_cycle_start = kw.get('next_cycle_start', 30)
    tol = kw.get('tol', 0.03)
    cooldown = kw.get('cooldown', 7)
    lifetime_w = kw.get('lifetime_w', 26)

    L_cash = INITIAL; L_pos = 0.0; L_avg = 0.0
    L_layer = 0; L_l1_size = 0
    L_buys = L_trims = L_force = 0
    L_support_last = {}; L_resist_last = {}
    cur_max_lev = long_lev_low; peak_eq_mult = 1.0
    force_exit_done_for = None
    S_cash = 0.0; S_pos_qty = 0.0; S_avg = 0.0
    S_layer = 0; S_l1_size = 0
    S_shorts = S_covers = S_stops = 0
    S_resist_last = {}; S_support_last = {}
    short_cycle_active = False
    liq = False
    eq_curve = []
    fees_t = funding_long_t = funding_short_t = 0.0
    realized_long_t = realized_short_t = 0.0
    lifetime_days = lifetime_w * 7

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq: eq_curve.append(0); continue
        mh = months_since_last_halving(dt)
        past_h = [h for h in HALVINGS if h <= dt]
        cur_halving = past_h[-1] if past_h else None
        in_bear_window = (force_exit <= mh < next_cycle_start)

        if L_pos > 0:
            f = L_pos * r.Close * fund_long_daily
            L_cash -= f; funding_long_t += f
            eq_low = L_cash + L_pos*(r.Low - L_avg); mm_req = L_pos*r.Low*MM
            if eq_low <= mm_req:
                L_cash = 0; L_pos = 0; L_avg = 0; L_layer = 0; L_l1_size = 0

        if S_pos_qty > 0:
            f = S_pos_qty * r.Close * abs(fund_short_daily) * 0.5
            S_cash -= f; funding_short_t += f
            eq_low_short = S_cash + S_pos_qty * (S_avg - r.High)
            short_mm_req = S_pos_qty * r.High * MM
            if eq_low_short <= short_mm_req:
                S_cash = 0; S_pos_qty = 0; S_avg = 0; S_layer = 0; S_l1_size = 0
                S_stops += 1
            adverse_pct = (r.High - S_avg) / S_avg if S_avg > 0 else 0
            if S_pos_qty > 0 and adverse_pct >= short_stop_pct:
                stop_price = S_avg * (1 + short_stop_pct)
                pnl = S_pos_qty * (S_avg - stop_price) - S_pos_qty * stop_price * FEE
                S_cash += S_pos_qty * S_avg + pnl
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0; S_layer = 0; S_l1_size = 0
                S_stops += 1
                S_resist_last.clear(); S_support_last.clear()

        if enable_short and in_bear_window and not short_cycle_active and L_cash > 0:
            allocation = L_cash * short_cash_pct
            S_cash = allocation; L_cash -= allocation; short_cycle_active = True

        if not in_bear_window and short_cycle_active:
            if S_pos_qty > 0:
                pnl = S_pos_qty * (S_avg - r.Close) - S_pos_qty * r.Close * FEE
                S_cash += S_pos_qty * S_avg + pnl
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0
            L_cash += S_cash; S_cash = 0
            S_layer = 0; S_l1_size = 0
            S_resist_last.clear(); S_support_last.clear()
            short_cycle_active = False

        long_equity = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_equity = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        total_equity = long_equity + short_equity
        peak_eq_mult = max(peak_eq_mult, total_equity / INITIAL)
        cur_max_lev = long_lev_high if peak_eq_mult > long_upgrade else long_lev_low

        if L_pos > 0 and force_exit <= mh < blackout_end and force_exit_done_for != cur_halving:
            fee = L_pos * r.Close * FEE
            L_cash += L_pos*(r.Close - L_avg) - fee
            L_pos = 0; L_avg = 0; L_layer = 0; L_l1_size = 0
            L_force += 1; force_exit_done_for = cur_halving
            L_support_last.clear(); L_resist_last.clear()

        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values

        if L_pos > 0 and r.Close > L_avg:
            if mh >= aggr_start: trim_pct = 60
            elif mh >= mid_start: trim_pct = 33
            else: trim_pct = 20
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = L_resist_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        btc_sell = L_pos * (trim_pct/100); fee = btc_sell*r.Close*FEE
                        L_cash += btc_sell*(r.Close - L_avg) - fee
                        realized_long_t += btc_sell*(r.Close - L_avg) - fee
                        L_pos -= btc_sell
                        if L_pos < 1e-9: L_pos = 0; L_avg = 0
                        L_trims += 1; L_resist_last[lvl] = dt
                        L_layer = 0; L_l1_size = 0
                        break

        in_long_blackout = (aggr_start <= mh < next_cycle_start)
        if not in_long_blackout and L_layer < 5 and not liq:
            if long_equity > 0:
                for lvl in sups:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = L_support_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            if L_layer == 0:
                                geo_sum = sum(1.5**k for k in range(5))
                                L_l1_size = long_equity * cur_max_lev / geo_sum
                                size = L_l1_size
                            else:
                                size = L_l1_size * (1.5)**L_layer
                            size = min(size, max(0, cur_max_lev*long_equity - L_pos*r.Close))
                            if size > 1:
                                btc = size / r.Close; fee = size*FEE; L_cash -= fee
                                new_pos = L_pos + btc
                                L_avg = (L_pos*L_avg + btc*r.Close)/new_pos if L_pos > 0 else r.Close
                                L_pos = new_pos
                                L_layer += 1; L_buys += 1
                                L_support_last[lvl] = dt
                            break

        if enable_short and short_cycle_active and S_layer < short_layers and not liq:
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol and r.Close <= lvl * (1 + tol):
                    last = S_resist_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        if S_layer == 0:
                            geo_sum = sum(short_layer_growth**k for k in range(short_layers))
                            S_l1_size = S_cash * short_lev / geo_sum
                            size = S_l1_size
                        else:
                            size = S_l1_size * (short_layer_growth)**S_layer
                        size = min(size, max(0, short_lev*S_cash - S_pos_qty*r.Close))
                        if size > 1:
                            btc = size / r.Close; fee = size*FEE; S_cash -= fee
                            new_qty = S_pos_qty + btc
                            S_avg = (S_pos_qty*S_avg + btc*r.Close)/new_qty if S_pos_qty > 0 else r.Close
                            S_pos_qty = new_qty
                            S_layer += 1; S_shorts += 1
                            S_resist_last[lvl] = dt
                        break

        if enable_short and short_cycle_active and S_pos_qty > 0 and r.Close < S_avg:
            for lvl in sups:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = S_support_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        cover_qty = S_pos_qty * 0.33
                        fee = cover_qty * r.Close * FEE
                        pnl = cover_qty*(S_avg - r.Close) - fee
                        S_cash += cover_qty * S_avg + pnl
                        realized_short_t += pnl
                        S_pos_qty -= cover_qty
                        if S_pos_qty < 1e-9: S_pos_qty = 0; S_avg = 0
                        S_covers += 1; S_support_last[lvl] = dt
                        S_layer = 0; S_l1_size = 0
                        break

        long_eq_now = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_eq_now = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        eq_curve.append(long_eq_now + short_eq_now)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(final=final, funding_long=funding_long_t, funding_short=funding_short_t,
                realized_long=realized_long_t, realized_short=realized_short_t,
                S_stops=S_stops)

print(f"  {'Mult':>5} {'描述':35} {'PW 倍数':>9} {'W 倍数':>9} {'Geo':>7}  {'Long 资金费':>13} {'Short 资金费':>13}")
print(f"  {'-'*5} {'-'*35} {'-'*9} {'-'*9} {'-'*7}  {'-'*13} {'-'*13}")

baseline_long = 0.0001 * 3   # 0.03%/day
baseline_short = -0.0001 * 3
labels = ["2018 calm bear", "baseline (~11%/yr)", "mid bull", "2024-style bull", "active bull", "heated bull", "top blowoff"]

for mult, label in zip(multipliers, labels):
    fund_l = baseline_long * mult
    fund_s = baseline_short * mult
    results = {}
    total_fl = 0; total_fs = 0
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate_v13_with_funding(data, fund_l, fund_s, **V13C_SAFE)
        results[wn] = r["final"]/INITIAL
        total_fl += r["funding_long"]
        total_fs += r["funding_short"]
    pw = results["PW"]; w = results["W"]
    geo = (pw*w)**0.5 if pw>0 and w>0 else 0
    avg_fund_l = total_fl / 2
    avg_fund_s = total_fs / 2
    annual_fund_pct = mult * 11
    print(f"  {mult:>4.1f}x {label:35} {pw:>7.2f}x  {w:>7.2f}x  {geo:>6.2f}x  ${avg_fund_l:>10,.0f}  ${avg_fund_s:>10,.0f}  (yr~{annual_fund_pct:.0f}%)")

print(f"\n{'='*135}\n按场景假设 — 实际 funding 可能不是常数:")
print(f"  早期 bull (halving-12 to halving):  ~1x baseline")
print(f"  中期 bull (halving to halving+12mo): ~2-3x baseline")
print(f"  末期 bull (halving+12 to +18mo):     ~5-10x baseline (peak FOMO)")
print(f"  熊市 (halving+19 to next bottom):    ~0.5x baseline (有时 negative)")
