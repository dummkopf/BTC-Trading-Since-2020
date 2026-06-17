"""
v13: v11h-a (long side) + bear-market shorting layer.

Bear window definition: halving + 19 to halving + 30 months
(force_exit 到 next cycle bottom 之前的时段).

In bear window:
  - Allow SHORT on resistance touch (mirror of long entry)
  - Cover SHORT on support touch (mirror of long trim)
  - Short pyramid: smaller (4 layers, 1.3x growth)
  - Short leverage: 1.5x (because bears are choppy, squeezes hurt)
  - Only deploy SHORT_CAPITAL_PCT of free cash (rest in USDC)
  - Hard stop: -15% from short entry (squeezes can be brutal)

This is on top of the long-side machine. Long side is COMPLETELY FLAT
in bear window (per v11h-a's blackout from force_exit to blackout_end).

Tested on PW + W extended windows that include bear periods.
"""
import pandas as pd, numpy as np

df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
weekly = df.resample("W-MON").agg(Open=("Open","first"), High=("High","max"),
                                    Low=("Low","min"), Close=("Close","last")).dropna()
def detect_swings(weekly_bars, w):
    highs, lows = [], []
    for i in range(w, len(weekly_bars) - w):
        wnd = weekly_bars.iloc[i-w:i+w+1]
        if weekly_bars.iloc[i].High == wnd.High.max():
            highs.append((weekly_bars.index[i+w], weekly_bars.iloc[i].High))
        if weekly_bars.iloc[i].Low == wnd.Low.min():
            lows.append((weekly_bars.index[i+w], weekly_bars.iloc[i].Low))
    return pd.DataFrame(highs, columns=["confirm","price"]), pd.DataFrame(lows, columns=["confirm","price"])
SH, SL = detect_swings(weekly, w=2)

INITIAL = 7000.0
FUNDING_DAILY_LONG  = 0.0001*3   # +0.01%/8h paid by longs
FUNDING_DAILY_SHORT = -0.0001*3  # shorts COLLECT in calm bear; assume neutral on average
FEE = 0.0005; MM = 0.005

HALVINGS = [pd.Timestamp(d) for d in
            ["2012-11-28", "2016-07-09", "2020-05-11", "2024-04-20", "2028-04-15"]]

def months_since_last_halving(dt):
    past = [h for h in HALVINGS if h <= dt]
    if not past: return -999
    return (dt - past[-1]).days / 30.44

# Strategy state — separate long & short books
def simulate_v13(data, enable_short=True,
                 long_lev_low=1.5, long_lev_high=3.0, long_upgrade=1.5,
                 short_lev=1.5, short_cash_pct=0.30, short_layers=4,
                 short_layer_growth=1.3, short_stop_pct=0.15,
                 mid_start=12, aggr_start=17, force_exit=19, blackout_end=24,
                 next_cycle_start=30, tol=0.03, cooldown=7, lifetime_w=26):
    """
    enable_short: if False, behaves like v11h-a.
    short_cash_pct: how much of cash to commit to short pyramid (rest stays in USDC).
    """
    # Long book
    L_cash = INITIAL; L_pos = 0.0; L_avg = 0.0
    L_layer = 0; L_l1_size = 0
    L_buys = L_trims = L_force = 0
    L_support_last = {}; L_resist_last = {}
    cur_max_lev = long_lev_low; peak_eq_mult = 1.0
    force_exit_done_for = None

    # Short book (separate cash)
    S_cash = 0.0  # filled when we enter bear window
    S_pos = 0.0   # NEGATIVE for short position (or track as positive contracts with side flag)
    S_pos_qty = 0.0  # positive number; negative is implicit
    S_avg = 0.0
    S_layer = 0; S_l1_size = 0
    S_shorts = S_covers = S_stops = 0
    S_resist_last = {}; S_support_last = {}
    short_cycle_active = False
    short_cash_allocated_for = None  # which halving's bear we're using cash for

    liq = False; near_liq = 1.0
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

        # === LONG BOOK: funding + liquidation ===
        if L_pos > 0:
            f = L_pos * r.Close * FUNDING_DAILY_LONG
            L_cash -= f; funding_long_t += f
            eq_low = L_cash + L_pos*(r.Low - L_avg); mm_req = L_pos*r.Low*MM
            if eq_low <= mm_req:
                # long book liquidated — strategy survives if short book has cash
                L_cash = 0; L_pos = 0; L_avg = 0
                L_layer = 0; L_l1_size = 0

        # === SHORT BOOK: funding + liquidation ===
        if S_pos_qty > 0:
            # short funding: pay if positive (bull-like), receive if negative
            # use small positive (conservative) for stress test
            f = S_pos_qty * r.Close * abs(FUNDING_DAILY_SHORT) * 0.5  # half rate, sign assumed neutral
            S_cash -= f; funding_short_t += f
            # short P&L when price moves: gain if down, loss if up
            # PnL = pos_qty * (S_avg - r.Close)
            eq_low_short = S_cash + S_pos_qty * (S_avg - r.High)  # worst case: price spikes up
            short_mm_req = S_pos_qty * r.High * MM
            if eq_low_short <= short_mm_req:
                S_cash = 0; S_pos_qty = 0; S_avg = 0
                S_layer = 0; S_l1_size = 0
                S_stops += 1
            # also: hard stop on adverse move from short entry
            adverse_pct = (r.High - S_avg) / S_avg if S_avg > 0 else 0
            if S_pos_qty > 0 and adverse_pct >= short_stop_pct:
                # cover at short_stop_pct loss
                stop_price = S_avg * (1 + short_stop_pct)
                pnl = S_pos_qty * (S_avg - stop_price) - S_pos_qty * stop_price * FEE
                S_cash += pnl  # FIXED: only PnL, no notional
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0; S_layer = 0; S_l1_size = 0
                S_stops += 1
                S_resist_last.clear(); S_support_last.clear()

        # === BEAR WINDOW ACTIVATION ===
        if enable_short and in_bear_window and not short_cycle_active and L_cash > 0:
            # allocate cash to short book
            allocation = L_cash * short_cash_pct
            S_cash = allocation
            L_cash -= allocation
            short_cycle_active = True
            short_cash_allocated_for = cur_halving

        # === BEAR WINDOW DEACTIVATION (cycle reset) ===
        if not in_bear_window and short_cycle_active:
            # cover any open short and return cash to long book
            if S_pos_qty > 0:
                pnl = S_pos_qty * (S_avg - r.Close) - S_pos_qty * r.Close * FEE
                S_cash += pnl  # FIXED: only PnL, no notional
                realized_short_t += pnl
                S_pos_qty = 0; S_avg = 0
            L_cash += S_cash  # return all short-book cash to long book
            S_cash = 0
            S_layer = 0; S_l1_size = 0
            S_resist_last.clear(); S_support_last.clear()
            short_cycle_active = False

        # === Compute current equity for leverage logic ===
        long_equity = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_equity = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        total_equity = long_equity + short_equity
        peak_eq_mult = max(peak_eq_mult, total_equity / INITIAL)
        cur_max_lev = long_lev_high if peak_eq_mult > long_upgrade else long_lev_low

        # === FORCE EXIT (long book only) ===
        if L_pos > 0 and force_exit <= mh < blackout_end and force_exit_done_for != cur_halving:
            fee = L_pos * r.Close * FEE
            L_cash += L_pos*(r.Close - L_avg) - fee
            L_pos = 0; L_avg = 0; L_layer = 0; L_l1_size = 0
            L_force += 1; force_exit_done_for = cur_halving
            L_support_last.clear(); L_resist_last.clear()

        # active S/R
        cutoff = dt - pd.Timedelta(days=lifetime_days)
        mask_l = (SL.confirm <= dt) & (SL.confirm > cutoff)
        mask_h = (SH.confirm <= dt) & (SH.confirm > cutoff)
        sups = SL.loc[mask_l, "price"].values
        ress = SH.loc[mask_h, "price"].values

        # === LONG TRIM ===
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

        # === LONG BUY (blackout in bear window) ===
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
                                btc = size / r.Close; fee = size*FEE; L_cash -= fee; fees_t += fee
                                new_pos = L_pos + btc
                                L_avg = (L_pos*L_avg + btc*r.Close)/new_pos if L_pos > 0 else r.Close
                                L_pos = new_pos
                                L_layer += 1; L_buys += 1
                                L_support_last[lvl] = dt
                            break

        # === SHORT ENTRY (resistance touch in bear window) ===
        if enable_short and short_cycle_active and S_layer < short_layers and not liq:
            for lvl in ress:
                # short enters when price is NEAR the resistance (selling into the bounce)
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
                            btc = size / r.Close; fee = size*FEE; S_cash -= fee; fees_t += fee
                            new_qty = S_pos_qty + btc
                            S_avg = (S_pos_qty*S_avg + btc*r.Close)/new_qty if S_pos_qty > 0 else r.Close
                            S_pos_qty = new_qty
                            S_layer += 1; S_shorts += 1
                            S_resist_last[lvl] = dt
                        break

        # === SHORT COVER (support touch in bear window) ===
        if enable_short and short_cycle_active and S_pos_qty > 0 and r.Close < S_avg:
            for lvl in sups:
                if abs(r.Close - lvl)/lvl <= tol:
                    last = S_support_last.get(lvl)
                    if last is None or (dt - last).days >= cooldown:
                        cover_qty = S_pos_qty * 0.33  # cover 1/3 each time
                        fee = cover_qty * r.Close * FEE
                        pnl = cover_qty*(S_avg - r.Close) - fee
                        S_cash += pnl  # FIXED: only PnL, no notional
                        realized_short_t += pnl
                        S_pos_qty -= cover_qty
                        if S_pos_qty < 1e-9: S_pos_qty = 0; S_avg = 0
                        S_covers += 1; S_support_last[lvl] = dt
                        S_layer = 0; S_l1_size = 0
                        break

        # equity tracking
        long_eq_now = L_cash + L_pos*(r.Close - L_avg) if L_pos > 0 else L_cash
        short_eq_now = S_cash + S_pos_qty*(S_avg - r.Close) if S_pos_qty > 0 else S_cash
        total_eq = long_eq_now + short_eq_now
        eq_curve.append(total_eq)

    s = pd.Series(eq_curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    return dict(curve=s, final=final, liq=liq,
                L_buys=L_buys, L_trims=L_trims, L_force=L_force,
                S_shorts=S_shorts, S_covers=S_covers, S_stops=S_stops,
                realized_long=realized_long_t, realized_short=realized_short_t,
                funding_long=funding_long_t, funding_short=funding_short_t,
                max_dd=(s/s.cummax()-1).min() if (s>0).all() else -1.0)

WINDOWS = {
    "PW (2018-09 → 2022-06)": ("2018-09-01", "2022-06-30"),
    "W  (2022-09 → 2026-05)":  ("2022-09-01", "2026-05-21"),
}

print(f"\n{'='*135}\nv13 = v11h-a long-side + bear-market shorting layer")
print(f"{'='*135}")

configs = [
    ("v11h-a 原版 (无 short)",                      False, 0.0,  0.15),
    ("v13a: short 30% cash, stop 15%, 4 layers",   True,  0.30, 0.15),
    ("v13b: short 50% cash, stop 15%",             True,  0.50, 0.15),
    ("v13c: short 30% cash, stop 25% (宽止损)",     True,  0.30, 0.25),
    ("v13d: short 30% cash, stop 10% (严止损)",     True,  0.30, 0.10),
    ("v13e: short 70% cash, stop 15% (重仓)",       True,  0.70, 0.15),
]

print(f"\n  {'配置':50}  {'PW 倍数':>9}  {'PW MDD':>8}  {'W 倍数':>9}  {'W MDD':>8}  {'几何均值':>9}")
print(f"  {'-'*50}  {'-'*9}  {'-'*8}  {'-'*9}  {'-'*8}  {'-'*9}")

for cfg in configs:
    name, en_short, scash, sstop = cfg
    results = {}
    for wn, dates in WINDOWS.items():
        data = df.loc[dates[0]:dates[1]]
        r = simulate_v13(data, enable_short=en_short,
                         short_cash_pct=scash, short_stop_pct=sstop)
        results[wn] = r
    pw = list(results.values())[0]; w = list(results.values())[1]
    pw_m = pw["final"]/INITIAL; w_m = w["final"]/INITIAL
    pw_s = f"{pw_m:>7.2f}x" + ('!' if pw['liq'] else ' ')
    w_s  = f"{w_m:>7.2f}x" + ('!' if w['liq'] else ' ')
    geo = f"{(pw_m*w_m)**0.5:.2f}x" if pw_m > 0 and w_m > 0 else "LIQ"
    pw_mdd = f"{pw['max_dd']*100:+.0f}%"
    w_mdd = f"{w['max_dd']*100:+.0f}%"
    print(f"  {name:50}  {pw_s:>10}  {pw_mdd:>7}  {w_s:>10}  {w_mdd:>7}  {geo:>9}")

# Detail best v13
print(f"\n{'='*135}\nv13a 详细 (short 30%, stop 15%, 4 layers)")
for wn, dates in WINDOWS.items():
    data = df.loc[dates[0]:dates[1]]
    r = simulate_v13(data, enable_short=True, short_cash_pct=0.30, short_stop_pct=0.15)
    print(f"\n  {wn}:")
    print(f"    final ${r['final']:,.0f} ({r['final']/INITIAL:.2f}x)  MDD {r['max_dd']*100:+.1f}%")
    print(f"    Long:  buys={r['L_buys']} trims={r['L_trims']} force_exit={r['L_force']} realized=${r['realized_long']:,.0f}")
    print(f"    Short: shorts={r['S_shorts']} covers={r['S_covers']} stops={r['S_stops']} realized=${r['realized_short']:,.0f}")
    print(f"    Funding: long ${r['funding_long']:,.0f}, short ${r['funding_short']:,.0f}")
