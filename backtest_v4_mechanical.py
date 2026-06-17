"""
v4: Pure mechanical rolling pyramid.
- N layers (5/6/7).  Each fires when price drops `a`% from previous fill.
- Each layer size = 1.b × previous layer (geometric growth).
- Trim `d`% when Close ≥ avg_cost × (1+c%).
- After trim: layer counter resets; next L1 fires on next `a`% dip from
  the trim price. Cycle repeats — compound across cycles.
- 3x leverage cap, daily-low intra-day liquidation check.

Window 2022-09-01 → 2025-08-31.  $7,000 start.
"""
import pandas as pd, numpy as np, itertools

OHLC = "/home/user/BTC-Trading-Since-2020/btc_daily_ohlc_2021-09_to_now.csv"
df = pd.read_csv(OHLC, parse_dates=["Date"]).sort_values("Date").set_index("Date")
d_data = df.loc["2022-09-01":"2025-08-31"].copy()

INITIAL = 7000.0
FUNDING_DAILY = 0.0001 * 3
FEE = 0.0005
MAX_LEV = 3.0
MM = 0.005

def simulate(a, b, c, d, max_layers):
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0
    l1_size = 0
    last_buy_price = None
    fees_t = funding_t = realized_t = 0.0
    n_trims = 0; n_buys = 0; max_layer_seen = 0
    out = []
    near_liq = 1.0
    liq = False

    for i, (dt, r) in enumerate(d_data.iterrows()):
        if liq:
            out.append(0.0); continue

        # funding
        if pos > 0:
            f = pos * r.Close * FUNDING_DAILY
            cash -= f; funding_t += f

        # intraday liquidation
        if pos > 0:
            eq_low = cash + pos * (r.Low - avg)
            mm_req = pos * r.Low * MM
            min_pct = eq_low / INITIAL
            if min_pct < near_liq: near_liq = min_pct
            if eq_low <= mm_req:
                liq = True; cash = 0; pos = 0; avg = 0
                out.append(0.0); continue

        # trim first
        if pos > 0 and r.Close >= avg * (1 + c/100):
            btc_sell = pos * (d/100)
            usd = btc_sell * r.Close
            fee = usd * FEE
            realized = btc_sell * (r.Close - avg) - fee
            cash += realized + btc_sell * avg  # wait — for perp, closing gives back unrealized only
            cash -= btc_sell * avg
            # net: cash += realized (which is btc_sell*(close-avg) - fee)
            realized_t += realized; fees_t += fee
            pos -= btc_sell
            if pos < 1e-9: pos = 0; avg = 0
            n_trims += 1
            layer = 0
            last_buy_price = r.Close
            l1_size = 0
            eq = cash + pos*(r.Close - avg) if pos > 0 else cash
            out.append(eq); continue

        # buy ladder
        equity = cash + pos * (r.Close - avg) if pos > 0 else cash
        if equity <= 0:
            liq = True; out.append(0.0); continue

        if layer == 0:
            # L1 trigger: day 1, OR a% drop from last anchor
            if last_buy_price is None or r.Close <= last_buy_price * (1 - a/100):
                geo_sum = sum((1+b)**k for k in range(max_layers))
                target = equity * MAX_LEV
                size = target / geo_sum
                cur_notional = pos * r.Close
                size = min(size, max(0, MAX_LEV*equity - cur_notional))
                if size > 1:
                    btc = size / r.Close
                    fee = size * FEE; cash -= fee; fees_t += fee
                    new_pos = pos + btc
                    avg = (pos*avg + btc*r.Close)/new_pos if pos > 0 else r.Close
                    pos = new_pos
                    layer = 1; n_buys += 1; l1_size = size
                    last_buy_price = r.Close
                    max_layer_seen = max(max_layer_seen, 1)
        elif layer < max_layers:
            if r.Close <= last_buy_price * (1 - a/100):
                layer_size = l1_size * (1+b)**layer
                cur_notional = pos * r.Close
                size = min(layer_size, max(0, MAX_LEV*equity - cur_notional))
                if size > 1:
                    btc = size / r.Close
                    fee = size * FEE; cash -= fee; fees_t += fee
                    new_pos = pos + btc
                    avg = (pos*avg + btc*r.Close)/new_pos
                    pos = new_pos
                    layer += 1; n_buys += 1
                    last_buy_price = r.Close
                    max_layer_seen = max(max_layer_seen, layer)

        eq = cash + pos*(r.Close - avg) if pos > 0 else cash
        out.append(eq)

    s = pd.Series(out, index=d_data.index)
    return dict(equity=s, final=s.iloc[-1], near_liq=near_liq, liq=liq,
                n_trims=n_trims, n_buys=n_buys, max_layer=max_layer_seen,
                realized=realized_t, funding=funding_t, fees=fees_t,
                final_pos=pos, final_avg=avg, final_cash=cash)

# ============= GRID SEARCH =============
params = list(itertools.product(
    [3, 5, 7, 10],          # a: % drop between layers
    [0.2, 0.5, 1.0],        # b: layer multiplier (1.b)
    [15, 25, 40, 60],       # c: % above avg_cost trim trigger
    [20, 33, 50],           # d: trim size %
    [5, 6, 7],              # max_layers
))
print(f"Running {len(params)} configurations...")

results = []
for a, b, c, d, n in params:
    r = simulate(a, b, c, d, n)
    results.append((r["final"], a, b, c, d, n, r))

results.sort(key=lambda x: -x[0])

# benchmark refs
spot = INITIAL * d_data.Close.iloc[-1] / d_data.Close.iloc[0]
# 3x HODL with intra-day liq check
def hodl(L):
    p0 = d_data.Close.iloc[0]; pos = INITIAL*L/p0; avg = p0; c = INITIAL; liq=False
    near = 1.0
    for _, r in d_data.iterrows():
        if liq: continue
        c -= pos * r.Close * FUNDING_DAILY
        el = c + pos*(r.Low - avg); mm_r = pos*r.Low*MM
        near = min(near, el/INITIAL)
        if el <= mm_r: liq=True; c=0;pos=0;avg=0
    return c + pos*(d_data.Close.iloc[-1]-avg) if pos>0 else c, near, liq
hodl_3x, near_3x, liq_3x = hodl(3.0)
hodl_2x, near_2x, liq_2x = hodl(2.0)

print(f"\n{'='*120}")
print(f"WINDOW 2022-09-01 → 2025-08-31   BTC ${d_data.Close.iloc[0]:,.0f} → ${d_data.Close.iloc[-1]:,.0f} (+{d_data.Close.iloc[-1]/d_data.Close.iloc[0]-1:.0%})")
print(f"  Spot HODL: ${spot:,.0f} ({spot/INITIAL-1:+.0%})")
print(f"  2x HODL  : ${hodl_2x:,.0f} ({hodl_2x/INITIAL-1:+.0%})  near-liq {near_2x*100:.1f}%")
print(f"  3x HODL  : ${hodl_3x:,.0f} ({hodl_3x/INITIAL-1:+.0%})  near-liq {near_3x*100:.1f}%")
print(f"{'='*120}")
print(f"\nTOP 15 PARAM SETS")
print(f"  {'eq':>10}  {'a%':>3}  {'1.b':>4}  {'c%':>3}  {'d%':>3}  {'N':>2}  {'buys':>4}  {'trims':>5}  {'maxL':>4}  {'real':>7}  {'fund':>7}  {'near':>5}")
for r in results[:15]:
    final, a, b, c, d, n, info = r
    print(f"  ${final:>9,.0f}  {a:>3}  {1+b:>4.1f}  {c:>3}  {d:>3}  {n:>2}  {info['n_buys']:>4}  {info['n_trims']:>5}  {info['max_layer']:>4}  ${info['realized']:>6,.0f}  ${info['funding']:>6,.0f}  {info['near_liq']*100:>4.0f}%")

print(f"\nBOTTOM 5:")
for r in results[-5:]:
    final, a, b, c, d, n, info = r
    print(f"  ${final:>9,.0f}  {a:>3}  {1+b:>4.1f}  {c:>3}  {d:>3}  {n:>2}  {info['n_buys']:>4}  {info['n_trims']:>5}  {info['max_layer']:>4}  ${info['realized']:>6,.0f}  ${info['funding']:>6,.0f}  {info['near_liq']*100:>4.0f}%")

# parameter sensitivity
print(f"\nPARAM SENSITIVITY (mean final equity over the 432 configs)")
for ax, label in [(1,"a%"), (2,"1+b"), (3,"c%"), (4,"d%"), (5,"max_layers")]:
    vals = sorted(set(r[ax] for r in results))
    print(f"  {label}:")
    for v in vals:
        grp = [r[0] for r in results if r[ax] == v]
        print(f"    {v:<6}  mean=${np.mean(grp):>9,.0f}  median=${np.median(grp):>9,.0f}  n={len(grp)}")

# liquidations?
liqs = [r for r in results if r[6]["liq"]]
print(f"\nLIQUIDATIONS: {len(liqs)}/{len(results)} configs")

# Top trade log
print(f"\n{'='*120}\nWINNING CONFIG DETAIL")
final, a, b, c, d, n, info = results[0]
print(f"  Params: a={a}% drop, 1+b={1+b}× size, c={c}% trim trigger, d={d}% trim size, max_layers={n}")
print(f"  Final equity: ${final:,.0f}  ({final/INITIAL-1:+.0%})  vs Spot ${spot:,.0f} ({final/spot-1:+.1%})")
print(f"  Buys {info['n_buys']}, Trims {info['n_trims']}, max layer reached {info['max_layer']}")
print(f"  Realized PnL ${info['realized']:,.0f}, Funding ${info['funding']:,.0f}, Fees ${info['fees']:,.0f}")
print(f"  Final pos {info['final_pos']:.5f} BTC @ avg ${info['final_avg']:,.0f}, Cash ${info['final_cash']:,.0f}")
print(f"  Near-liq closest: {info['near_liq']*100:.1f}% of initial equity")

# Save winner equity curve
out = pd.DataFrame({
    "date": d_data.index,
    "btc": d_data.Close.values,
    "winner": info["equity"].values,
})
out.to_csv("/home/user/BTC-Trading-Since-2020/backtest_v4_mechanical.csv", index=False)
print(f"\nSaved: backtest_v4_mechanical.csv")
