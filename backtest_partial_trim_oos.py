"""
Partial-trim grid + OUT-OF-SAMPLE cross-validation.

User's refinement: instead of FULL reset at +X% (which resets avg cost to
current price and liquidates in volatile cycles), trim only a FRACTION.
Keep a base position so avg cost stays weighted toward the original low
entry → preserves the liquidation buffer while still harvesting swings.

Critical method: tune (target, trim_pct) on ONE cycle, apply to the OTHER.
If a param best on PW dies/underperforms on W (or vice versa) → overfit.
If it generalizes → robust.

Base: 3x from cycle bottom, 5-layer pyramid rebuild on supports.
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005

BULLS = {
    "PW (2018-12→2021-11)": ("2018-12-15","2021-11-08"),
    "W  (2022-11→2025-10)": ("2022-11-21","2025-10-06"),
}

def swings(data, w=2):
    wb=data.resample("W-MON").agg(High=("High","max"),Low=("Low","min")).dropna()
    H,L=[],[]
    for i in range(w,len(wb)-w):
        win=wb.iloc[i-w:i+w+1]
        if wb.iloc[i].High==win.High.max(): H.append((wb.index[i+w],wb.iloc[i].High))
        if wb.iloc[i].Low==win.Low.min():  L.append((wb.index[i+w],wb.iloc[i].Low))
    return pd.DataFrame(H,columns=["confirm","price"]),pd.DataFrame(L,columns=["confirm","price"])

def partial_grid(data, lev, target, trim_pct, N=5, growth=1.5, lifetime_w=26):
    """3x pyramid from bottom; at price>=avg*(1+target) trim trim_pct (keep base);
    rebuild a layer on support touch. avg cost stays anchored low."""
    SH,SL=swings(data); cash=INITIAL; pos=0.0; avg=0.0; layer=0; l1=0
    sup_last={}; res_last={}; liq=False; ntrim=0; fills=0; ld=lifetime_w*7; curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if pos>0:
            cash-=pos*r.Close*FUNDING
            if cash+pos*(r.Low-avg)<=pos*r.Low*MM:
                liq=True;cash=0;pos=0;curve.append(0);continue
        eq=cash+pos*(r.Close-avg) if pos>0 else cash
        cut=dt-pd.Timedelta(days=ld)
        sups=SL[(SL.confirm<=dt)&(SL.confirm>cut)].price.values
        # PARTIAL TRIM at +target (keep base; trim once per up-cross via res cooldown)
        if pos>0 and (r.Close/avg-1)>=target:
            # only trim if not trimmed in last 7d (avoid hammering)
            if res_last.get("trim") is None or (dt-res_last["trim"]).days>=7:
                sell=pos*trim_pct; cash+=sell*(r.Close-avg)-sell*r.Close*FEE
                pos-=sell; ntrim+=1; res_last["trim"]=dt
                # note: avg unchanged by trim; layer count keeps so we don't over-rebuild
        # rebuild a layer on support touch (only if below layer cap)
        if layer<N and eq>0:
            for lvl in sups:
                if abs(r.Close-lvl)/lvl<=0.03:
                    last=sup_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        gs=sum(growth**k for k in range(N))
                        if layer==0: l1=eq*lev/gs; size=l1
                        else: size=l1*(growth**layer)
                        size=min(size,max(0,lev*eq-pos*r.Close))
                        if size>1:
                            b=size/r.Close;cash-=size*FEE
                            nq=pos+b;avg=(pos*avg+b*r.Close)/nq if pos>0 else r.Close
                            pos=nq;layer+=1;fills+=1;sup_last[lvl]=dt
                        break
        curve.append(cash+pos*(r.Close-avg) if pos>0 else cash)
    if pos>0: cash+=pos*(data.Close.iloc[-1]-avg)
    f=cash if not liq else 0; s=pd.Series(curve,index=data.index)
    return f/INITIAL,(s/s.cummax()-1).min() if (s>0).all() else -1,liq,ntrim,fills

def hold(data, lev):
    cash=INITIAL;pos=INITIAL*lev/data.Close.iloc[0];avg=data.Close.iloc[0];cash-=INITIAL*lev*FEE;liq=False
    for dt,r in data.iterrows():
        if liq:continue
        cash-=pos*r.Close*FUNDING
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM:liq=True;cash=0;pos=0
    return (cash+pos*(data.Close.iloc[-1]-avg) if not liq else 0)/INITIAL

# full grid over both cycles
targets=[0.10,0.15,0.20,0.30]; trims=[0.20,0.33,0.50]
results={cy:{} for cy in BULLS}
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    for tg in targets:
        for tp in trims:
            m,mdd,liq,nt,fl=partial_grid(data,3.0,tg,tp)
            results[cy][(tg,tp)]=(m,mdd,liq,nt)

print("="*100)
print("部分减仓 grid — 全表 (3x 从底, 保留底仓)")
print("="*100)
for cy,(s,e) in BULLS.items():
    h=hold(df.loc[s:e],3.0)
    print(f"\n{cy}  [死扛3x = {h:.1f}x]")
    print(f"  {'目标/减仓':>14}", end="")
    for tp in trims: print(f"  trim{int(tp*100)}%".rjust(12), end="")
    print()
    for tg in targets:
        print(f"  {'+'+str(int(tg*100))+'%':>14}", end="")
        for tp in trims:
            m,mdd,liq,nt=results[cy][(tg,tp)]
            cell=f"{m:.1f}x{'!' if liq else ''}({nt})"
            print(f"  {cell:>12}", end="")
        print()

# OUT-OF-SAMPLE cross-validation
print(f"\n{'='*100}")
print("样本外交叉验证: 在A周期选最优参数 → 套用到B周期")
print("="*100)
cys=list(BULLS.keys())
for train in cys:
    test=[c for c in cys if c!=train][0]
    # best by final mult on train (exclude liquidated)
    valid={k:v for k,v in results[train].items() if not v[2]}
    best=max(valid, key=lambda k: valid[k][0])
    bm=results[train][best]; tm=results[test][best]
    print(f"\n  在 [{train}] 选最优: 目标+{int(best[0]*100)}%/trim{int(best[1]*100)}%  → {bm[0]:.1f}x")
    print(f"    套到 [{test}]: {tm[0]:.1f}x{' 爆仓!' if tm[2] else ''}  (vs 该周期死扛 {hold(df.loc[BULLS[test][0]:BULLS[test][1]],3.0):.1f}x)")
    # is it robust? does train-best also rank well on test?
    test_valid={k:v for k,v in results[test].items() if not v[2]}
    test_best=max(test_valid,key=lambda k:test_valid[k][0]) if test_valid else None
    print(f"    [{test}] 自己的最优是 目标+{int(test_best[0]*100)}%/trim{int(test_best[1]*100)}% = {results[test][test_best][0]:.1f}x")
    print(f"    → train-best 在 test 上拿到 {tm[0]/results[test][test_best][0]*100:.0f}% 的 test-best 收益")
