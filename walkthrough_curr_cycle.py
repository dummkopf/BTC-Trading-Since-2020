"""
Narrated walkthrough of the v16 strategy on the PREVIOUS cycle.
$7000 start, entry 2018-12-15 (cycle bottom $3,237) through 2022-06.

Logs EVERY action with: date, BTC price, what fired, size, and running
state (position, avg cost, cash, equity, leverage). So the user can
FEEL the journey trade by trade.
"""
import pandas as pd, numpy as np

df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
weekly = df.resample("W-MON").agg(Open=("Open","first"), High=("High","max"),
                                    Low=("Low","min"), Close=("Close","last")).dropna()
def detect_swings(wb, w):
    highs, lows = [], []
    for i in range(w, len(wb)-w):
        win = wb.iloc[i-w:i+w+1]
        if wb.iloc[i].High == win.High.max(): highs.append((wb.index[i+w], wb.iloc[i].High))
        if wb.iloc[i].Low == win.Low.min():  lows.append((wb.index[i+w], wb.iloc[i].Low))
    return pd.DataFrame(highs, columns=["confirm","price"]), pd.DataFrame(lows, columns=["confirm","price"])
SH, SL = detect_swings(weekly, 2)

INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005
HALVINGS=[pd.Timestamp(d) for d in ["2016-07-09","2020-05-11","2024-04-20","2028-04-15"]]
def msh(dt):
    p=[h for h in HALVINGS if h<=dt]
    return (dt-p[-1]).days/30.44 if p else -999

data = df.loc["2022-11-21":"2026-05-21"]

# state
cash=INITIAL; pos=0.0; avg=0.0; layer=0; l1=0
Scash=0.0; Sqty=0.0; Savg=0.0; Slayer=0; Sl1=0; short_active=False
sup_last={}; res_last={}; Sres_last={}; Ssup_last={}
peak_mult=1.0; fe_done=None; trim_done=None
lifetime_days=26*7

def money(x): return f"${x:,.0f}"

print(f"\n{'='*108}")
print(f"  v16 策略全程复盘 — 当前周期 (2022-11 底 → 2026-05)")
print(f"  起始本金 $7,000 | 入场 2018-12-15 BTC ${data.Close.iloc[0]:,.0f}")
print(f"{'='*108}")
print(f"\n  规则速记:")
print(f"    多头: 触及周线支撑 ±3% 买入, 倒金字塔 5 层 (先小后大 1.5x), 杠杆 1.5x→3x(翻倍后)")
print(f"    保命 trim: halving+14月 触阻力卖 20% (仅一次)")
print(f"    逃顶: halving+18月 全平多头")
print(f"    做空: halving+18~30月, 30% 现金, 1.5x, 3层平均, 触阻力进/触支撑平, +30%止损")
print(f"    halving 锚 = 2024-04-20")

events=[]
for i,(dt,r) in enumerate(data.iterrows()):
    mh=msh(dt)
    past=[h for h in HALVINGS if h<=dt]; cur_h=past[-1] if past else None
    bear=(18<=mh<30)

    # funding
    if pos>0: cash-=pos*r.Close*FUNDING
    if Sqty>0: Scash-=Sqty*r.Close*FUNDING*0.5

    # liquidation check
    if pos>0:
        if cash+pos*(r.Low-avg)<=pos*r.Low*MM:
            print(f"\n  💀 {dt.date()} LIQUIDATION @ ${r.Close:,.0f}"); break

    eq_long = cash+pos*(r.Close-avg) if pos>0 else cash
    eq_short = Scash+Sqty*(Savg-r.Close) if Sqty>0 else Scash
    eq = eq_long+eq_short
    peak_mult=max(peak_mult,eq/INITIAL)
    maxlev = 3.0 if peak_mult>1.5 else 1.5

    # FORCE EXIT (long) — before short allocation
    if pos>0 and 18<=mh<24 and fe_done!=cur_h:
        val=pos*r.Close; pnl=pos*(r.Close-avg)-val*FEE
        cash+=pnl
        events.append(("🏁 FORCE EXIT 全平多头", dt, r.Close,
            f"halving+{mh:.0f}月 时间锚定逃顶 | 平掉 {pos:.4f} BTC @ avg ${avg:,.0f} | 实现盈亏 {money(pnl)}",
            cash, 0, 0, eq))
        pos=0; avg=0; layer=0; l1=0; fe_done=cur_h
        sup_last.clear(); res_last.clear()

    # bear short allocation (AFTER force exit)
    if bear and not short_active and cash>0:
        Scash=cash*0.30; cash-=Scash; short_active=True
        events.append(("🔻 启动熊市做空", dt, r.Close,
            f"halving+{mh:.0f}月 进入熊市窗口 | 分配 30% 现金 = {money(Scash)} 做空 (其余 {money(cash)} 持币)",
            cash, pos, avg, eq))

    # bear end → cover + return cash
    if not bear and short_active:
        if Sqty>0:
            pnl=Sqty*(Savg-r.Close)-Sqty*r.Close*FEE; Scash+=pnl
        cash+=Scash; Scash=0; Sqty=0; Savg=0; Slayer=0; short_active=False

    # active S/R
    cut=dt-pd.Timedelta(days=lifetime_days)
    sups=SL[(SL.confirm<=dt)&(SL.confirm>cut)].price.values
    ress=SH[(SH.confirm<=dt)&(SH.confirm>cut)].price.values

    # PRE-EXIT TRIM (halving+14, once)
    if pos>0 and r.Close>avg and mh>=14 and trim_done!=cur_h:
        for lvl in ress:
            if abs(r.Close-lvl)/lvl<=0.03:
                sell=pos*0.20; pnl=sell*(r.Close-avg)-sell*r.Close*FEE; cash+=pnl
                events.append(("✂️  保命 TRIM 20%", dt, r.Close,
                    f"halving+{mh:.0f}月 + 触及阻力 ${lvl:,.0f} | 卖 {sell:.4f} BTC | 实现 {money(pnl)} | 留 {pos-sell:.4f} BTC",
                    cash, pos-sell, avg, eq))
                pos-=sell; trim_done=cur_h; layer=0; l1=0
                break

    # LONG BUY (inverted pyramid)
    in_bo=(17<=mh<30)
    if not in_bo and layer<5 and pos>=0:
        if eq_long>0:
            for lvl in sups:
                if abs(r.Close-lvl)/lvl<=0.03:
                    last=sup_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        if layer==0:
                            gs=sum(1.5**k for k in range(5)); l1=eq_long*maxlev/gs; size=l1
                        else:
                            size=l1*(1.5**layer)
                        size=min(size,max(0,maxlev*eq_long-pos*r.Close))
                        if size>1:
                            btc=size/r.Close; cash-=size*FEE
                            npos=pos+btc; avg=(pos*avg+btc*r.Close)/npos if pos>0 else r.Close
                            pos=npos; layer+=1; sup_last[lvl]=dt
                            lev=pos*r.Close/eq_long if eq_long>0 else 0
                            events.append((f"🟢 多头 L{layer} 买入", dt, r.Close,
                                f"触及支撑 ${lvl:,.0f} (±3%) | 买 {money(size)} = {btc:.4f} BTC | 占目标 {size/(eq_long*maxlev)*100:.0f}% | 新均价 ${avg:,.0f} | 杠杆 {lev:.2f}x",
                                cash, pos, avg, eq))
                        break

    # SHORT ENTRY (flat 3-layer)
    if short_active and Slayer<3:
        for lvl in ress:
            if abs(r.Close-lvl)/lvl<=0.03 and r.Close<=lvl*1.03:
                last=Sres_last.get(lvl)
                if last is None or (dt-last).days>=3:
                    if Slayer==0:
                        gs=3.0; Sl1=Scash*1.5/gs; size=Sl1
                    else:
                        size=Sl1
                    size=min(size,max(0,1.5*Scash-Sqty*r.Close))
                    if size>1:
                        btc=size/r.Close; Scash-=size*FEE
                        nq=Sqty+btc; Savg=(Sqty*Savg+btc*r.Close)/nq if Sqty>0 else r.Close
                        Sqty=nq; Slayer+=1; Sres_last[lvl]=dt
                        events.append((f"🔴 空头 S{Slayer} 做空", dt, r.Close,
                            f"触及阻力 ${lvl:,.0f} | 做空 {money(size)} = {btc:.4f} BTC | 空头均价 ${Savg:,.0f}",
                            cash, pos, avg, eq))
                    break

    # SHORT stop / cover
    if short_active and Sqty>0:
        if (r.High-Savg)/Savg>=0.30:
            pnl=Sqty*(Savg-Savg*1.30)-Sqty*Savg*1.30*FEE; Scash+=pnl
            events.append(("🛑 空头止损 +30%", dt, r.Close,
                f"反弹超空头均价 +30% (${Savg*1.3:,.0f}) | 平 {Sqty:.4f} BTC | 实现 {money(pnl)}",
                cash, pos, avg, eq))
            Sqty=0; Savg=0; Slayer=0; Sres_last.clear(); Ssup_last.clear()
        elif r.Close<Savg:
            for lvl in sups:
                if abs(r.Close-lvl)/lvl<=0.03:
                    last=Ssup_last.get(lvl)
                    if last is None or (dt-last).days>=3:
                        cq=Sqty*0.33; pnl=cq*(Savg-r.Close)-cq*r.Close*FEE; Scash+=pnl
                        events.append(("🟡 空头平仓 1/3", dt, r.Close,
                            f"触及支撑 ${lvl:,.0f} | 平 {cq:.4f} BTC | 实现 {money(pnl)} | 剩 {Sqty-cq:.4f} BTC",
                            cash, pos, avg, eq))
                        Sqty-=cq; Ssup_last[lvl]=dt; Slayer=0
                        if Sqty<1e-9: Sqty=0; Savg=0
                        break

# print all events
print(f"\n{'='*108}")
print(f"  逐笔操作 (共 {len(events)} 笔)")
print(f"{'='*108}")
last_eq=INITIAL
for j,(action,dt,price,desc,c,p,a,eq) in enumerate(events):
    print(f"\n  [{j+1:2}] {dt.date()}  BTC ${price:,.0f}   {action}")
    print(f"       {desc}")
    print(f"       → 账户权益 {money(eq)} ({eq/INITIAL:.2f}x)  现金 {money(c)}  多头 {p:.4f} BTC @ ${a:,.0f}")

# final
final_eq = cash + (pos*(data.Close.iloc[-1]-avg) if pos>0 else 0) + (Scash+Sqty*(Savg-data.Close.iloc[-1]) if Sqty>0 else (Scash if short_active else 0))
print(f"\n{'='*108}")
print(f"  终局 ({data.index[-1].date()}, BTC ${data.Close.iloc[-1]:,.0f}):  权益 {money(final_eq)}  =  {final_eq/INITIAL:.2f}x")
print(f"{'='*108}")
