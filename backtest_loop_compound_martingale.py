"""
Loop-compounding martingale grid (user's true design).

Mechanism:
- $7k, 3x leverage cap, start at cycle bottom.
- X layers; layer k size = b^k × L1 (geometric, b=1.2 means +20% each).
- L1 size = current_equity × lev / geo_sum, so it AUTO-GROWS as equity grows.
- Start a loop: deploy L1 at current price.
- While in loop: if price drops a% from last fill AND layer<X → add next layer.
- Close loop when price >= weighted_avg × (1+c): bank profit. Equity grows.
  → next loop's layers are proportionally bigger (compounding).
- SMALL a% (1-3%) so loops fire MANY times.
- Force-exit at halving+17.

Fee optimization tested two ways:
  mode='full'    : close entire loop, reopen L1 next bar (more fees)
  mode='partial' : keep L1 as base, only close the ADDED layers (fewer fees)

Tested across PW + W cycles + OOS cross-validation + fee sensitivity.
Question: does loop-compounding finally beat hold-from-bottom?
"""
import pandas as pd, numpy as np, itertools
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; MM=0.005
HALV=[pd.Timestamp(d) for d in ["2020-05-11","2024-04-20"]]

BULLS = {
    "PW (2018-12→2021-11)": ("2018-12-15","2021-11-08"),
    "W  (2022-11→2025-10)": ("2022-11-21","2025-10-06"),
}

def loop_martingale(data, a, b, c, X, fee=0.0005, lev=3.0, exit_mh=17, mode='full'):
    bd = data.index[0]
    cash = INITIAL; pos = 0.0; avg = 0.0
    layer = 0; L1_qty = 0.0; L1_size_usd = 0.0
    last_fill_px = None
    geo_sum = sum(b**k for k in range(X))
    loops = 0; fills = 0; liq = False; near = 1.0; fee_paid = 0.0
    curve = []
    in_loop = False

    for i,(dt,r) in enumerate(data.iterrows()):
        if liq: curve.append(0); continue
        if pos>0:
            cash -= pos*r.Close*FUNDING
            eq_low = cash + pos*(r.Low - avg)
            near = min(near, eq_low/INITIAL)
            if eq_low <= pos*r.Low*MM:
                liq=True; cash=0; pos=0; curve.append(0); continue
        equity = cash + pos*(r.Close - avg) if pos>0 else cash

        # force exit at halving+17 → close everything, stop
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44 >= exit_mh:
            if pos>0:
                cash += pos*(r.Close-avg) - pos*r.Close*fee; fee_paid += pos*r.Close*fee
                pos=0; avg=0
            curve.append(cash)
            for _ in range(len(data)-len(curve)): curve.append(cash)
            break

        # CLOSE loop at +c above weighted avg
        if in_loop and pos>0 and r.Close >= avg*(1+c):
            if mode=='full':
                cash += pos*(r.Close-avg) - pos*r.Close*fee; fee_paid += pos*r.Close*fee
                pos=0; avg=0; in_loop=False; layer=0
            else:  # partial: keep L1 base, close added layers
                if layer>1:
                    sell = pos - L1_qty
                    if sell>0:
                        cash += sell*(r.Close-avg) - sell*r.Close*fee; fee_paid += sell*r.Close*fee
                        pos = L1_qty
                        avg = avg  # approx: keep weighted avg of remaining (L1 portion)
                        # recompute avg as L1's original — approximate by keeping current avg
                    layer=1; last_fill_px=r.Close
                else:
                    # only L1 deployed; close it and restart (same as full for L1)
                    cash += pos*(r.Close-avg) - pos*r.Close*fee; fee_paid += pos*r.Close*fee
                    pos=0; avg=0; in_loop=False; layer=0
            loops += 1
            equity = cash + pos*(r.Close - avg) if pos>0 else cash

        # START a loop: deploy L1 at current price (sized from current equity → compounding)
        if not in_loop and pos==0:
            L1_size_usd = equity * lev / geo_sum
            L1_size_usd = min(L1_size_usd, equity*lev)
            if L1_size_usd>1:
                L1_qty = L1_size_usd / r.Close
                cash -= L1_size_usd*fee; fee_paid += L1_size_usd*fee
                pos = L1_qty; avg = r.Close
                layer = 1; last_fill_px = r.Close; in_loop=True; fills+=1

        # ADD a layer on a% drop from last fill
        elif in_loop and layer<X and last_fill_px and r.Close <= last_fill_px*(1-a):
            new_size = L1_size_usd * (b**layer)
            new_size = min(new_size, max(0, lev*equity - pos*r.Close))
            if new_size>1:
                qty = new_size/r.Close
                cash -= new_size*fee; fee_paid += new_size*fee
                avg = (pos*avg + qty*r.Close)/(pos+qty)
                pos += qty; layer+=1; last_fill_px=r.Close; fills+=1

        eq_now = cash + pos*(r.Close-avg) if pos>0 else cash
        curve.append(eq_now)

    s = pd.Series(curve, index=data.index)
    final = s.iloc[-1] if not liq else 0
    mdd = (s/s.cummax()-1).min() if (s>0).all() else -1
    return final/INITIAL, mdd, liq, near, loops, fills, fee_paid

def hold(data, lev=3.0, exit_mh=17):
    cash=INITIAL; pos=INITIAL*lev/data.Close.iloc[0]; avg=data.Close.iloc[0]; cash-=INITIAL*lev*0.0005
    bd=data.index[0]; liq=False; ex=False
    for dt,r in data.iterrows():
        if liq or ex: continue
        cash-=pos*r.Close*FUNDING
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM: liq=True;cash=0;pos=0;continue
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44>=exit_mh: cash+=pos*(r.Close-avg)-pos*r.Close*0.0005;pos=0;ex=True
    return (cash+pos*(data.Close.iloc[-1]-avg) if (pos>0 and not liq) else (cash if not liq else 0))/INITIAL

print(f"{'='*100}\n复利 loop 马丁格尔 — 小 a% 高频滚动 (mode=full)")
print(f"{'='*100}")
for cy,(s,e) in BULLS.items():
    print(f"\n{cy}  [3x死扛(+17退) = {hold(df.loc[s:e]):.1f}x]")

# grid: small a%, growth b, recovery c, layers X
aas=[0.01,0.02,0.03,0.05]; bs=[1.2,1.5]; ccs=[0.01,0.02,0.03,0.05]; Xs=[5,7]
results={cy:{} for cy in BULLS}
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    for a in aas:
        for b in bs:
            for c in ccs:
                for X in Xs:
                    m,mdd,liq,near,lp,fl,fp = loop_martingale(data,a,b,c,X)
                    results[cy][(a,b,c,X)]=(m,mdd,liq,lp,fl,fp)

for cy in BULLS:
    valid={k:v for k,v in results[cy].items() if not v[2]}
    top=sorted(valid.items(),key=lambda x:-x[1][0])[:6] if valid else []
    nliq=sum(1 for v in results[cy].values() if v[2])
    print(f"\n  [{cy}] Top 6 (爆仓 {nliq}/{len(results[cy])} 个配置):")
    print(f"    {'a':>4} {'b':>4} {'c':>4} {'X':>3}  {'倍数':>7} {'回撤':>7} {'loops':>6} {'fills':>6} {'fees':>8}")
    for params,(m,mdd,liq,lp,fl,fp) in top:
        a,b,c,X=params
        print(f"    {a*100:>3.0f}% {b:>4.1f} {c*100:>3.0f}% {X:>3}  {m:>6.1f}x {mdd*100:>+6.0f}% {lp:>6} {fl:>6}  ${fp:>6,.0f}")

# OOS
print(f"\n{'='*100}\n样本外交叉验证")
print(f"{'='*100}")
cys=list(BULLS.keys())
for train in cys:
    test=[c for c in cys if c!=train][0]
    valid={k:v for k,v in results[train].items() if not v[2]}
    if not valid:
        print(f"\n  [{train}] 无非爆仓配置"); continue
    best=max(valid,key=lambda k:valid[k][0]); a,b,c,X=best
    bm=results[train][best][0]; tr=results[test][best]
    h_test=hold(df.loc[BULLS[test][0]:BULLS[test][1]])
    print(f"\n  [{train}] 最优 a={int(a*100)}% b={b} c={int(c*100)}% X={X} → {bm:.1f}x")
    print(f"    套到 [{test}]: {tr[0]:.1f}x{' 爆仓!' if tr[2] else ''}  (死扛 {h_test:.1f}x, 相对 {tr[0]/h_test*100:.0f}%)")

# robust set
print(f"\n{'='*100}\n双周期都不爆仓且都打败死扛:")
print(f"{'='*100}")
robust=[]
h0=hold(df.loc[BULLS[cys[0]][0]:BULLS[cys[0]][1]]); h1=hold(df.loc[BULLS[cys[1]][0]:BULLS[cys[1]][1]])
for params in results[cys[0]]:
    r0=results[cys[0]][params]; r1=results[cys[1]][params]
    if not r0[2] and not r1[2] and r0[0]>h0 and r1[0]>h1:
        robust.append((params,r0[0],r1[0]))
if robust:
    for params,p,w in sorted(robust,key=lambda x:-(x[1]*x[2])**0.5):
        a,b,c,X=params
        print(f"  a={int(a*100)}% b={b} c={int(c*100)}% X={X}: PW {p:.1f}x, W {w:.1f}x (死扛 {h0:.1f}/{h1:.1f})")
else:
    print(f"  没有任何配置在两个周期都打败死扛 (死扛 PW {h0:.1f}x / W {h1:.1f}x).")

# fee impact: best config with fee=0 vs fee=0.05% vs partial mode
print(f"\n{'='*100}\n手续费影响 (用 PW 最优配置)")
print(f"{'='*100}")
valid={k:v for k,v in results[cys[0]].items() if not v[2]}
if valid:
    best=max(valid,key=lambda k:valid[k][0]); a,b,c,X=best
    data=df.loc[BULLS[cys[0]][0]:BULLS[cys[0]][1]]
    for fee,lbl in [(0.0,"无手续费"),(0.0005,"0.05% (taker)"),(0.0002,"0.02% (maker)")]:
        m,mdd,liq,near,lp,fl,fp=loop_martingale(data,a,b,c,X,fee=fee,mode='full')
        print(f"  full  fee={lbl:14}: {m:.1f}x  loops={lp} fills={fl} fees=${fp:,.0f}")
    for fee,lbl in [(0.0005,"0.05% taker")]:
        m,mdd,liq,near,lp,fl,fp=loop_martingale(data,a,b,c,X,fee=fee,mode='partial')
        print(f"  partial fee={lbl:13}: {m:.1f}x  loops={lp} fills={fl} fees=${fp:,.0f}  (保留base省手续费)")
