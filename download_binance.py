import argparse, time, requests, pandas as pd, os
URL='https://fapi.binance.com/fapi/v1/klines'

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--symbol',default='BTCUSDT'); ap.add_argument('--interval',default='5m'); ap.add_argument('--start',default='2021-01-01'); ap.add_argument('--end',default='2026-01-01'); ap.add_argument('--out',default='data/BTCUSDT_5m.csv'); a=ap.parse_args()
    start=int(pd.Timestamp(a.start,tz='UTC').timestamp()*1000); end=int(pd.Timestamp(a.end,tz='UTC').timestamp()*1000)
    rows=[]
    while start<end:
        r=requests.get(URL,params={'symbol':a.symbol,'interval':a.interval,'startTime':start,'endTime':end,'limit':1500},timeout=30); r.raise_for_status(); data=r.json()
        if not data: break
        rows.extend(data); start=data[-1][0]+1; print(len(rows),pd.to_datetime(start,unit='ms',utc=True),flush=True); time.sleep(0.05)
    cols=['open_time','open','high','low','close','volume','close_time','quote_volume','trades','taker_base','taker_quote','ignore']
    df=pd.DataFrame(rows,columns=cols); df['timestamp']=pd.to_datetime(df.open_time,unit='ms',utc=True)
    df[['timestamp','open','high','low','close','volume']].to_csv(a.out,index=False); print('wrote',a.out,len(df))
if __name__=='__main__': main()
