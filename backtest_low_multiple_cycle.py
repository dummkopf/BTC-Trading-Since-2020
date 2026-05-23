"""
Low-multiple cycle stress test.

Next cycle (2026→2029) likely only 3-4x (vs last cycle's 8x), per the
diminishing-returns trend (573x→107x→21x→8x). We synthesize compressed
cycles via power-law scaling anchored at the cycle bottom:

    compressed[t] = bottom * (real[t]/bottom) ** k,  k = ln(target)/ln(real)

This preserves the SHAPE (peaks stay peaks, dips stay dips, timing
intact) but rescales total amplitude to `target` multiple. Both trend
and drawdowns compress proportionally (consistent with the observation
that lower-multiple cycles also had shallower dips).

Question answered: in a 3-4x cycle, what leverage maximizes return
without liquidation? Does the strategy still beat spot HODL?
"""
import pandas as pd, numpy as np

df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")

# W cycle window: bottom 2022-11-21, real top 2025-10-06 (8x), through bear
WIN = ("2022-11-21", "2026-03-30")
data_real = df.loc[WIN[0]:WIN[1]].copy()
bottom = data_real.Close.iloc[0]
real_top = data_real.Close.max()
real_mult = real_top / bottom
print(f"真实 W 周期: 底 ${bottom:,.0f} → 顶 ${real_top:,.0f} = {real_mult:.1f}x")

def compress(data, target_mult):
    """Power-law compress price path to target bottom→top multiple."""
    k = np.log(target_mult) / np.log(real_mult)
    out = data.copy()
    for col in ["Open","High","Low","Close"]:
        out[col] = bottom * (data[col] / bottom) ** k
    return out

# ---- v16 engine (long pyramid + bear short), returns final/maxdd/liq ----
weekly_full = df.resample("W-MON").agg(Open=("Open","first"),High=("High","max"),Low=("Low","min"),Close=("Close","last")).dropna()
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005
HALV=[pd.Timestamp(d) for d in ["2016-07-09","2020-05-11","2024-04-20","2028-04-15"]]
def msh(dt):
    p=[h for h in HALV if h<=dt]; return (dt-p[-1]).days/30.44 if p else -999

def detect_swings_on(data, w=2):
    wb = data.resample("W-MON").agg(High=("High","max"),Low=("Low","min")).dropna()
    H,L=[],[]
    for i in range(w,len(wb)-w):
        win=wb.iloc[i-w:i+w+1]
        if wb.iloc[i].High==win.High.max(): H.append((wb.index[i+w],wb.iloc[i].High))
        if wb.iloc[i].Low==win.Low.min():  L.append((wb.index[i+w],wb.iloc[i].Low))
    return pd.DataFrame(H,columns=["confirm","price"]),pd.DataFrame(L,columns=["confirm","price"])

def run_v16(data, lev_low, lev_high, enable_short=True, short_pct=0.30, short_lev=1.5):
    SH,SL = detect_swings_on(data)
    cash=INITIAL;pos=0.0;avg=0.0;layer=0;l1=0
    Scash=0.0;Sqty=0.0;Savg=0.0;Slayer=0;Sl1=0;short_active=False
    sup_last={};res_last={};Sres_last={};Ssup_last={}
    peak=1.0;fe=None;trim_done=None; ld=26*7; liq=False
    curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        mh=msh(dt);past=[h for h in HALV if h<=dt];cur=past[-1] if past else None
        bear=(18<=mh<30)
        if pos>0: cash-=pos*r.Close*FUNDING
        if Sqty>0: Scash-=Sqty*r.Close*FUNDING*0.5
        if pos>0 and cash+pos*(r.Low-avg)<=pos*r.Low*MM:
            liq=True;curve.append(0);continue
        eqL=cash+pos*(r.Close-avg) if pos>0 else cash
        eqS=Scash+Sqty*(Savg-r.Close) if Sqty>0 else Scash
        eq=eqL+eqS; peak=max(peak,eq/INITIAL); mlev=lev_high if peak>1.5 else lev_low
        # force exit
        if pos>0 and 18<=mh<24 and fe!=cur:
            cash+=pos*(r.Close-avg)-pos*r.Close*FEE;pos=0;avg=0;layer=0;fe=cur
            sup_last.clear();res_last.clear()
        # bear alloc (after FE)
        if bear and not short_active and cash>0:
            Scash=cash*short_pct;cash-=Scash;short_active=True
        if not bear and short_active:
            if Sqty>0: Scash+=Sqty*(Savg-r.Close)-Sqty*r.Close*FEE
            cash+=Scash;Scash=0;Sqty=0;Savg=0;Slayer=0;short_active=False
        cut=dt-pd.Timedelta(days=ld)
        sups=SL[(SL.confirm<=dt)&(SL.confirm>cut)].price.values
        ress=SH[(SH.confirm<=dt)&(SH.confirm>cut)].price.values
        # +14mo trim
        if pos>0 and r.Close>avg and mh>=14 and trim_done!=cur:
            for lvl in ress:
                if abs(r.Close-lvl)/lvl<=0.03:
                    sell=pos*0.20;cash+=sell*(r.Close-avg)-sell*r.Close*FEE
                    pos-=sell;trim_done=cur;layer=0;break
        # long buy
        in_bo=(17<=mh<30)
        if not in_bo and layer<5 and eqL>0:
            for lvl in sups:
                if abs(r.Close-lvl)/lvl<=0.03:
                    last=sup_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        gs=sum(1.5**k for k in range(5))
                        if layer==0: l1=eqL*mlev/gs; size=l1
                        else: size=l1*(1.5**layer)
                        size=min(size,max(0,mlev*eqL-pos*r.Close))
                        if size>1:
                            btc=size/r.Close;cash-=size*FEE
                            np_=pos+btc;avg=(pos*avg+btc*r.Close)/np_ if pos>0 else r.Close
                            pos=np_;layer+=1;sup_last[lvl]=dt
                        break
        # short (flat 3-layer)
        if short_active and Slayer<3:
            for lvl in ress:
                if abs(r.Close-lvl)/lvl<=0.03 and r.Close<=lvl*1.03:
                    last=Sres_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        size=(Scash*short_lev/3.0) if Slayer==0 else Sl1
                        if Slayer==0: Sl1=Scash*short_lev/3.0
                        size=min(size,max(0,short_lev*Scash-Sqty*r.Close))
                        if size>1:
                            btc=size/r.Close;Scash-=size*FEE
                            nq=Sqty+btc;Savg=(Sqty*Savg+btc*r.Close)/nq if Sqty>0 else r.Close
                            Sqty=nq;Slayer+=1;Sres_last[lvl]=dt
                        break
        if short_active and Sqty>0:
            if (r.High-Savg)/Savg>=0.30:
                Scash+=Sqty*(Savg-Savg*1.30)-Sqty*Savg*1.30*FEE;Sqty=0;Savg=0;Slayer=0
                Sres_last.clear();Ssup_last.clear()
            elif r.Close<Savg:
                for lvl in sups:
                    if abs(r.Close-lvl)/lvl<=0.03:
                        last=Ssup_last.get(lvl)
                        if last is None or (dt-last).days>=3:
                            cq=Sqty*0.33;Scash+=cq*(Savg-r.Close)-cq*r.Close*FEE
                            Sqty-=cq;Ssup_last[lvl]=dt;Slayer=0
                            if Sqty<1e-9:Sqty=0;Savg=0
                            break
        e=cash+(pos*(r.Close-avg) if pos>0 else 0)+(Scash+Sqty*(Savg-r.Close) if Sqty>0 else (Scash if short_active else 0))
        curve.append(e)
    s=pd.Series(curve,index=data.index)
    final=s.iloc[-1] if not liq else 0
    mdd=(s/s.cummax()-1).min() if (s>0).all() else -1
    return final, mdd, liq

print(f"\n{'='*100}")
print(f"低倍数周期压力测试 — 不同 cycle 涨幅 × 不同杠杆")
print(f"{'='*100}\n")
print(f"  {'cycle倍数':>8} {'杠杆':>10}  {'终值':>10}  {'倍数':>7}  {'最大回撤':>8}  {'vs现货':>7}  {'爆仓':>4}")
print(f"  {'-'*8} {'-'*10}  {'-'*10}  {'-'*7}  {'-'*8}  {'-'*7}  {'-'*4}")

for tm in [3.0, 3.5, 4.0, 8.0]:
    data = compress(data_real, tm) if tm<8 else data_real
    spot = INITIAL * data.Close.iloc[-1]/data.Close.iloc[0]  # spot from bottom to end
    spot_mult = spot/INITIAL
    for ll,lh,label in [(1.5,3.0,"1.5→3x"),(1.5,4.0,"1.5→4x"),(2.0,4.0,"2→4x"),(2.0,5.0,"2→5x")]:
        final,mdd,liq = run_v16(data, ll, lh)
        mult=final/INITIAL
        vs=f"{mult/spot_mult:.1f}x" if spot_mult>0 else "-"
        print(f"  {tm:>7.1f}x {label:>10}  ${final:>9,.0f}  {mult:>6.1f}x  {mdd*100:>+7.0f}%  {vs:>7}  {'是!' if liq else '否':>4}")
    print(f"     (现货持有从底到终 = {spot_mult:.1f}x)")
    print()
