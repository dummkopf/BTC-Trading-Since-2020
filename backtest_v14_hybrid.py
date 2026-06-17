"""
v14: Hybrid — inverted pyramid entry + HODL through bull + halving force exit.

Goal: capture most of 3x-HODL's 28x geo upside while keeping v13's
input-timing tolerance.

Design changes vs v13c-safe:
- KEEP: 5-layer inverted pyramid entry on S/R supports
- KEEP: phased leverage (1.5x → 3x after equity hits 1.5x init)
- KEEP: halving+18 force exit
- KEEP: bear-side short overlay (optional)
- REMOVE: long-side resistance trims during halving+0 to halving+17
- ADD (optional): light "halving+15 to +17" pre-exit trim (30% only)

Variants to test:
  v14a:  pure HODL through bull (no long trims at all, just force exit)
  v14b:  v14a + light pre-exit trim (30% at halving+16 first resistance touch)
  v14c:  v14a + bear short overlay
  v14d:  v14a but start at 2x leverage (skip 1.5x phase since user prefers 2x)
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v13_fixed.py').read().split('print(f"\\n{')[0])

INITIAL = 7000.0; FUNDING_DAILY_LONG = 0.0001*3
FUNDING_DAILY_SHORT = -0.0001*3
FEE = 0.0005; MM = 0.005

WINDOWS = {
    "PW": ("2018-09-01", "2022-06-30", "2018-12-15"),
    "W":  ("2022-09-01", "2026-05-21", "2022-11-21"),
}

def simulate_v14(data, enable_short=False,
                 long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
                 short_lev=2.0, short_cash_pct=0.50, short_layers=4,
                 short_layer_growth=1.3, short_stop_pct=0.30,
                 # HODL trim controls
                 trim_during_bull=False, pre_exit_trim_month=16, pre_exit_trim_pct=30,
                 # Standard params
                 force_exit=18, blackout_end=24, next_cycle_start=30,
                 tol=0.03, cooldown=3, lifetime_w=26, N=5,
                 layer_growth=1.5):
    """
    v14: inverted pyramid entry, then HODL through bull, force exit at halving+force_exit.
    """
    L_cash = INITIAL; L_pos = 0.0; L_avg = 0.0
    L_layer = 0; L_l1_size = 0
    L_buys = L_trims = L_force = 0
    L_support_last = {}; L_resist_last = {}
    cur_max_lev = long_lev_low; peak_eq_mult = 1.0
    force_exit_done_for = None
    pre_exit_trim_done_for = None
    S_cash = 0.0; S_pos_qty = 0.0; S_avg = 0.0
    S_layer = 0; S_l1_size = 0
    S_shorts = S_covers = S_stops = 0
    S_resist_last = {}; S_support_last = {}
    short_cycle_active = False
    last_cover_price = None
    liq = False
    eq_curve = []
    realized_long_t = realized_short_t = 0.0
    lifetime_days = lifetime_w * 7

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq: eq_curve.append(0); continue
        mh = months_since_last_halving(dt)
        past_h = [h for h in HALVINGS if h <= dt]
        cur_halving = past_h[-1] if past_h else None
        in_bear_window = (force_exit <= mh < next_cycle_start)

        # Long funding + liq
        if L_pos > 0:
            f = L_pos * r.Close * FUNDING_DAILY_LONG
            L_cash -= f
            eq_low = L_cash + L_pos*(r.Low - L_avg); mm_req = L_pos*r.Low*MM
            if eq_low <= mm_req:
                L_cash = 0; L_pos = 0; L_avg = 0; L_layer = 0; L_l1_size = 0

        # Short funding + liq + stop
        if S_pos_qty > 0:
            f = S_pos_qty * r.Close * abs(FUNDING_DAILY_SHORT) * 0.5
            S_cash -= f
            eq_low_short = S_cash + S_pos_qty * (S_avg - r.High)
            short_mm_req = S_pos_qty * r.High * MM
            if eq_low_short <= short_mm_req:
                S_cash = 0; S_pos_qty = 0; S_avg = 0; S_layer = 0; S_l1_size = 0
                S_stops += 1
            adverse_pct = (r.High - S_avg) / S_avg if S_avg > 0 else 0
            if S_pos_qty > 0 and adverse_pct >= short_stop_pct:
                stop_price = S_avg * (1 + short_stop_pct)
                pnl = S_pos_qty * (S_avg - stop_price) - S_pos_qty * stop_price * FEE
                S_cash += pnl
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0; S_layer = 0; S_l1_size = 0
                S_stops += 1
                S_resist_last.clear(); S_support_last.clear()

        # Bear window allocation
        if enable_short and in_bear_window and not short_cycle_active and L_cash > 0:
            allocation = L_cash * short_cash_pct
            S_cash = allocation; L_cash -= allocation; short_cycle_active = True

        if not in_bear_window and short_cycle_active:
            if S_pos_qty > 0:
                pnl = S_pos_qty * (S_avg - r.Close) - S_pos_qty * r.Close * FEE
                S_cash += pnl
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0
            L_cash += S_cash; S_cash = 0
            S_layer = 0; S_l1_size = 0; last_cover_price = None
            short_cycle_active = False

        long_equity = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_equity = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        total_equity = long_equity + short_equity
        peak_eq_mult = max(peak_eq_mult, total_equity / INITIAL)
        cur_max_lev = long_lev_high if peak_eq_mult > long_upgrade else long_lev_low

        # === FORCE EXIT (long) ===
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

        # === PRE-EXIT LIGHT TRIM (only if trim_during_bull=False)===
        # This is the ONLY trim allowed when HODL mode is on
        if (not trim_during_bull and L_pos > 0 and r.Close > L_avg
            and mh >= pre_exit_trim_month and pre_exit_trim_done_for != cur_halving):
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol:
                    btc_sell = L_pos * (pre_exit_trim_pct/100); fee = btc_sell*r.Close*FEE
                    L_cash += btc_sell*(r.Close - L_avg) - fee
                    realized_long_t += btc_sell*(r.Close - L_avg) - fee
                    L_pos -= btc_sell
                    if L_pos < 1e-9: L_pos = 0; L_avg = 0
                    L_trims += 1
                    pre_exit_trim_done_for = cur_halving
                    L_layer = 0; L_l1_size = 0
                    break

        # === LONG TRIM during bull (only if trim_during_bull=True; v13-style) ===
        if trim_during_bull and L_pos > 0 and r.Close > L_avg:
            if mh >= 17: trim_pct = 60
            elif mh >= 12: trim_pct = 33
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

        # === LONG BUY ===
        in_long_blackout = (force_exit - 1 <= mh < next_cycle_start)  # no new buys near top
        if not in_long_blackout and L_layer < N and not liq:
            if long_equity > 0:
                for lvl in sups:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = L_support_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            if L_layer == 0:
                                geo_sum = sum(layer_growth**k for k in range(N))
                                L_l1_size = long_equity * cur_max_lev / geo_sum
                                size = L_l1_size
                            else:
                                size = L_l1_size * (layer_growth)**L_layer
                            size = min(size, max(0, cur_max_lev*long_equity - L_pos*r.Close))
                            if size > 1:
                                btc = size / r.Close; fee = size*FEE; L_cash -= fee
                                new_pos = L_pos + btc
                                L_avg = (L_pos*L_avg + btc*r.Close)/new_pos if L_pos > 0 else r.Close
                                L_pos = new_pos
                                L_layer += 1; L_buys += 1
                                L_support_last[lvl] = dt
                            break

        # === SHORT ENTRY ===
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

        # === SHORT COVER ===
        if enable_short and short_cycle_active and S_pos_qty > 0 and r.Close < S_avg:
            for lvl in sups:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = S_support_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        cover_qty = S_pos_qty * 0.33
                        fee = cover_qty * r.Close * FEE
                        pnl = cover_qty*(S_avg - r.Close) - fee
                        S_cash += pnl
                        realized_short_t += pnl
                        S_pos_qty -= cover_qty
                        if S_pos_qty < 1e-9: S_pos_qty = 0; S_avg = 0
                        S_covers += 1; S_support_last[lvl] = dt
                        S_layer = 0; S_l1_size = 0
                        last_cover_price = r.Close
                        break

        long_eq_now = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_eq_now = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        eq_curve.append(long_eq_now + short_eq_now)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(final=final, max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0,
                L_buys=L_buys, L_trims=L_trims, L_force=L_force,
                S_shorts=S_shorts, S_covers=S_covers, S_stops=S_stops,
                realized_long=realized_long_t, realized_short=realized_short_t)

print(f"\n{'='*135}\nv14 HYBRID — Inverted pyramid entry + HODL through bull + halving force exit\n{'='*135}\n")
print(f"  {'Strategy':56}  {'PW 倍数':>9}  {'W 倍数':>9}  {'Geo':>7}  {'PW MDD':>7}  {'W MDD':>7}")
print(f"  {'-'*56}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*7}  {'-'*7}")

configs = [
    # Baselines for reference
    ("REF: v13c-safe (FIXED, with trims)",
     dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
          long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
          short_lev=2.0, short_layers=4, short_layer_growth=1.3,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=True, pre_exit_trim_pct=0)),

    # v14 pure HODL variants (no long trims)
    ("v14a: HODL no trim, 1.5→3x phased, no short",
     dict(enable_short=False, long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_pct=0,
          short_lev=2.0, short_layers=4, short_cash_pct=0.50,
          short_stop_pct=0.30, short_layer_growth=1.3)),

    ("v14a + short overlay",
     dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
          long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
          short_lev=2.0, short_layers=4, short_layer_growth=1.3,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_pct=0)),

    ("v14b: HODL + light pre-exit trim 30% at +16mo",
     dict(enable_short=False, long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_month=16, pre_exit_trim_pct=30,
          short_lev=2.0, short_layers=4, short_cash_pct=0.50,
          short_stop_pct=0.30, short_layer_growth=1.3)),

    ("v14b + short overlay",
     dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
          long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
          short_lev=2.0, short_layers=4, short_layer_growth=1.3,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_month=16, pre_exit_trim_pct=30)),

    # v14d: 2x start (no 1.5x phase)
    ("v14d: 2x→3x phased, HODL no trim, no short",
     dict(enable_short=False, long_lev_low=2.0, long_lev_high=3.0, long_upgrade=1.5,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_pct=0,
          short_lev=2.0, short_layers=4, short_cash_pct=0.50,
          short_stop_pct=0.30, short_layer_growth=1.3)),

    ("v14d + short overlay",
     dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
          long_lev_low=2.0, long_lev_high=3.0, long_upgrade=1.5,
          short_lev=2.0, short_layers=4, short_layer_growth=1.3,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_pct=0)),

    # Aggressive: 3x from day 1
    ("v14e: 3x full leverage from L1 (most aggressive)",
     dict(enable_short=False, long_lev_low=3.0, long_lev_high=3.0, long_upgrade=999,
          force_exit=18, blackout_end=24, next_cycle_start=30,
          tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
          trim_during_bull=False, pre_exit_trim_pct=0,
          short_lev=2.0, short_layers=4, short_cash_pct=0.50,
          short_stop_pct=0.30, short_layer_growth=1.3)),
]

best_geo = 0; best_cfg = None
for label, cfg in configs:
    results = {}
    for wn, (s, e, bottom) in WINDOWS.items():
        data = df.loc[s:e]
        r = simulate_v14(data, **cfg)
        results[wn] = (r["final"]/INITIAL, r["max_dd"], r["L_buys"], r["L_trims"], r["L_force"], r["S_shorts"], r["S_covers"])
    pw, pw_mdd, pw_b, pw_t, pw_f, pw_sh, pw_cv = results["PW"]
    w, w_mdd, w_b, w_t, w_f, w_sh, w_cv = results["W"]
    geo = (pw*w)**0.5 if pw > 0 and w > 0 else 0
    pw_s = f"{pw:>7.2f}x" + ('!' if pw == 0 else ' ')
    w_s = f"{w:>7.2f}x" + ('!' if w == 0 else ' ')
    geo_s = f"{geo:>5.2f}x" if geo > 0 else "  LIQ"
    print(f"  {label:56}  {pw_s:>10}  {w_s:>10}  {geo_s:>7}  {pw_mdd*100:>+6.0f}%  {w_mdd*100:>+6.0f}%")
    if geo > best_geo:
        best_geo = geo; best_cfg = (label, results)

print(f"\nBest by geo: {best_cfg[0] if best_cfg else 'none'}")
print(f"\nReference benchmarks (from earlier):")
print(f"  3x HODL from bottom (perfect timing):   PW 52.25x, W 15.26x, Geo 28.24x")
print(f"  v13c-safe with short (older bug not v13c):  was 51x (BUG), now 7.52x")
print(f"  Spot HODL from cycle bottom:            PW 18.08x, W 5.75x, Geo 10.20x")
