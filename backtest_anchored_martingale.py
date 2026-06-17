"""
Anchored-base martingale grid + OOS cross-validation.

User's design: $7k, 3x leverage from cycle bottom.
- Layer 1 = initial entry at cycle bottom (ANCHOR; never closed during bull)
- Drop a% from prev layer → add a new layer, size = 1.b × prev layer (geometric)
- Up to X layers per loop
- When price >= weighted_avg × (1+c%): PARTIAL close — bank the ADDED layers
  (Layer 2..N), keep Layer 1 as anchor. Start a new loop.
- Force exit at halving+17 (1 month before top)
- Test fees impact and check OOS robustness.

Key safety: Layer 1 anchors avg cost low → liq line stays near the cycle
bottom (below it after Layer 1). Subsequent layers pull avg up only modestly.
Question: does a generally-working (a,b,c,X) exist that beats HOLD across
both cycles AND survives OOS?
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005
HALV=[pd.Timestamp(d) for d in ["2020-05-11","2024-04-20"]]

BULLS = {
    "PW (2018-12→2021-11)": ("2018-12-15","2021-11-08"),
    "W  (2022-11→2025-10)": ("2022-11-21","2025-10-06"),
}

def anchored_grid(data, a, b, c, X, exit_mh=17, lev=3.0):
    """
    a: drop fraction between layers (e.g., 0.10 = -10%)
    b: layer size growth (e.g., 1.5 = each layer 1.5x prev)
    c: recovery target above weighted avg (e.g., 0.10 = +10%)
    X: max layers per loop (including anchor Layer 1)
    Layer 1 is permanent anchor; only Layers 2..X close at +c.
    """
    bd = data.index[0]
    # Layer 1 at bottom
    entry = data.Close.iloc[0]
    # split total target notional across X layers with growth b
    geo_sum = sum(b**k for k in range(X))
    L1_size_usd = INITIAL * lev / geo_sum  # Layer 1 notional
    L1_qty = L1_size_usd / entry
    anchor_qty = L1_qty; anchor_px = entry
    pos = L1_qty; avg = entry
    cash = INITIAL - L1_size_usd*FEE
    layer = 1  # we've placed Layer 1
    last_layer_px = entry
    fills = 1; closes = 0; liq = False; near=1.0
    curve=[]; fee_paid = L1_size_usd*FEE
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        cash -= pos*r.Close*FUNDING
        if pos>0:
            eq_low = cash + pos*(r.Low - avg)
            near = min(near, eq_low/INITIAL)
            if eq_low <= pos*r.Low*MM:
                liq=True; cash=0; pos=0; curve.append(0); continue
        eq = cash + pos*(r.Close - avg)
        # force exit at halving+17
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44 >= exit_mh:
            cash += pos*(r.Close - avg) - pos*r.Close*FEE
            fee_paid += pos*r.Close*FEE
            pos=0; avg=0
            curve.append(cash)
            # done — no more loops
            for j in range(len(data)-len(curve)): curve.append(cash)
            break
        # ADD a layer if price drops a% from last layer fill
        if layer < X and r.Close <= last_layer_px*(1-a):
            new_size = L1_size_usd * (b**layer)
            # leverage cap
            new_size = min(new_size, max(0, lev*eq - pos*r.Close))
            if new_size > 1:
                qty = new_size / r.Close
                cash -= new_size*FEE; fee_paid += new_size*FEE
                avg = (pos*avg + qty*r.Close) / (pos+qty)
                pos += qty
                layer += 1
                last_layer_px = r.Close
                fills += 1
        # CLOSE LOOP at price >= avg*(1+c): bank the ADDED layers, keep anchor
        if layer > 1 and r.Close >= avg*(1+c):
            # close the difference (pos - anchor_qty)
            sell = pos - anchor_qty
            if sell > 0:
                cash += sell*(r.Close - avg) - sell*r.Close*FEE
                fee_paid += sell*r.Close*FEE
                pos = anchor_qty
                avg = anchor_px  # back to anchor's avg
                layer = 1
                last_layer_px = r.Close  # next pyramid restarts from here
                closes += 1
        eq_now = cash + pos*(r.Close - avg) if pos>0 else cash
        curve.append(eq_now)
    s = pd.Series(curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    mdd = (s/s.cummax()-1).min() if (s>0).all() else -1
    return final/INITIAL, mdd, liq, near, fills, closes, fee_paid

def hold(data, lev, exit_mh=17):
    cash=INITIAL; pos=INITIAL*lev/data.Close.iloc[0]; avg=data.Close.iloc[0]; cash-=INITIAL*lev*FEE
    bd=data.index[0]; liq=False; ex=False
    for dt,r in data.iterrows():
        if liq or ex: continue
        cash-=pos*r.Close*FUNDING
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM: liq=True; cash=0; pos=0; continue
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44>=exit_mh:
            cash+=pos*(r.Close-avg)-pos*r.Close*FEE; pos=0; ex=True
    final=cash+pos*(data.Close.iloc[-1]-avg) if pos>0 else cash
    return (final if not liq else 0)/INITIAL

# Grid search
print(f"{'='*100}")
print(f"锚定基础 + 马丁格尔加仓 + 部分平仓 — 参数网格")
print(f"{'='*100}")

for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    h=hold(data,3.0)
    print(f"\n{cy}  [3x死扛(+17月退) = {h:.1f}x]")

aas=[0.05,0.10,0.15]; bs=[1.3,1.5,2.0]; cs=[0.05,0.10,0.15]; Xs=[4,5]
results={cy:{} for cy in BULLS}
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    for a in aas:
        for b in bs:
            for c in cs:
                for X in Xs:
                    m,mdd,liq,near,fl,cl,fp = anchored_grid(data,a,b,c,X)
                    results[cy][(a,b,c,X)] = (m,mdd,liq,fl,cl,fp)

# Show top-5 in each cycle
for cy in BULLS:
    valid={k:v for k,v in results[cy].items() if not v[2]}
    top=sorted(valid.items(), key=lambda x:-x[1][0])[:5]
    print(f"\n  Top 5 在 [{cy}]:")
    print(f"    {'a':>5} {'b':>5} {'c':>5} {'X':>3}  {'倍数':>7} {'回撤':>7} {'fills':>5} {'closes':>6} {'fees':>7}")
    for params,(m,mdd,liq,fl,cl,fp) in top:
        a,b,c,X=params
        print(f"    {a*100:>4.0f}% {b:>5.1f} {c*100:>4.0f}% {X:>3}  {m:>6.1f}x {mdd*100:>+6.0f}% {fl:>5} {cl:>6}  ${fp:>5,.0f}")

# OOS cross-validation
print(f"\n{'='*100}\n样本外交叉验证")
print(f"{'='*100}")
cys=list(BULLS.keys())
for train in cys:
    test=[c for c in cys if c!=train][0]
    valid={k:v for k,v in results[train].items() if not v[2]}
    best=max(valid,key=lambda k:valid[k][0])
    a,b,c,X = best
    bm = results[train][best][0]
    tm_res = results[test][best]
    tm = tm_res[0]
    h_test = hold(df.loc[BULLS[test][0]:BULLS[test][1]],3.0)
    print(f"\n  在 [{train}] 选最优: a={int(a*100)}% b={b} c={int(c*100)}% X={X}  → {bm:.1f}x")
    print(f"    套到 [{test}]: {tm:.1f}x{' 爆仓!' if tm_res[2] else ''}  (vs 该周期死扛 {h_test:.1f}x)")
    if not tm_res[2]:
        rel = tm/h_test*100
        print(f"    → 相对死扛 {rel:.0f}%  ({'打败' if rel>100 else '跑输'}死扛)")

# Also show the "fully optimal" — best (a,b,c,X) that BOTH cycles survive and beat hold
print(f"\n{'='*100}\n双周期都不爆仓 且都打败死扛 的配置:")
print(f"{'='*100}")
robust=[]
for params in results[cys[0]]:
    r0=results[cys[0]][params]; r1=results[cys[1]][params]
    if not r0[2] and not r1[2]:
        h0=hold(df.loc[BULLS[cys[0]][0]:BULLS[cys[0]][1]],3.0)
        h1=hold(df.loc[BULLS[cys[1]][0]:BULLS[cys[1]][1]],3.0)
        if r0[0]>h0 and r1[0]>h1:
            robust.append((params, r0[0], r1[0]))
if robust:
    print(f"  {'a':>5} {'b':>5} {'c':>5} {'X':>3}  {'PW':>6} {'W':>6}  几何")
    for params,p,w in sorted(robust, key=lambda x:-(x[1]*x[2])**0.5):
        a,b,c,X=params
        print(f"  {a*100:>4.0f}% {b:>5.1f} {c*100:>4.0f}% {X:>3}  {p:>5.1f}x {w:>5.1f}x  {(p*w)**0.5:.1f}x")
else:
    print(f"  没有任何配置在两个周期都打败死扛.")
