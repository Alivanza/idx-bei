import sys, json, numpy as np, pandas as pd
sys.path.insert(0,'.'); import replay as R, configs as C, rets as X, evaluate as E
import argparse
ap=argparse.ArgumentParser(description="Threshold sensitivity: replay the F1-F5 screen over the Backfill dates, one threshold at a time.")
ap.add_argument("--cap",default="2026-11-18",help="last price date used for forward windows")
ap.add_argument("--prices",default="../idx_price_history.csv"); ap.add_argument("--log",default="../screen_log_export.csv")
ap.add_argument("--parquet",default="../../data/parquet"); ap.add_argument("--out",default=None)
ap.add_argument("--exclude-adjusted",action="store_true",help="drop rows whose forward window crosses an exchange price adjustment (|Previous/prior Close-1|>2%%): splits, e.g. PACK 12/01/2026, and ex-rights days")
a=ap.parse_args(); CAP,CSV,PARQ=a.cap,a.prices,a.parquet
log=pd.read_csv(a.log,dtype=str)
bf=log[log.Source=="Backfill"].drop_duplicates(["LastTradingDate","Ticker"])
gen=bf.groupby("Ticker").Sector.first().to_dict()
ctx=R.prepare(PARQ, dates_wanted=set(bf.LastTradingDate)); ctx["sector"]={**ctx["sector"],**gen}
ret=X.Returns(None,CSV,CAP,"csv")
cfgs=C.configs(); sl={}
for key,label,v,fmt,isb,cfg in cfgs: sl[(key,fmt)]=R.shortlists(ctx,cfg)
base_key=("MIN_ADTV","5")
sl[("LOG","log")]={d:bf[bf.LastTradingDate==str(pd.Timestamp(d).date())].Ticker.tolist() for d in ctx["dates"]}   # the actual logged Backfill rows
excl=None
if a.exclude_adjusted:
    import bisect, numpy as np
    adj=X.Returns(PARQ,CSV,CAP,"parquet")
    def excl(t,d,n):
        if t not in adj.S: return False
        D,O,C_,A=adj.S[t]; i=bisect.bisect_left(D,d)
        return bool(i<len(D) and D[i]==d and (np.abs(A[i+2:i+n+1]-1)>0.02).any())
res=E.evaluate(sl,ret,ctx,excl=excl)
rows=[]
for key,label,v,fmt,isb,cfg in cfgs:
    for n in (7,14):
        r=res[((key,fmt),n)]; b=res[(base_key,n)]
        net,bn=r["net"],b["net"]; com=net.index.intersection(bn.index); diff=(net[com]-bn[com])
        rows.append(dict(Param=label,Key=key,Value=fmt,IsCurrent=isb,H=n,Dates=len(net),Rows=r["rows"],RowsPerDate=round(r["rows"]/len(sl[(key,fmt)]),2),Unpriced=r["miss"],
            Gross=r["gross"].mean(),NetA=net.mean(),SD=net.std(ddof=1),NWt=E.nw_t(net.values,n-1),Cost=r["cost"].mean(),
            dNetA=(0.0 if isb else diff.mean()),dT=(np.nan if isb else E.nw_t(diff.values,n-1)),dDates=len(com)))
df=pd.DataFrame(rows); df.to_csv(a.out or f"threshold_sensitivity_{CAP}.csv",index=False)
pd.set_option("display.width",250); pd.set_option("display.max_columns",30)
print(df.round(3).to_string(index=False))
for nm in [("LOG","log"),base_key]:
    for n in (7,14):
        r=res[(nm,n)]; print(nm,n,"dates",len(r["net"]),"gross",round(r["gross"].mean(),2),"netA",round(r["net"].mean(),2),"NWt",round(E.nw_t(r["net"].values,n-1),2),"unpriced",r["miss"])
