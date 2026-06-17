"""
Loop-compounding martingale WITH a 30% safety-margin constraint.

User's goal: NOT chase absolute return. Find a setting that, in BOTH
cycles, NEVER liquidates AND keeps a >=30% margin to the liquidation
line — i.e., the worst price-vs-weighted-avg drawdown (MAE) never exceeds
-23% (at 3x, liq is ~-33%; staying above -23% keeps 30% of the distance
as buffer). Then check it out-of-sample.

Two safety knobs added on top of the loop martingale:
  - deploy_cap: max total deployed notional as a multiple of equity
    (the real lever for margin; 'using 3x' but capping how much is
    actually deployed at any time).
  - brake: stop ADDING layers once price is already brake% below avg
    (don't deepen into a crash).

We track MAE = min(Low/avg - 1) while holding, in each cycle.
Filter: no-liq AND MAE >= -0.23 in BOTH cycles. Rank survivors by return.
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; MM=0.005
HALV=[pd.Timestamp(d) for d in ["2020-05-11","2024-04-20"]]
BULLS = {"PW (2018-12→2021-11)": ("2018-12-15","2021-11-08"),
         "W  (2022-11→2025-10)": ("2022-11-21","2025-10-06")}

def loop_safe(data, a, b, c, X, deploy_cap, brake, fee=0.0005, exit_mh=17):
    bd=data.index[0]; cash=INITIAL; pos=0.0; avg=0.0
    layer=0; L1_usd=0.0; last_px=None
    geo=sum(b**k for k in range(X))
    loops=0; fills=0; liq=False; mae=0.0; fee_paid=0.0; curve=[]; in_loop=False
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if pos>0:
            cash-=pos*r.Close*FUNDING
            # MAE: worst price vs avg while holding
            mae=min(mae, r.Low/avg-1)
            eq_low=cash+pos*(r.Low-avg)
            if eq_low<=pos*r.Low*MM: liq=True;cash=0;pos=0;curve.append(0);continue
        equity=cash+pos*(r.Close-avg) if pos>0 else cash
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44>=exit_mh:
            if pos>0: cash+=pos*(r.Close-avg)-pos*r.Close*fee; fee_paid+=pos*r.Close*fee; pos=0;avg=0
            curve.append(cash)
            for _ in range(len(data)-len(curve)): curve.append(cash)
            break
        # close loop at +c
        if in_loop and pos>0 and r.Close>=avg*(1+c):
            cash+=pos*(r.Close-avg)-pos*r.Close*fee; fee_paid+=pos*r.Close*fee
            pos=0;avg=0;in_loop=False;layer=0; loops+=1
            equity=cash
        # start loop
        if not in_loop and pos==0:
            L1_usd=equity*deploy_cap/geo
            if L1_usd>1:
                qty=L1_usd/r.Close; cash-=L1_usd*fee; fee_paid+=L1_usd*fee
                pos=qty; avg=r.Close; layer=1; last_px=r.Close; in_loop=True; fills+=1
        # add layer on a% drop — BUT not if already brake% below avg (safety)
        elif in_loop and layer<X and last_px and r.Close<=last_px*(1-a):
            adverse=(avg-r.Close)/avg
            if adverse < brake:  # only deepen if not yet too deep
                new=L1_usd*(b**layer)
                new=min(new, max(0, deploy_cap*equity - pos*r.Close))
                if new>1:
                    qty=new/r.Close; cash-=new*fee; fee_paid+=new*fee
                    avg=(pos*avg+qty*r.Close)/(pos+qty); pos+=qty
                    layer+=1; last_px=r.Close; fills+=1
        eq=cash+pos*(r.Close-avg) if pos>0 else cash
        curve.append(eq)
    s=pd.Series(curve,index=data.index); final=s.iloc[-1] if not liq else 0
    mdd=(s/s.cummax()-1).min() if (s>0).all() else -1
    return final/INITIAL, mdd, liq, mae, loops, fills, fee_paid

def hold(data, lev=3.0, exit_mh=17):
    cash=INITIAL;pos=INITIAL*lev/data.Close.iloc[0];avg=data.Close.iloc[0];cash-=INITIAL*lev*0.0005
    bd=data.index[0];liq=False;ex=False;mae=0.0
    for dt,r in data.iterrows():
        if liq or ex: continue
        cash-=pos*r.Close*FUNDING; mae=min(mae,r.Low/avg-1)
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM: liq=True;cash=0;pos=0;continue
        fut=[h for h in HALV if h>bd and h<=dt]
        if fut and (dt-fut[-1]).days/30.44>=exit_mh: cash+=pos*(r.Close-avg)-pos*r.Close*0.0005;pos=0;ex=True
    return (cash+pos*(data.Close.iloc[-1]-avg) if (pos>0 and not liq) else (cash if not liq else 0))/INITIAL, mae

# hold reference + its MAE (hold from bottom: avg=bottom, so MAE is shallow)
print("="*100)
print("基准: 3x 从底死扛的 MAE (价格相对均价最深跌幅)")
print("="*100)
for cy,(s,e) in BULLS.items():
    h,mae=hold(df.loc[s:e]); print(f"  {cy}: {h:.1f}x, MAE {mae*100:+.0f}%  ({'安全' if mae>-0.23 else '边际不足'})")

# grid with safety knobs
aas=[0.01,0.02,0.03]; bs=[1.2,1.5]; ccs=[0.02,0.03,0.05]; Xs=[5,7]
caps=[1.5,2.0,2.5,3.0]; brakes=[0.10,0.15,0.20]
res={cy:{} for cy in BULLS}
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    for a in aas:
     for b in bs:
      for c in ccs:
       for X in Xs:
        for cap in caps:
         for br in brakes:
            m,mdd,liq,mae,lp,fl,fp=loop_safe(data,a,b,c,X,cap,br)
            res[cy][(a,b,c,X,cap,br)]=(m,liq,mae,lp,fp)

cys=list(BULLS.keys())
h0,_=hold(df.loc[BULLS[cys[0]][0]:BULLS[cys[0]][1]]); h1,_=hold(df.loc[BULLS[cys[1]][0]:BULLS[cys[1]][1]])

# FILTER: both cycles no-liq AND MAE >= -23%
print(f"\n{'='*100}")
print(f"满足 30% 安全边际 (两周期都不爆仓 且 MAE >= -23%) 的配置")
print(f"{'='*100}")
safe=[]
for p in res[cys[0]]:
    r0=res[cys[0]][p]; r1=res[cys[1]][p]
    if (not r0[1]) and (not r1[1]) and r0[2]>=-0.23 and r1[2]>=-0.23:
        safe.append((p, r0[0], r1[0], r0[2], r1[2]))
print(f"  共 {len(safe)} 个配置满足约束 (总 {len(res[cys[0]])} 个)")
if safe:
    print(f"\n  按几何均值排序 Top 12:")
    print(f"  {'a':>4}{'b':>5}{'c':>4}{'X':>3}{'cap':>5}{'brake':>6}  {'PW':>7}{'W':>7}{'几何':>7}  {'PW_MAE':>7}{'W_MAE':>7}")
    for p,pw,w,m0,m1 in sorted(safe,key=lambda x:-(x[1]*x[2])**0.5)[:12]:
        a,b,c,X,cap,br=p
        print(f"  {int(a*100):>3}%{b:>5.1f}{int(c*100):>3}%{X:>3}{cap:>5.1f}{int(br*100):>5}%  {pw:>6.1f}x{w:>6.1f}x{(pw*w)**0.5:>6.1f}x  {m0*100:>+6.0f}%{m1*100:>+6.0f}%")
    print(f"\n  对照死扛: PW {h0:.1f}x / W {h1:.1f}x, 几何 {(h0*h1)**0.5:.1f}x")
    # how many beat hold in BOTH?
    beat=[x for x in safe if x[1]>h0 and x[2]>h1]
    print(f"  其中两周期都打败死扛: {len(beat)} 个")
else:
    print("  没有配置满足 30% 安全边际约束.")

# OOS on the safest-best
if safe:
    print(f"\n{'='*100}\n样本外: 在每个周期选'满足约束里收益最高'的配置, 套到另一周期")
    print(f"{'='*100}")
    for train_i,train in enumerate(cys):
        test=cys[1-train_i]
        # safe configs ranked by train return
        train_safe=[(p,res[train][p][0],res[train][p][2]) for p in res[train]
                    if not res[train][p][1] and res[train][p][2]>=-0.23]
        if not train_safe: print(f"  [{train}] 无满足约束配置"); continue
        best=max(train_safe,key=lambda x:x[1])
        p=best[0]; a,b,c,X,cap,br=p
        tr=res[test][p]
        h_test,_=hold(df.loc[BULLS[test][0]:BULLS[test][1]])
        ok_margin = (not tr[1]) and tr[2]>=-0.23
        print(f"\n  [{train}] 约束内最优 a={int(a*100)}% b={b} c={int(c*100)}% X={X} cap={cap} brake={int(br*100)}% → {best[1]:.1f}x (MAE {best[2]*100:+.0f}%)")
        print(f"    套到 [{test}]: {tr[0]:.1f}x  MAE {tr[2]*100:+.0f}%  {'✓满足30%margin' if ok_margin else '✗ 边际破了'}  (死扛 {h_test:.1f}x)")
