"""
Mid-bull dip-buy sleeve (SEPARATE from the core hold).

Idea: core 3x hold-from-bottom is NEVER touched (keeps 97% margin).
Use EXTRA cash to buy mid-bull corrections of >=20% and sell when price
recovers to the pre-dip peak. Because this is separate capital, it does
NOT affect the core's avg cost or liquidation line.

Questions:
1. How many >=20% mid-bull corrections actually happen, how deep, how long
   to recover to the pre-dip peak?
2. Does buying the dip (at various trigger depths & leverage) and selling
   at recovery make money NET, and what's the liquidation risk on the dip
   leg (it can keep falling past your entry)?
3. OOS: does it generalize across both cycles?
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005

# Bull windows (bottom to halving+17 region — use the run-up to top)
BULLS = {"PW (2018-12→2021-11)": ("2018-12-15","2021-11-08"),
         "W  (2022-11→2025-10)": ("2022-11-21","2025-10-06")}

# ---- 1. Catalog all >=20% drawdowns from running peak within the bull ----
print("="*100)
print("牛市内 >=20% 回撤盘点 (从局部峰值算, 直到恢复到峰值前)")
print("="*100)
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    print(f"\n{cy}:")
    peak=data.Close.iloc[0]; peak_dt=data.index[0]
    in_dd=False; trough=peak; trough_dt=peak_dt
    events=[]
    for dt,r in data.iterrows():
        if r.Close>peak:
            if in_dd:
                # recovered to peak — close the dd event
                events.append((peak_dt,peak,trough_dt,trough,dt))
                in_dd=False
            peak=r.Close; peak_dt=dt; trough=r.Close; trough_dt=dt
        else:
            if r.Close<=peak*0.80 and not in_dd:
                in_dd=True
            if r.Close<trough:
                trough=r.Close; trough_dt=dt
    print(f"  {'峰值日':12}{'峰值$':>10}  {'谷底日':12}{'谷底$':>10}  {'跌幅':>6}  {'恢复天数':>8}")
    for pd_dt,pv,td,tv,rd in events:
        dd=(tv/pv-1)*100
        rec_days=(rd-pd_dt).days
        print(f"  {pd_dt.date()!s:12}${pv:>8,.0f}  {td.date()!s:12}${tv:>8,.0f}  {dd:>+5.0f}%  {rec_days:>8}")
    print(f"  → 共 {len(events)} 次 >=20% 回撤")

# ---- 2. Simulate dip-buy sleeve: buy at -trigger% from peak, sell at recovery to peak ----
def dip_sleeve(data, sleeve_cash, trigger, lev, exit_at='peak'):
    """Buy when price <= running_peak*(1-trigger) (not already in position).
    Sell when price >= entry_peak (recovered to pre-dip peak) OR liquidation."""
    cash=sleeve_cash; pos=0.0; avg=0.0; entry_peak=None
    peak=data.Close.iloc[0]; liq=False; trades=0; wins=0; near=1.0
    curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if r.Close>peak and pos==0: peak=r.Close
        if pos>0:
            cash-=pos*r.Close*FUNDING
            eq_low=cash+pos*(r.Low-avg); near=min(near,eq_low/sleeve_cash)
            if eq_low<=pos*r.Low*MM: liq=True;cash=0;pos=0;curve.append(0);continue
            # sell at recovery to pre-dip peak
            if r.High>=entry_peak:
                cash+=pos*(entry_peak-avg)-pos*entry_peak*FEE
                if entry_peak>avg: wins+=1
                pos=0;avg=0; peak=entry_peak; entry_peak=None
        else:
            # buy the dip
            if r.Close<=peak*(1-trigger):
                eq=cash
                notional=eq*lev
                pos=notional/r.Close; cash-=notional*FEE; avg=r.Close
                entry_peak=peak; trades+=1
        eq_now=cash+pos*(r.Close-avg) if pos>0 else cash
        curve.append(eq_now)
    # close any residual at end price
    if pos>0: cash+=pos*(data.Close.iloc[-1]-avg)
    final=cash if not liq else 0
    return final/sleeve_cash, liq, trades, wins, near

print(f"\n{'='*100}")
print(f"吃回撤 sleeve: 跌 trigger% 买入, 恢复到峰值卖出 (独立资金, 核心仓不动)")
print(f"{'='*100}")
for cy,(s,e) in BULLS.items():
    data=df.loc[s:e]
    print(f"\n{cy}:")
    print(f"  {'trigger':>8}{'lev':>5}  {'sleeve倍数':>9}{'爆仓':>5}{'交易':>5}{'胜':>4}{'近爆仓':>7}")
    for trig in [0.20,0.25,0.30]:
        for lev in [1.0,1.5,2.0,3.0]:
            m,liq,tr,wn,near=dip_sleeve(data,1000.0,trig,lev)
            print(f"  {int(trig*100):>7}%{lev:>5.1f}  {m:>8.2f}x{'是!' if liq else '否':>5}{tr:>5}{wn:>4}{near*100:>6.0f}%")
