"""
Bear-phase asymmetry test: for SHORTING a bear, is rolling (multiple
cover+reshort = inverted-pyramid-style) better than holding?

Hypothesis (user): bulls reward holding (smooth uptrend), but bears are
choppy with violent counter-trend bounces (+35% to +66%), so a held 3x
short gets liquidated on a bounce (+33% x3 = -100%). Therefore shorts
should ROLL — cover before bounces, re-short after.

Compare on the two post-top bears:
  PW bear: 2021-11-08 ($67.6k) -> 2022-11-21 ($15.8k), -77%
  W bear:  2025-10-06 ($124.8k) -> 2026-03-30 ($66.7k), -47% (partial)

Methods:
  A) HOLD short: short at bear start, hold to end (or liquidation)
  B) ROLL short: short at resistance, cover 1/3 at support, re-short (v16 logic)
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005

BEARS = {
    "PW bear 2021-11→2022-11 (-77%)": ("2021-11-08","2022-11-21"),
    "W  bear 2025-10→2026-03 (-47%)": ("2025-10-06","2026-03-30"),
}

def detect_swings_on(data, w=2):
    wb = data.resample("W-MON").agg(High=("High","max"),Low=("Low","min")).dropna()
    H,L=[],[]
    for i in range(w,len(wb)-w):
        win=wb.iloc[i-w:i+w+1]
        if wb.iloc[i].High==win.High.max(): H.append((wb.index[i+w],wb.iloc[i].High))
        if wb.iloc[i].Low==win.Low.min():  L.append((wb.index[i+w],wb.iloc[i].Low))
    return pd.DataFrame(H,columns=["confirm","price"]),pd.DataFrame(L,columns=["confirm","price"])

def hold_short(data, lev):
    """Short lev x at bear start, hold to end. Funding RECEIVED by shorts in bear (~neutral here)."""
    cash=INITIAL; entry=data.Close.iloc[0]; qty=INITIAL*lev/entry; cash-=INITIAL*lev*FEE
    liq=False; near=1.0; curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        cash -= qty*r.Close*FUNDING*0.5   # shorts roughly neutral/receive in bear; use half rate as cost
        # short liquidation: loss when price RISES. eq = cash + qty*(entry - mark). check at HIGH.
        eq_high = cash + qty*(entry - r.High)
        near=min(near, eq_high/INITIAL)
        if eq_high <= qty*r.High*MM:
            liq=True; cash=0; qty=0; curve.append(0); continue
        curve.append(cash + qty*(entry - r.Close))
    s=pd.Series(curve,index=data.index); f=s.iloc[-1] if not liq else 0
    return f/INITIAL, (s/s.cummax()-1).min() if (s>0).all() else -1, liq, near

def roll_short(data, lev, layers=3, stop=0.30):
    """v16-style rolling short: short at resistance (front-loaded flat), cover 1/3 at support."""
    SH,SL = detect_swings_on(data)
    cash=INITIAL; qty=0.0; avg=0.0; slayer=0; s1=0
    res_last={}; sup_last={}; liq=False; near=1.0; curve=[]; ld=26*7
    ncover=0; nshort=0; nstop=0
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if qty>0:
            cash -= qty*r.Close*FUNDING*0.5
            eq_high = cash + qty*(avg - r.High)
            near=min(near, eq_high/INITIAL)
            if eq_high <= qty*r.High*MM:
                liq=True; cash=0; qty=0; curve.append(0); continue
            if (r.High-avg)/avg >= stop:   # stop on bounce
                sp=avg*(1+stop); cash += qty*(avg-sp) - qty*sp*FEE
                qty=0; avg=0; slayer=0; nstop+=1; res_last.clear(); sup_last.clear()
        cut=dt-pd.Timedelta(days=ld)
        sups=SL[(SL.confirm<=dt)&(SL.confirm>cut)].price.values
        ress=SH[(SH.confirm<=dt)&(SH.confirm>cut)].price.values
        eq = cash + qty*(avg-r.Close) if qty>0 else cash
        # short entry at resistance
        if slayer<layers:
            for lvl in ress:
                if abs(r.Close-lvl)/lvl<=0.03 and r.Close<=lvl*1.03:
                    last=res_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        size = eq*lev/layers   # flat
                        size=min(size, max(0, lev*eq - qty*r.Close))
                        if size>1:
                            b=size/r.Close; cash-=size*FEE
                            nq=qty+b; avg=(qty*avg+b*r.Close)/nq if qty>0 else r.Close
                            qty=nq; slayer+=1; nshort+=1; res_last[lvl]=dt
                        break
        # cover 1/3 at support
        if qty>0 and r.Close<avg:
            for lvl in sups:
                if abs(r.Close-lvl)/lvl<=0.03:
                    last=sup_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        cq=qty*0.33; cash += cq*(avg-r.Close) - cq*r.Close*FEE
                        qty-=cq; ncover+=1; sup_last[lvl]=dt; slayer=0
                        if qty<1e-9: qty=0; avg=0
                        break
        curve.append(cash + qty*(avg-r.Close) if qty>0 else cash)
    # close residual at end
    if qty>0: cash += qty*(avg-data.Close.iloc[-1])
    s=pd.Series(curve,index=data.index); f=(cash) if not liq else 0
    return f/INITIAL, (s/s.cummax()-1).min() if (s>0).all() else -1, liq, near, nshort, ncover, nstop

print(f"{'='*100}")
print(f"熊市做空: 持有 vs 滚动  (本金 $7000 投入做空 book)")
print(f"{'='*100}\n")

for name,(s,e) in BEARS.items():
    data=df.loc[s:e]
    drop=(data.Close.iloc[-1]/data.Close.iloc[0]-1)
    print(f"{name}  [BTC {data.Close.iloc[0]:,.0f}→{data.Close.iloc[-1]:,.0f}, {drop*100:.0f}%]")
    print(f"  {'方法':28} {'终值倍数':>8} {'最大回撤':>8} {'爆仓':>5} {'近爆仓':>7}")
    # HOLD at various leverage
    for lev in [1.0,1.5,2.0,3.0]:
        m,mdd,liq,near = hold_short(data, lev)
        print(f"  {'持有空 '+str(lev)+'x':28} {m:>7.2f}x {mdd*100:>+7.0f}% {'是!' if liq else '否':>5} {near*100:>6.0f}%")
    # ROLL at various leverage
    for lev in [1.5,2.0,3.0]:
        m,mdd,liq,near,ns,nc,nst = roll_short(data, lev)
        tag=f"滚动空 {lev}x"
        print(f"  {tag:28} {m:>7.2f}x {mdd*100:>+7.0f}% {'是!' if liq else '否':>5} {near*100:>6.0f}%  (空{ns}/平{nc}/止损{nst})")
    print()
