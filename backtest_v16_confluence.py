"""
v16: Two-layer confluence strategy.

Core idea: the largest lever is bottom-timing precision. Test whether a
mechanical confluence signal (Mayer<0.7 AND RSI_w<35) can substitute for
perfect-hindsight bottom timing.

Layer A (Core, X% capital):
  - Deploy at 2x leverage in ONE shot when confluence fires (first day
    Mayer < mayer_thr AND RSI_w < rsiw_thr after being flat)
  - Hold (no trims) until halving+18 force exit — like signal-triggered 2x HODL

Layer B (Insurance, 1-X% capital):
  - Inverted pyramid on S/R, 1.5→3x phased, trim @+14mo 20%
  - Hedges the case where confluence fires too early / wrong

Both force-exit at halving+18. Bear short overlay on combined freed cash.

First: validate confluence-only (100% core) vs HODL-from-bottom.
Then: test the 60/40 blend.
"""
import sys, pandas as pd, numpy as np
sys.path.insert(0, '/home/user/BTC-Trading-Since-2020')
exec(open('/home/user/BTC-Trading-Since-2020/backtest_v14_hybrid.py').read().split('print(f"\\n{')[0])

# Need Mayer and RSI_w on the long dataset
df["sma200"] = df.Close.rolling(200).mean()
df["mayer"] = df.Close / df.sma200
def _rsi(s, n=14):
    d = s.diff()
    u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1 + u/v)
df["rsi_w"] = _rsi(df.Close.resample("W-MON").last()).reindex(df.index, method="ffill")

INITIAL = 7000.0; FUNDING_DAILY = 0.0001*3; FEE = 0.0005; MM = 0.005

WINDOWS = {
    "PW": ("2018-09-01", "2022-06-30", "2018-12-15"),
    "W":  ("2022-09-01", "2026-05-21", "2022-11-21"),
}

# ---- First: when does confluence fire vs actual bottom? ----
print(f"\n{'='*110}\nCONFLUENCE SIGNAL TIMING vs ACTUAL CYCLE BOTTOM")
print(f"{'='*110}\n")
for wn, (s, e, bottom) in WINDOWS.items():
    data = df.loc[s:e]
    conf = data[(data.mayer < 0.70) & (data.rsi_w < 35)]
    actual_bottom_price = data.loc[bottom, "Close"] if bottom in data.index else None
    print(f"  {wn}: actual bottom {bottom} ${actual_bottom_price:,.0f}")
    if len(conf) > 0:
        first_conf = conf.index[0]
        print(f"    first confluence fire: {first_conf.date()} @ ${data.loc[first_conf,'Close']:,.0f}  "
              f"({(data.loc[first_conf,'Close']/actual_bottom_price-1)*100:+.0f}% vs bottom, "
              f"{(first_conf - pd.Timestamp(bottom)).days:+d} days)")
        print(f"    total confluence days: {len(conf)}, last: {conf.index[-1].date()}")
    else:
        print(f"    NO confluence fire (Mayer<0.7 AND RSI_w<35 never triggered)")

# ---- v16 simulation ----
def simulate_v16(data, core_pct=0.60, core_lev=2.0,
                 core_entry_before_halving=18,  # deploy core at (next_halving - this) months
                 mayer_thr=0.70, rsiw_thr=35,    # unused now, kept for signature compat
                 ins_lev_low=1.5, ins_lev_high=3.0, ins_upgrade=1.5,
                 enable_short=True, short_cash_pct=0.70, short_lev=2.5,
                 short_layers=4, short_layer_growth=1.3, short_stop_pct=0.30,
                 force_exit=18, blackout_end=24, next_cycle_start=30,
                 tol=0.03, cooldown=3, lifetime_w=26, N=5, layer_growth=1.5,
                 ins_trim_month=14, ins_trim_pct=20):
    """Two-book: core (HALVING-ANCHORED 2x) + insurance (inverted pyramid).

    Core deploys 2x at (next_halving - core_entry_before_halving) months,
    which historically coincides with the cycle bottom (halving - 17 to -19mo).
    """
    # CORE book
    C_cash = INITIAL * core_pct
    C_pos = 0.0; C_avg = 0.0
    C_deployed = False
    C_force_done = None

    # INSURANCE book (inverted pyramid) — track separately
    I_cash = INITIAL * (1 - core_pct)
    I_pos = 0.0; I_avg = 0.0
    I_layer = 0; I_l1_size = 0
    I_support_last = {}
    I_force_done = None
    I_trim_done = None
    I_peak_mult = 1.0

    # SHORT book
    S_cash = 0.0; S_pos_qty = 0.0; S_avg = 0.0
    S_layer = 0; S_l1_size = 0
    S_resist_last = {}; S_support_last = {}
    short_cycle_active = False
    S_stops = 0

    liq = False
    eq_curve = []
    lifetime_days = lifetime_w * 7
    INS_INITIAL = INITIAL * (1 - core_pct)

    for i, (dt, r) in enumerate(data.iterrows()):
        if liq: eq_curve.append(0); continue
        mh = months_since_last_halving(dt)
        past_h = [h for h in HALVINGS if h <= dt]
        cur_halving = past_h[-1] if past_h else None
        in_bear_window = (force_exit <= mh < next_cycle_start)

        # ===== CORE BOOK =====
        if C_pos > 0:
            C_cash -= C_pos * r.Close * FUNDING_DAILY
            eq_low = C_cash + C_pos*(r.Low - C_avg); mm_req = C_pos*r.Low*MM
            if eq_low <= mm_req:
                C_cash = 0; C_pos = 0; C_avg = 0
        # Core deploy: HALVING-ANCHORED (next_halving - core_entry_before_halving months)
        if not C_deployed:
            future_h = [h for h in HALVINGS if h > dt]
            if future_h:
                months_until_halving = (future_h[0] - dt).days / 30.44
                if months_until_halving <= core_entry_before_halving:
                    notional = C_cash * core_lev
                    C_pos = notional / r.Close
                    C_cash -= notional * FEE
                    C_avg = r.Close
                    C_deployed = True
        # Core force exit
        if C_pos > 0 and force_exit <= mh < blackout_end and C_force_done != cur_halving:
            fee = C_pos * r.Close * FEE
            C_cash += C_pos*(r.Close - C_avg) - fee
            C_pos = 0; C_avg = 0; C_force_done = cur_halving

        # ===== INSURANCE BOOK (inverted pyramid) =====
        if I_pos > 0:
            I_cash -= I_pos * r.Close * FUNDING_DAILY
            eq_low = I_cash + I_pos*(r.Low - I_avg); mm_req = I_pos*r.Low*MM
            if eq_low <= mm_req:
                I_cash = 0; I_pos = 0; I_avg = 0; I_layer = 0; I_l1_size = 0

        ins_equity = I_cash + I_pos*(r.Close - I_avg) if I_pos > 0 else I_cash
        I_peak_mult = max(I_peak_mult, ins_equity / INS_INITIAL if INS_INITIAL > 0 else 1)
        ins_max_lev = ins_lev_high if I_peak_mult > ins_upgrade else ins_lev_low

        # Insurance force exit
        if I_pos > 0 and force_exit <= mh < blackout_end and I_force_done != cur_halving:
            fee = I_pos * r.Close * FEE
            I_cash += I_pos*(r.Close - I_avg) - fee
            I_pos = 0; I_avg = 0; I_layer = 0; I_l1_size = 0; I_force_done = cur_halving

        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values

        # Insurance pre-exit trim
        if (I_pos > 0 and r.Close > I_avg and mh >= ins_trim_month
            and I_trim_done != cur_halving):
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol:
                    btc_sell = I_pos * (ins_trim_pct/100); fee = btc_sell*r.Close*FEE
                    I_cash += btc_sell*(r.Close - I_avg) - fee
                    I_pos -= btc_sell
                    if I_pos < 1e-9: I_pos = 0; I_avg = 0
                    I_trim_done = cur_halving; I_layer = 0; I_l1_size = 0
                    break

        # Insurance buy
        in_long_blackout = (force_exit - 1 <= mh < next_cycle_start)
        if not in_long_blackout and I_layer < N and not liq:
            if ins_equity > 0:
                for lvl in sups:
                    if abs(r.Close - lvl)/lvl <= tol:
                        last = I_support_last.get(lvl)
                        if last is None or (dt - last).days >= cooldown:
                            if I_layer == 0:
                                geo_sum = sum(layer_growth**k for k in range(N))
                                I_l1_size = ins_equity * ins_max_lev / geo_sum
                                size = I_l1_size
                            else:
                                size = I_l1_size * (layer_growth)**I_layer
                            size = min(size, max(0, ins_max_lev*ins_equity - I_pos*r.Close))
                            if size > 1:
                                btc = size / r.Close; fee = size*FEE; I_cash -= fee
                                new_pos = I_pos + btc
                                I_avg = (I_pos*I_avg + btc*r.Close)/new_pos if I_pos > 0 else r.Close
                                I_pos = new_pos; I_layer += 1
                                I_support_last[lvl] = dt
                            break

        # ===== SHORT BOOK (bear window) =====
        if S_pos_qty > 0:
            S_cash -= S_pos_qty * r.Close * FUNDING_DAILY * 0.5
            eq_low_short = S_cash + S_pos_qty * (S_avg - r.High)
            if eq_low_short <= S_pos_qty * r.High * MM:
                S_cash = 0; S_pos_qty = 0; S_avg = 0; S_layer = 0; S_stops += 1
            adverse = (r.High - S_avg)/S_avg if S_avg > 0 else 0
            if S_pos_qty > 0 and adverse >= short_stop_pct:
                sp = S_avg*(1+short_stop_pct)
                pnl = S_pos_qty*(S_avg - sp) - S_pos_qty*sp*FEE
                S_cash += pnl; S_pos_qty = 0; S_avg = 0; S_layer = 0; S_stops += 1
                S_resist_last.clear(); S_support_last.clear()

        if enable_short and in_bear_window and not short_cycle_active:
            free = C_cash + I_cash
            if free > 0:
                alloc = free * short_cash_pct
                # pull proportionally
                C_take = C_cash * short_cash_pct; I_take = I_cash * short_cash_pct
                S_cash = C_take + I_take
                C_cash -= C_take; I_cash -= I_take
                short_cycle_active = True

        if not in_bear_window and short_cycle_active:
            if S_pos_qty > 0:
                pnl = S_pos_qty*(S_avg - r.Close) - S_pos_qty*r.Close*FEE
                S_cash += pnl; S_pos_qty = 0; S_avg = 0
            I_cash += S_cash; S_cash = 0; S_layer = 0; short_cycle_active = False

        if enable_short and short_cycle_active and S_layer < short_layers:
            for lvl in ress:
                if abs(r.Close - lvl)/lvl <= tol and r.Close <= lvl*(1+tol):
                    last = S_resist_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        if S_layer == 0:
                            gs = sum(short_layer_growth**k for k in range(short_layers))
                            S_l1_size = S_cash * short_lev / gs; size = S_l1_size
                        else:
                            size = S_l1_size * short_layer_growth**S_layer
                        size = min(size, max(0, short_lev*S_cash - S_pos_qty*r.Close))
                        if size > 1:
                            btc = size/r.Close; fee = size*FEE; S_cash -= fee
                            nq = S_pos_qty + btc
                            S_avg = (S_pos_qty*S_avg + btc*r.Close)/nq if S_pos_qty>0 else r.Close
                            S_pos_qty = nq; S_layer += 1
                            S_resist_last[lvl] = dt
                        break

        if enable_short and short_cycle_active and S_pos_qty > 0 and r.Close < S_avg:
            for lvl in sups:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = S_support_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        cq = S_pos_qty * 0.33; fee = cq*r.Close*FEE
                        pnl = cq*(S_avg - r.Close) - fee
                        S_cash += pnl; S_pos_qty -= cq
                        if S_pos_qty < 1e-9: S_pos_qty = 0; S_avg = 0
                        S_support_last[lvl] = dt; S_layer = 0
                        break

        # Total equity
        ce = C_cash + C_pos*(r.Close - C_avg) if C_pos > 0 else C_cash
        ie = I_cash + I_pos*(r.Close - I_avg) if I_pos > 0 else I_cash
        se = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        eq_curve.append(ce + ie + se)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(final=final, max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)

print(f"\n{'='*110}\nv16 RESULTS")
print(f"{'='*110}\n")
print(f"  {'Config':50}  {'PW':>8}  {'W':>8}  {'Geo':>6}  {'PW MDD':>7}  {'W MDD':>7}")
print(f"  {'-'*50}  {'-'*8}  {'-'*8}  {'-'*6}  {'-'*7}  {'-'*7}")

def run16(label, **cfg):
    results = {}
    for wn, (s, e, bottom) in WINDOWS.items():
        data = df.loc[s:e]
        r = simulate_v16(data, **cfg)
        results[wn] = (r["final"]/INITIAL, r["max_dd"])
    pw, pdd = results["PW"]; w, wdd = results["W"]
    geo = (pw*w)**0.5 if pw>0 and w>0 else 0
    pw_s = f"{pw:>6.2f}x"+('!' if pw==0 else ''); w_s = f"{w:>6.2f}x"+('!' if w==0 else '')
    print(f"  {label:50}  {pw_s:>8}  {w_s:>8}  {(str(round(geo,2))+'x') if geo>0 else 'LIQ':>6}  {pdd*100:>+6.0f}%  {wdd*100:>+6.0f}%")

print(f"  [core 占比扫描, halving-18mo 入场, core 2x]")
run16("v16 core 0% (pure insurance pyramid)", core_pct=0.0)
run16("v16 core 40%", core_pct=0.40)
run16("v16 core 60%", core_pct=0.60)
run16("v16 core 80%", core_pct=0.80)
run16("v16 core 100% (pure halving-anchored 2x)", core_pct=1.00)

print(f"\n  [core 入场时点扫描, core 60%, 2x] — halving 前 N 个月")
for m in [15, 16, 17, 18, 19, 20]:
    run16(f"core entry halving-{m}mo", core_pct=0.60, core_entry_before_halving=m)

print(f"\n  [core 杠杆扫描, core 60%, halving-18mo]")
for cl in [2.0, 2.5, 3.0]:
    run16(f"core_lev {cl}x", core_pct=0.60, core_lev=cl)

print(f"\n  [组合: core 80% 3x + halving-18mo + short 70%]")
run16("core 80% 3x", core_pct=0.80, core_lev=3.0)
run16("core 100% 3x (signal-free 3x HODL)", core_pct=1.00, core_lev=3.0)
run16("core 100% 2x (signal-free 2x HODL)", core_pct=1.00, core_lev=2.0)

print(f"\n  对照:")
print(f"  v15-robust (no core)                                17.39x   11.68x  14.25x    -76%/-46%")
print(f"  2x HODL 完美底                                      35.17x   10.51x  19.22x    -79%/-37%")
print(f"  3x HODL 完美底                                      52.25x   15.26x  28.24x    -83%/-39%")
