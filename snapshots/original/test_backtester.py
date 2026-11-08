import pandas as pd, numpy as np, tempfile, subprocess, sys, json, os
n=3000; idx=pd.date_range('2025-01-01',periods=n,freq='5min',tz='UTC'); ret=np.random.default_rng(1).normal(0,0.001,n); close=50000*np.exp(np.cumsum(ret)); open_=np.r_[close[0],close[:-1]]; high=np.maximum(open_,close)*1.001; low=np.minimum(open_,close)*0.999; vol=np.random.default_rng(2).integers(100,1000,n)
pd.DataFrame({'timestamp':idx,'open':open_,'high':high,'low':low,'close':close,'volume':vol}).to_csv('/tmp/testbtc.csv',index=False)
r=subprocess.run([sys.executable,'backtest.py','--csv','/tmp/testbtc.csv','--out','/tmp/testout'],capture_output=True,text=True); print(r.stdout); assert r.returncode==0, r.stderr
print(json.load(open('/tmp/testout/summary.json')))
