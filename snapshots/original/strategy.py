from dataclasses import dataclass
import numpy as np
import pandas as pd


def ema(s, n): return s.ewm(span=n, adjust=False, min_periods=n).mean()
def sma(s, n): return s.rolling(n, min_periods=n).mean()
def atr(df, n=14):
    prev = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-prev).abs(), (df.low-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()
def rsi(s, n=14):
    d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
    au=up.ewm(alpha=1/n, adjust=False, min_periods=n).mean(); ad=dn.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
    return 100-(100/(1+au/ad.replace(0,np.nan)))
def adx(df,n=14):
    up=df.high.diff(); dn=-df.low.diff()
    plus=np.where((up>dn)&(up>0),up,0.0); minus=np.where((dn>up)&(dn>0),dn,0.0)
    tr=pd.concat([(df.high-df.low),(df.high-df.close.shift()).abs(),(df.low-df.close.shift()).abs()],axis=1).max(axis=1)
    atrv=tr.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
    p=100*pd.Series(plus,index=df.index).ewm(alpha=1/n,adjust=False,min_periods=n).mean()/atrv
    m=100*pd.Series(minus,index=df.index).ewm(alpha=1/n,adjust=False,min_periods=n).mean()/atrv
    dx=100*(p-m).abs()/(p+m).replace(0,np.nan)
    return dx.ewm(alpha=1/n,adjust=False,min_periods=n).mean()
def zscore(s,n):
    m=s.rolling(n,min_periods=n).mean(); sd=s.rolling(n,min_periods=n).std()
    return (s-m)/sd.replace(0,np.nan)

@dataclass
class Params:
    risk_per_trade=0.0025
    daily_loss_limit=0.015
    weekly_loss_limit=0.03
    max_drawdown=0.05
    max_leverage=2.0
    min_confidence=78.0
    min_rr=1.5
    fee_bps=4.0
    slippage_bps=2.0
    max_bars=288  # 24h on 5m
    atr_stop_mult=1.8

class Strategy:
    def __init__(self,p=None): self.p=p or Params()

    def features(self, raw):
        df=raw.copy(); df.index=pd.to_datetime(df.timestamp,utc=True) if 'timestamp' in df else pd.to_datetime(df.index,utc=True)
        df=df.sort_index().loc[~df.index.duplicated()]
        for c in ['open','high','low','close','volume']:
            df[c]=pd.to_numeric(df[c],errors='coerce')
        if 'funding_rate' not in df: df['funding_rate']=0.0
        if 'open_interest' not in df: df['open_interest']=np.nan
        if 'orderbook_imbalance' not in df: df['orderbook_imbalance']=0.0
        if 'spread_bps' not in df: df['spread_bps']=0.0
        if 'news_sentiment' not in df: df['news_sentiment']=0.0
        return df.dropna(subset=['open','high','low','close','volume'])

    def _tf(self, df, rule):
        x=df[['open','high','low','close','volume','funding_rate','open_interest','orderbook_imbalance','spread_bps','news_sentiment']].resample(rule,label='right',closed='right').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum','funding_rate':'last','open_interest':'last','orderbook_imbalance':'last','spread_bps':'last','news_sentiment':'last'}).dropna(subset=['open','high','low','close'])
        x['atr']=atr(x,14); x['rsi']=rsi(x.close,14); x['adx']=adx(x,14)
        x['ema20']=ema(x.close,20); x['ema50']=ema(x.close,50); x['ema200']=ema(x.close,200)
        x['vwap_proxy']= (x.close*x.volume).rolling(20,min_periods=20).sum()/x.volume.rolling(20,min_periods=20).sum()
        x['ret']=x.close.pct_change(); x['vol_z']=zscore(x.volume,26); x['mom']=x.close.pct_change(12)
        x['hh']=x.high.rolling(20,min_periods=20).max().shift(1); x['ll']=x.low.rolling(20,min_periods=20).min().shift(1)
        x['oi_chg']=x.open_interest.pct_change().replace([np.inf,-np.inf],np.nan).fillna(0)
        return x

    def prepare(self, raw):
        base=self.features(raw); t5=base.copy(); t15=self._tf(base,'15min'); t1=self._tf(base,'1h'); t4=self._tf(base,'4h')
        # Shift higher TF features by one completed candle before aligning to 5m.
        cols=[c for c in t4.columns if c not in []]
        a=t4[cols].shift(1).reindex(t5.index,method='ffill').add_prefix('h4_')
        b=t1[cols].shift(1).reindex(t5.index,method='ffill').add_prefix('h1_')
        c=t15[cols].shift(1).reindex(t5.index,method='ffill').add_prefix('m15_')
        out=pd.concat([t5,a,b,c],axis=1)
        return out

    def signal(self,row):
        # Macro regime
        bull4 = row.h4_close>row.h4_ema50>row.h4_ema200 and row.h4_adx>=20
        bear4 = row.h4_close<row.h4_ema50<row.h4_ema200 and row.h4_adx>=20
        range4 = row.h4_adx<18
        bull1 = row.h1_close>row.h1_ema50>row.h1_ema200
        bear1 = row.h1_close<row.h1_ema50<row.h1_ema200
        highvol = row.h4_atr/row.h4_close > 0.035
        if highvol and not (bull4 or bear4): return ('NONE',0,None)
        regime='BULL' if bull4 and bull1 else 'BEAR' if bear4 and bear1 else 'RANGE' if range4 else 'NONE'
        if regime=='NONE': return ('NONE',0,None)

        trend=0
        trend += 25 if (bull1 if regime=='BULL' else bear1 if regime=='BEAR' else True) else 0
        trend += 20 if row.h1_adx>=20 else 0
        trend += 15 if ((row.h1_ema20>row.h1_ema50) if regime=='BULL' else (row.h1_ema20<row.h1_ema50) if regime=='BEAR' else True) else 0
        trend += 10 if ((row.m15_close>row.m15_vwap_proxy) if regime=='BULL' else (row.m15_close<row.m15_vwap_proxy) if regime=='BEAR' else True) else 0
        trend += 10 if ((row.m15_rsi>=52) if regime=='BULL' else (row.m15_rsi<=48) if regime=='BEAR' else True) else 0
        trend += 10 if abs(row.m15_mom)>0.005 else 0
        trend += 10 if (row.m15_vol_z>=0.5) else 0

        pos=50
        if row.funding_rate>0.0005: pos-=10
        if row.funding_rate<-0.0005: pos+=10
        if row.orderbook_imbalance>0.10: pos+=8
        if row.orderbook_imbalance<-0.10: pos-=8
        if pd.notna(row.open_interest):
            if row.oi_chg>0.03 and row.m15_mom>0: pos+=5
            if row.oi_chg>0.03 and row.m15_mom<0: pos-=5
        liq=max(0,100-min(100,row.spread_bps*5))
        sent=50+25*np.clip(row.news_sentiment,-1,1)

        long_setup=(regime=='BULL' and row.m15_close>row.m15_hh and row.m15_vol_z>=0.5 and row.m15_rsi<75)
        short_setup=(regime=='BEAR' and row.m15_close<row.m15_ll and row.m15_vol_z>=0.5 and row.m15_rsi>25)
        # Range reversal around VWAP/range extremes.
        range_long=(regime=='RANGE' and row.m15_rsi<32 and row.m15_close<row.m15_ll*1.01)
        range_short=(regime=='RANGE' and row.m15_rsi>68 and row.m15_close>row.m15_hh*0.99)
        if not (long_setup or short_setup or range_long or range_short): return ('NONE',0,None)
        side='LONG' if (long_setup or range_long) else 'SHORT'
        momentum=70 if (long_setup or short_setup) else 60
        vol=70 if row.m15_vol_z>=0.5 else 50
        confidence=0.35*trend+0.15*momentum+0.10*vol+0.15*pos+0.15*liq+0.10*sent
        if side=='SHORT':
            # invert directional scores for short context where appropriate
            confidence=0.35*trend+0.15*momentum+0.10*vol+0.15*pos+0.15*liq+0.10*(100-sent)
        if confidence<self.p.min_confidence: return ('NONE',confidence,None)
        return side,confidence,regime
