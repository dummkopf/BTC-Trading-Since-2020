"""
Halving-month micro-pattern analysis.

User's hypothesis: local top ~1 month BEFORE halving + local bottom ~1
month AFTER halving (not necessarily exactly 1 month).

Tests on the 3 halvings in our data (2016-07, 2020-05, 2024-04).
Identifies the local high and low within ±6 months of each halving and
measures the timing offset relative to halving day.

Result: 2/3 confirm the pattern; 2020 was COVID-disrupted (the
-52% crash happened 2 months BEFORE halving, consuming what would
otherwise have been a post-halving dip).

Tactical implication: a -18 to -24% post-halving dip is statistically
common and represents a good leverage upgrade point IF you started at
lower leverage. But n=3 with 1 outlier is too small a sample to be
treated as a law.
"""
import pandas as pd
df = pd.read_csv("/tmp/btc_long.csv", parse_dates=["Date"]).sort_values("Date").set_index("Date")

def get_price(d):
    idx = df.index.get_indexer([pd.Timestamp(d)], method='nearest')[0]
    return df.iloc[idx].Close, df.index[idx]

HALV = [pd.Timestamp(d) for d in ["2016-07-09","2020-05-11","2024-04-20"]]

print(f"{'='*100}")
print(f"减半 ±6 月微观模式 — 三次减半")
print(f"{'='*100}")

for h in HALV:
    sub = df.loc[h-pd.Timedelta(days=180):h+pd.Timedelta(days=180)]
    if len(sub)<10: continue
    halv_p, _ = get_price(h)
    pre = sub.loc[:h]; post = sub.loc[h:]

    print(f"\n减半 {h.date()}  BTC ${halv_p:,.0f}")
    print(f"  前 6 月: 局部高 {pre.High.idxmax().date()} ${pre.High.max():,.0f} "
          f"(减半前 {(h-pre.High.idxmax()).days/30.44:.1f} 月, "
          f"{(pre.High.max()/halv_p-1)*100:+.0f}%)")
    print(f"  后 6 月: 局部低 {post.Low.idxmin().date()} ${post.Low.min():,.0f} "
          f"(减半后 {(post.Low.idxmin()-h).days/30.44:.1f} 月, "
          f"{(post.Low.min()/halv_p-1)*100:+.0f}%)")

print(f"\n{'='*100}\n假设检验: 前 0-3 月有局部顶 + 后 0-4 月有局部底 + 跌幅 < -5%")
print(f"{'='*100}")
matches=[]
for h in HALV:
    sub = df.loc[h-pd.Timedelta(days=180):h+pd.Timedelta(days=180)]
    halv_p, _ = get_price(h)
    pre_off = (h-sub.loc[:h].High.idxmax()).days/30.44
    post_off = (sub.loc[h:].Low.idxmin()-h).days/30.44
    drop = (sub.loc[h:].Low.min()/halv_p - 1)*100
    ok = (0<=pre_off<=3) and (0<=post_off<=4) and drop<-5
    matches.append(ok)
    print(f"  {h.date()!s:12}  前高 {pre_off:>4.1f}月  后低 {post_off:>4.1f}月  跌 {drop:>+5.0f}%  {'✓' if ok else '✗'}")
print(f"\n→ {sum(matches)}/3 符合. 2020 是 COVID 异常 (前 2 月已 -52%, 消化了 post 回调)")
