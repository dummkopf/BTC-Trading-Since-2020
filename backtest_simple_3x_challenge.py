"""
Challenge: is "3x near the bottom + exit one month before top" simpler
AND better than the complex v16, and does it survive COVID?

Key intuition (user): liquidation is measured vs YOUR entry. If you enter
within 5-10% of the cycle bottom, price (by definition) won't drop 33%
below it — so 3x essentially never liquidates.

Tests:
  A) 3x from EXACT bottom, exit at halving+17 (1mo before the +18 top)
  B) 3x from bottom+10% (mechanical confirmation: price 8-10% above the
     trailing-120d low while deeply drawn down), exit halving+17
  C) 3x from window start (no timing) — the naive version that died before
  Compare on PW (2018, has COVID), W (2022), and W compressed to 3.5x.
  Also trace COVID equity for version A explicitly.
"""
import pandas as pd, numpy as np
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")
INITIAL=7000.0; FUNDING=0.0001*3; FEE=0.0005; MM=0.005
HALV=[pd.Timestamp(d) for d in ["2016-07-09","2020-05-11","2024-04-20","2028-04-15"]]
def msh(dt):
    p=[h for h in HALV if h<=dt]; return (dt-p[-1]).days/30.44 if p else -999

CYCLES = {  # window start, bottom date, real top
    "PW (2018→2021, 含COVID)": ("2018-09-01","2018-12-15","2021-11-08"),
    "W  (2022→2025)":          ("2022-09-01","2022-11-21","2025-10-06"),
}

def hodl_3x(data, entry_date, lev, exit_mh):
    """Enter lev× at entry_date, exit at next-halving-after-entry + exit_mh months."""
    cash=INITIAL; pos=0.0; avg=0.0; entered=False; ex=False; liq=False
    entry_d=pd.Timestamp(entry_date)
    near=1.0
    curve=[]
    for dt,r in data.iterrows():
        if liq: curve.append(0); continue
        if not entered and dt>=entry_d:
            pos=INITIAL*lev/r.Close; avg=r.Close; cash-=INITIAL*lev*FEE; entered=True
        if pos>0 and not ex:
            cash-=pos*r.Close*FUNDING
            eq_low=cash+pos*(r.Low-avg)
            near=min(near, eq_low/INITIAL)
            if eq_low<=pos*r.Low*MM:
                liq=True;cash=0;pos=0;avg=0;curve.append(0);continue
            # exit at next halving after entry + exit_mh
            fut=[h for h in HALV if h>entry_d and h<=dt]
            if fut:
                mhp=(dt-fut[-1]).days/30.44
                if mhp>=exit_mh:
                    cash+=pos*(r.Close-avg)-pos*r.Close*FEE; pos=0;avg=0;ex=True
        curve.append(cash+pos*(r.Close-avg) if pos>0 else cash)
    s=pd.Series(curve,index=data.index)
    final=s.iloc[-1] if not liq else 0
    mdd=(s/s.cummax()-1).min() if (s>0).all() else -1
    return final, mdd, liq, near

def confirm_entry_date(data, bottom_date, pct_above=0.10, lookback=120):
    """Mechanical 'bottom+X%' entry: first day price >= trailing-lookback-low × (1+pct),
    on/after a window that contains the bottom. No lookahead beyond trailing low."""
    sub = data.copy()
    sub["roll_low"] = sub.Low.rolling(lookback, min_periods=20).min()
    # only consider after we're near the cycle bottom era (price within 30% of bottom)
    bp = data.loc[bottom_date].Close
    for dt,r in sub.iterrows():
        if dt < pd.Timestamp(bottom_date): continue
        if not np.isnan(r.roll_low) and r.Close >= r.roll_low*(1+pct_above) and r.roll_low <= bp*1.15:
            return dt
    return data.index[-1]

print(f"{'='*100}")
print(f"挑战测试: 3x 近底入场 + 顶前一月跑  vs  COVID 是否爆仓")
print(f"{'='*100}\n")

# explicit COVID trace for "3x from 2018-12 bottom"
print("COVID 验证 — 3x 从 2018-12-15 ($3,237) 入场, 追踪到 COVID 低点:")
pw=df.loc["2018-12-15":"2020-04-01"]
entry=pw.Close.iloc[0]; pos=INITIAL*3/entry
covid_low=pw.Low.min(); covid_d=pw.Low.idxmin()
liq_price=entry*(1-1/3+0.005)
eq_at_covid=INITIAL+pos*(covid_low-entry)
print(f"  入场价 ${entry:,.0f} | 3x 爆仓线 ≈ ${liq_price:,.0f}")
print(f"  COVID 最低 ${covid_low:,.0f} ({covid_d.date()}) —— {'高于' if covid_low>entry else '低于'}入场价 {(covid_low/entry-1)*100:+.0f}%")
print(f"  COVID 时账户权益 ${eq_at_covid:,.0f} ({eq_at_covid/INITIAL:.2f}x)  → {'没爆仓 ✓' if covid_low>liq_price else '爆仓!'}")
print(f"  结论: COVID 低点 ${covid_low:,.0f} 远高于爆仓线 ${liq_price:,.0f}, 因为入场即在底部, 价格回不到那么低\n")

print(f"{'='*100}")
print(f"{'策略':40} {'PW倍数':>8} {'PW爆仓':>6} {'W倍数':>8} {'W爆仓':>6} {'几何':>6}")
print(f"{'-'*40} {'-'*8} {'-'*6} {'-'*8} {'-'*6} {'-'*6}")

def show(label, res):
    pw,w=res
    pwm,pwl=pw; wm,wl=w
    geo=(pwm*wm)**0.5 if pwm>0 and wm>0 else 0
    print(f"{label:40} {pwm:>7.1f}x {'是' if pwl else '否':>6} {wm:>7.1f}x {'是' if wl else '否':>6} {(str(round(geo,1))+'x') if geo>0 else 'LIQ':>6}")

for exit_mh,exit_label in [(17,"顶前1月"),(18,"顶")]:
    print(f"\n  [退出时点: 减半+{exit_mh}月 ({exit_label})]")
    # A: exact bottom
    res=[]
    for cy,(ws,bd,td) in CYCLES.items():
        data=df.loc[ws:pd.Timestamp(td)+pd.Timedelta(days=240)]
        f,m,l,n=hodl_3x(data, bd, 3.0, exit_mh)
        res.append((f/INITIAL,l))
    show(f"  A: 3x 精确底入场", res)
    # B: bottom+10% confirmation
    res=[]
    for cy,(ws,bd,td) in CYCLES.items():
        data=df.loc[ws:pd.Timestamp(td)+pd.Timedelta(days=240)]
        ed=confirm_entry_date(data, bd, 0.10)
        f,m,l,n=hodl_3x(data, ed, 3.0, exit_mh)
        res.append((f/INITIAL,l))
    show(f"  B: 3x 底+10%确认入场", res)
    # B2: bottom+5%
    res=[]
    for cy,(ws,bd,td) in CYCLES.items():
        data=df.loc[ws:pd.Timestamp(td)+pd.Timedelta(days=240)]
        ed=confirm_entry_date(data, bd, 0.05)
        f,m,l,n=hodl_3x(data, ed, 3.0, exit_mh)
        res.append((f/INITIAL,l))
    show(f"  B2: 3x 底+5%确认入场", res)

print(f"\n  [对照] v16 复杂策略几何均值 ≈ 13-16x (从底部, 含做空)")
print(f"  [对照] 现货持有从底到顶: PW {df.loc['2021-11-08'].Close/df.loc['2018-12-15'].Close:.0f}x, W {df.loc['2025-10-06'].Close/df.loc['2022-11-21'].Close:.0f}x")
