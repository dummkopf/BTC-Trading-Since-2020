"""
User's grid-roll design: 5-layer inverted-pyramid entry on S/R supports,
RESET (close all, bank profit) when the whole position is up +X%, then
rebuild on the next support touch. Rolls many times.

Tested with profit targets 3/5/10/20%, on DAILY and WEEKLY support/
resistance, vs simple 3x HODL, on both bull cycles.

Goal: does the many-small-wins grid beat buy-and-hold in a trend?
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005

BULLS = {
    "PW bull 2018-12→2021-11": ("2018-12-15","2021-11-08"),
    "W  bull 2022-11→2025-10": ("2022-11-21","2025-10-06"),
}

def swings(data, w, weekly):
    if weekly:
        bars = data.resample("W-MON").agg(High=("High","max"),Low=("Low","min")).dropna()
    else:
        bars = data[["High","Low"]].copy()
    H,L=[],[]
    for i in range(w,len(bars)-w):
        win=bars.iloc[i-w:i+w+1]
        if bars.iloc[i].High==win.High.max(): H.append((bars.index[i+w],bars.iloc[i].High))
        if bars.iloc[i].Low==win.Low.min():  L.append((bars.index[i+w],bars.iloc[i].Low))
    return pd.DataFrame(H,columns=["confirm","price"]),pd.DataFrame(L,columns=["confirm","price"])

def grid_roll(data, lev, target, weekly, w=2, N=5, growth=1.5, lifetime_w=26):
    SH,SL = swings(data, w, weekly)
    cash=INITIAL; pos=0.0; avg=0.0; layer=0; l1=0
    sup_last={}; liq=False; resets=0; fills=0
    ld=lifetime_w*7; curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if pos>0:
            cash-=pos*r.Close*FUNDING
            if cash+pos*(r.Low-avg)<=pos*r.Low*MM:
                liq=True;cash=0;pos=0;curve.append(0);continue
        eq=cash+pos*(r.Close-avg) if pos>0 else cash
        # RESET: whole position up >= target (price vs avg)
        if pos>0 and (r.Close/avg-1)>=target:
            cash+=pos*(r.Close-avg)-pos*r.Close*FEE
            pos=0; avg=0; layer=0; resets+=1
            sup_last.clear()
            curve.append(cash); continue
        # build pyramid on support touch
        cut=dt-pd.Timedelta(days=ld)
        sups=SL[(SL.confirm<=dt)&(SL.confirm>cut)].price.values
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
    f=cash if not liq else 0
    s=pd.Series(curve,index=data.index)
    return f/INITIAL,(s/s.cummax()-1).min() if (s>0).all() else -1,liq,resets,fills

def hold(data, lev):
    cash=INITIAL;pos=INITIAL*lev/data.Close.iloc[0];avg=data.Close.iloc[0];cash-=INITIAL*lev*FEE;liq=False
    for dt,r in data.iterrows():
        if liq:continue
        cash-=pos*r.Close*FUNDING
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM:liq=True;cash=0;pos=0
    return (cash+pos*(data.Close.iloc[-1]-avg) if not liq else 0)/INITIAL

print("="*96)
print("Grid-roll (5层金字塔 + 整体+X%重置) vs 死扛 3x")
print("="*96)

for name,(s,e) in BULLS.items():
    data=df.loc[s:e]
    h=hold(data,3.0)
    spot=data.Close.iloc[-1]/data.Close.iloc[0]
    print(f"\n{name}  [BTC {spot:.0f}x, 死扛3x = {h:.1f}x, 现货 = {spot:.1f}x]")
    print(f"  {'设计':34} {'终值':>7} {'回撤':>7} {'重置次数':>8} {'加仓次数':>8}")
    for weekly,wl in [(False,"日线S/R"),(True,"周线S/R")]:
        for tgt in [0.03,0.05,0.10,0.20]:
            m,mdd,liq,rs,fl=grid_roll(data,3.0,tgt,weekly)
            tag=f"{wl} 目标+{int(tgt*100)}%"
            print(f"  {tag:34} {m:>6.1f}x {mdd*100:>+6.0f}% {rs:>8} {fl:>8}{'  爆仓!' if liq else ''}")
