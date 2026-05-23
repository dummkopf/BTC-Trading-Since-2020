"""
v13d: v13c-safe + protections against the "avg cost spiral" risk.

Protections:
1. short_stop_pct widened to 0.30 (was 0.15) — more buffer for V-shape reversals
2. post_cover_cooldown_pct = 10% — after covering, price must rally >=10% from
   cover_price before allowing a new short L1 entry (prevents averaging-down spiral)
3. Optional: max_short_cycles_per_halving — cap re-entries per bear window

Also: HONEST benchmark vs "3x HODL from cycle bottom with halving force exit".
The user's question: is all this complexity actually better than a simple
"buy at bottom 3x, sell at halving+18mo"?
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v13_fixed.py').read().split('print(f"\\n{')[0])

INITIAL = 7000.0
FUNDING_DAILY = 0.0001 * 3
FEE = 0.0005
MM = 0.005

WINDOWS = {
    "PW": ("2018-09-01", "2022-06-30", "2018-12-15"),  # start, end, cycle_bottom
    "W":  ("2022-09-01", "2026-05-21", "2022-11-21"),
}

# ============================================================
# Implementation of v13d with cover cooldown
# ============================================================
def simulate_v13d(data, post_cover_rally_required=0.10, max_short_cycles_per_bear=999, **kw):
    """v13c-safe with anti-spiral protections."""
    enable_short = kw.get('enable_short', True)
    long_lev_low = kw.get('long_lev_low', 1.5)
    long_lev_high = kw.get('long_lev_high', 3.0)
    long_upgrade = kw.get('long_upgrade', 1.5)
    short_lev = kw.get('short_lev', 2.0)
    short_cash_pct = kw.get('short_cash_pct', 0.50)
    short_layers = kw.get('short_layers', 4)
    short_layer_growth = kw.get('short_layer_growth', 1.3)
    short_stop_pct = kw.get('short_stop_pct', 0.30)
    mid_start = kw.get('mid_start', 12)
    aggr_start = kw.get('aggr_start', 17)
    force_exit = kw.get('force_exit', 18)
    blackout_end = kw.get('blackout_end', 24)
    next_cycle_start = kw.get('next_cycle_start', 30)
    tol = kw.get('tol', 0.03)
    cooldown = kw.get('cooldown', 3)
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
    last_cover_price = None        # NEW: track last cover price
    short_cycles_count = 0         # NEW: count cycle restarts in this bear
    short_cycles_for_bear = None   # NEW: track which halving's bear

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

        # reset cycle counter when we enter a new bear window
        if in_bear_window and short_cycles_for_bear != cur_halving:
            short_cycles_for_bear = cur_halving
            short_cycles_count = 0

        if L_pos > 0:
            f = L_pos * r.Close * FUNDING_DAILY
            L_cash -= f
            eq_low = L_cash + L_pos*(r.Low - L_avg); mm_req = L_pos*r.Low*MM
            if eq_low <= mm_req:
                L_cash = 0; L_pos = 0; L_avg = 0; L_layer = 0; L_l1_size = 0

        if S_pos_qty > 0:
            f = S_pos_qty * r.Close * FUNDING_DAILY * 0.5  # half rate, neutral assumption
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

        # LONG TRIM
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

        # LONG BUY
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

        # SHORT ENTRY (with NEW protections)
        if enable_short and short_cycle_active and S_layer < short_layers and not liq:
            # Protection 1: cycle count limit
            if short_cycles_count >= max_short_cycles_per_bear:
                pass  # skip
            else:
                for lvl in ress:
                    if abs(r.Close - lvl)/lvl <= tol and r.Close <= lvl * (1 + tol):
                        last = S_resist_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            # Protection 2: if this is a NEW cycle (S_layer == 0), require last_cover_price recovery
                            if S_layer == 0 and last_cover_price is not None:
                                if r.Close < last_cover_price * (1 + post_cover_rally_required):
                                    # price hasn't rallied enough since last cover, skip
                                    break
                            if S_layer == 0:
                                geo_sum = sum(short_layer_growth**k for k in range(short_layers))
                                S_l1_size = S_cash * short_lev / geo_sum
                                size = S_l1_size
                                short_cycles_count += 1  # count new cycle
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

        # SHORT COVER
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
                        last_cover_price = r.Close  # NEW: track cover price
                        break

        long_eq_now = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_eq_now = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        eq_curve.append(long_eq_now + short_eq_now)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(final=final, S_stops=S_stops, S_shorts=S_shorts, S_covers=S_covers,
                realized_long=realized_long_t, realized_short=realized_short_t,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)


# ============================================================
# Simple "3x HODL from cycle bottom + halving force exit"
# ============================================================
def simple_3x_hodl(data, cycle_bottom_date, force_exit_months=18):
    """Buy 3x at first day price >= cycle bottom date, hold until halving+force_exit_months."""
    cash = INITIAL
    pos = 0.0
    avg = 0.0
    n_pos = 0
    liq = False
    eq_curve = []
    entered = False
    force_exited = False

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq:
            eq_curve.append(0); continue

        # Entry: on or after cycle_bottom_date
        if not entered and dt >= cycle_bottom_date:
            pos = INITIAL * 3 / r.Close
            avg = r.Close
            cash -= INITIAL * 3 * FEE
            entered = True

        # Funding + liquidation
        if pos > 0 and not force_exited:
            cash -= pos * r.Close * FUNDING_DAILY
            eq_low = cash + pos*(r.Low - avg)
            mm_req = pos*r.Low*MM
            if eq_low <= mm_req:
                liq = True
                cash = 0; pos = 0; avg = 0
                eq_curve.append(0); continue

        # Force exit at halving + force_exit_months
        if pos > 0 and not force_exited:
            mh = months_since_last_halving(dt)
            if mh >= force_exit_months:
                # close
                fee = pos * r.Close * FEE
                cash += pos*(r.Close - avg) - fee
                pos = 0; avg = 0
                force_exited = True

        eq = cash + pos*(r.Close - avg) if pos > 0 else cash
        eq_curve.append(eq)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(final=final, liq=liq,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)


# ============================================================
# Run all comparisons
# ============================================================
print(f"\n{'='*135}\nFINAL HONEST COMPARISON (post-bug-fix)")
print(f"{'='*135}\n")

print(f"  {'Strategy':50}  {'PW':>9}  {'W':>9}  {'Geo':>6}  {'PW MDD':>7}  {'W MDD':>7}")
print(f"  {'-'*50}  {'-'*9}  {'-'*9}  {'-'*6}  {'-'*7}  {'-'*7}")

# Strategies to compare
strategies = [
    # Simple benchmarks
    ("Spot HODL from window start", "spot", None),
    ("3x HODL from window start (no exit)", "hodl_window", 3.0),
    ("3x HODL from CYCLE BOTTOM + halving+18 exit", "hodl_bottom", 3.0),
    ("2x HODL from CYCLE BOTTOM + halving+18 exit", "hodl_bottom", 2.0),
    ("1.5x HODL from CYCLE BOTTOM + halving+18 exit", "hodl_bottom", 1.5),
    # v13 variants (post-fix)
    ("v13b (FIXED)", "v13", dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.15,
        long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
        short_lev=1.5, short_layers=4, short_layer_growth=1.3,
        mid_start=12, aggr_start=17, force_exit=19, blackout_end=24,
        next_cycle_start=30, tol=0.03, cooldown=7, lifetime_w=26)),
    ("v13c-safe (FIXED)", "v13", dict(enable_short=True, short_cash_pct=0.50, short_stop_pct=0.15,
        long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
        short_lev=2.0, short_layers=4, short_layer_growth=1.3,
        mid_start=12, aggr_start=17, force_exit=18, blackout_end=24,
        next_cycle_start=30, tol=0.03, cooldown=3, lifetime_w=26)),
    ("v13d (FIXED + stop 30% + cover cooldown)", "v13d", dict(
        post_cover_rally_required=0.10, max_short_cycles_per_bear=999,
        enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
        short_lev=2.0, short_layers=4, force_exit=18, cooldown=3)),
    ("v13d-strict (max 3 cycles per bear)", "v13d", dict(
        post_cover_rally_required=0.10, max_short_cycles_per_bear=3,
        enable_short=True, short_cash_pct=0.50, short_stop_pct=0.30,
        short_lev=2.0, short_layers=4, force_exit=18, cooldown=3)),
    ("v13c-safe long-only (no short)", "v13", dict(
        enable_short=False, short_cash_pct=0.50,
        long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
        short_lev=2.0, short_layers=4,
        mid_start=12, aggr_start=17, force_exit=18, blackout_end=24,
        next_cycle_start=30, tol=0.03, cooldown=3, lifetime_w=26)),
]

for label, kind, params in strategies:
    results = {}
    for wn, (s, e, bottom) in WINDOWS.items():
        data = df.loc[s:e]
        if kind == "spot":
            p0 = data.Close.iloc[0]
            final = INITIAL * data.Close.iloc[-1] / p0
            mdd_pct = (data.Close/data.Close.cummax()-1).min()
            results[wn] = (final/INITIAL, mdd_pct)
        elif kind == "hodl_window":
            L = params
            p0 = data.Close.iloc[0]
            pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq=False
            curve = []
            for _, row in data.iterrows():
                if liq: curve.append(0); continue
                c -= pos*row.Close*FUNDING_DAILY
                el = c + pos*(row.Low - avg); mm_r = pos*row.Low*MM
                if el <= mm_r: liq=True; c=0;pos=0;avg=0
                curve.append(c + pos*(row.Close-avg) if pos>0 else c)
            final = curve[-1]
            cs = pd.Series(curve)
            mdd_pct = (cs/cs.cummax()-1).min() if (cs>0).all() else -1
            results[wn] = (final/INITIAL, mdd_pct)
        elif kind == "hodl_bottom":
            L = params
            r = simple_3x_hodl(data, pd.Timestamp(bottom), force_exit_months=18)
            # adjust for leverage L (simple_3x_hodl is hardcoded 3x, so reimplement)
            cash = INITIAL; pos = 0.0; avg = 0.0
            entered = False; force_exited = False; liq = False
            curve = []
            for _, row in data.iterrows():
                if liq: curve.append(0); continue
                if not entered and row.name >= pd.Timestamp(bottom):
                    pos = INITIAL * L / row.Close
                    avg = row.Close
                    cash -= INITIAL * L * FEE
                    entered = True
                if pos > 0 and not force_exited:
                    cash -= pos * row.Close * FUNDING_DAILY
                    eq_low = cash + pos*(row.Low - avg); mm_req = pos*row.Low*MM
                    if eq_low <= mm_req: liq=True; cash=0; pos=0; avg=0
                if pos > 0 and not force_exited:
                    mh = months_since_last_halving(row.name)
                    if mh >= 18:
                        fee = pos * row.Close * FEE
                        cash += pos*(row.Close - avg) - fee
                        pos = 0; avg = 0; force_exited = True
                eq = cash + pos*(row.Close - avg) if pos > 0 else cash
                curve.append(eq)
            final = curve[-1] if curve else INITIAL
            if liq: final = 0
            cs = pd.Series(curve)
            mdd_pct = (cs/cs.cummax()-1).min() if (cs>0).all() else -1
            results[wn] = (final/INITIAL, mdd_pct)
        elif kind == "v13":
            r = simulate_v13(data, **params)
            results[wn] = (r["final"]/INITIAL, r["max_dd"])
        elif kind == "v13d":
            r = simulate_v13d(data, **params)
            results[wn] = (r["final"]/INITIAL, r["max_dd"])

    pw, pw_mdd = results["PW"]
    w, w_mdd = results["W"]
    geo = (pw*w)**0.5 if pw > 0 and w > 0 else 0
    pw_s = f"{pw:>7.2f}x" + ('!' if pw == 0 else ' ')
    w_s = f"{w:>7.2f}x" + ('!' if w == 0 else ' ')
    geo_s = f"{geo:>5.2f}x" if geo > 0 else " LIQ "
    print(f"  {label:50}  {pw_s:>10}  {w_s:>10}  {geo_s:>6}  {pw_mdd*100:>+6.0f}%  {w_mdd*100:>+6.0f}%")
