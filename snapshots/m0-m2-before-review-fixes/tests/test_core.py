import unittest
import tempfile
import subprocess
import sys
sys.dont_write_bytecode = True  # Never alter cached files in the preserved baseline.
import uuid
from contextlib import nullcontext
from pathlib import Path
import pandas as pd
import numpy as np
from strategy import Strategy, Params
from trading.data import validate, BAR
from trading.ledger import Ledger, Halts
from backtest import run


def candles(n=60):
    return pd.DataFrame(dict(timestamp=pd.date_range('2025-01-01',periods=n,freq='5min',tz='UTC'),
                             open=100.,high=101.,low=99.,close=100.,volume=10.))

class Stub:
    def __init__(self,side='LONG'): self.side=side
    def prepare(self,df):
        x=df.copy(); x.index=x.index+BAR; x['m15_atr']=1.; return x
    def signal(self,row): return (self.side,80,'TEST')

class Tests(unittest.TestCase):
    def test_partial_accounting(self):
        for side,first,last,sign in [('LONG',110,95,1),('SHORT',90,105,-1)]:
            with self.subTest(side=side):
                l=Ledger(1000,.001); l.open(side,100,2,90,None)
                self.assertAlmostEqual(l.balance,999.8)
                l.close(1,first,None,'TP1'); l.fund(.01,100,None)
                self.assertEqual(l.position.qty,1)
                self.assertAlmostEqual(l.equity(last),1000+5-.2-first*.001-sign)
                result=l.close(1,last,None,'END')
                expected=5-(200+first+last)*.001-sign
                self.assertAlmostEqual(result['pnl'],expected)
                self.assertAlmostEqual(l.balance,1000+expected)
                self.assertIsNone(l.position)

    def test_slippage_once(self):
        for side in ('LONG','SHORT'):
            l=Ledger(1000,0,.01); l.open(side,100,1,90,None)
            result=l.close(1,100,None,'END')
            self.assertAlmostEqual(result['pnl'],-2)

    def test_halts(self):
        p=Params(); h=Halts(p); t=pd.Timestamp('2025-01-05',tz='UTC')
        h.update(t,2000); self.assertTrue(h.update(t,1960))
        self.assertFalse(h.update(t+pd.Timedelta(days=1),1960))
        h.update(t+pd.Timedelta(days=1),1890)
        self.assertTrue(h.update(t+pd.Timedelta(days=8),1890))
        self.assertTrue(h.hard)

    def test_weekly_reset_independent(self):
        h=Halts(Params(max_drawdown=.5)); t=pd.Timestamp('2024-12-29',tz='UTC')
        h.update(t,2000); h.update(t,1930)
        self.assertTrue(h.weekly)
        self.assertFalse(h.update(t+pd.Timedelta(days=1),1930))

    def test_data_rejections_and_epoch(self):
        x=candles(); x.timestamp=x.timestamp.astype('int64')//1000000 if x.timestamp.dtype.unit=='ns' else x.timestamp.astype('int64')//1000
        self.assertEqual(validate(x).index[0].year,2025)
        for bad in (candles().drop(3),pd.concat([candles(),candles().iloc[:1]])):
            with self.assertRaises(ValueError): validate(bad)
        bad=candles(); bad.loc[0,'high']=90
        with self.assertRaises(ValueError): validate(bad)

    def test_htf_and_prefix(self):
        s=Strategy(); raw=candles(100); raw['close']=np.linspace(100,100.5,100)
        full=s.prepare(raw); prefix=s.prepare(raw.iloc[:53])
        pd.testing.assert_frame_equal(full.iloc[:53],prefix)
        self.assertTrue(pd.isna(full.iloc[1].m15_close))
        self.assertEqual(full.iloc[2].m15_close,raw.iloc[2].close)
        self.assertTrue(pd.isna(full.iloc[46].h4_close))
        self.assertEqual(full.iloc[47].h4_close,raw.iloc[47].close)

    def test_oi_regression(self):
        s=Strategy(); x=candles(); x['open_interest']=np.arange(100,160)
        row=s.prepare(x).iloc[-1].copy()
        self.assertAlmostEqual(row.oi_chg,159/158-1)
        for name,value in dict(h4_close=110,h4_ema50=100,h4_ema200=90,h4_adx=25,h4_atr=1,
                               h1_close=110,h1_ema50=100,h1_ema200=90,h1_adx=25,h1_ema20=105,
                               m15_close=110,m15_vwap_proxy=100,m15_rsi=60,m15_mom=.01,
                               m15_vol_z=1,m15_hh=109,m15_ll=90).items(): row[name]=value
        self.assertEqual(s.signal(row)[0],'LONG')

    def test_next_open_and_stop_first(self):
        for side in ('LONG','SHORT'):
            x=candles(3); x.loc[1,['high','low']]=[110,90]
            summary,eq,tr=run(x,Params(fee_bps=0,slippage_bps=0),Stub(side))
            self.assertEqual(tr.iloc[0].entry_time,x.iloc[1].timestamp)
            self.assertEqual(tr.iloc[0].reason,'STOP')
            self.assertAlmostEqual(tr.iloc[0].pnl,-5)
            self.assertAlmostEqual(summary['end_equity'],2000+tr.pnl.sum())
            self.assertAlmostEqual(eq.iloc[-1].equity,summary['end_equity'])

    def test_gap_stop(self):
        x=candles(3); x.loc[2,['open','high','low','close']]=[90,91,89,90]
        _,_,tr=run(x,Params(fee_bps=0,slippage_bps=0),Stub())
        self.assertEqual(tr.iloc[0].exit_price,90)

    def test_funding_and_time_exit(self):
        x=candles(4); x['funding_event_rate']=[np.nan,.1,.01,np.nan]
        summary,eq,tr=run(x,Params(max_bars=2),Stub())
        self.assertEqual(tr.iloc[0].reason,'TIME')
        self.assertEqual(len(eq.attrs['funding']),1)
        self.assertLess(tr.iloc[0].funding,0)
        self.assertAlmostEqual(summary['end_equity'],2000+tr.pnl.sum())

    def test_cli(self):
        # Normal inherited Windows permissions; retain CLI artifacts for inspection.
        with nullcontext(Path('runs') / ('test-'+uuid.uuid4().hex)) as temp:
            temp.mkdir(parents=True)
            path=Path(temp); candles().to_csv(path/'input.csv',index=False)
            cmd=[sys.executable,'backtest.py','--csv',str(path/'input.csv'),'--out',str(path/'out')]
            result=subprocess.run(cmd,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((path/'out'/'fills.csv').exists())
            self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode,0)

    def test_simulated_partial_and_end(self):
        x=candles(4)
        x.loc[2,['open','high','low','close']]=[101,103,100.5,102]
        x.loc[3,['open','high','low','close']]=[102,103,101,102]
        summary,eq,tr=run(x,Params(fee_bps=4,slippage_bps=2),Stub())
        self.assertEqual([f['kind'] for f in eq.attrs['fills']],['ENTRY','TP1','END'])
        self.assertAlmostEqual(tr.iloc[0].fees,sum(f['fee'] for f in eq.attrs['fills']))
        self.assertAlmostEqual(tr.iloc[0].gross,sum(f['gross'] for f in eq.attrs['fills']))
        self.assertAlmostEqual(summary['end_equity'],2000+tr.pnl.sum())

    def test_original_oi_failure(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('original_strategy','snapshots/original/strategy.py')
        original=importlib.util.module_from_spec(spec); sys.modules[spec.name]=original; spec.loader.exec_module(original)
        raw=candles(); raw['open_interest']=100.
        row=original.Strategy().prepare(raw).iloc[-1].copy()
        for name,value in dict(h4_close=110,h4_ema50=100,h4_ema200=90,h4_adx=25,h4_atr=1,
                               h1_close=110,h1_ema50=100,h1_ema200=90,h1_adx=25,h1_ema20=105).items(): row[name]=value
        with self.assertRaises(AttributeError): original.Strategy().signal(row)

    def test_parameters(self):
        for p in (Params(initial_capital=0),Params(risk_per_trade=2),Params(fee_bps=-1)):
            with self.assertRaises(ValueError): p.validate()

    def test_original_partial_reporting_discrepancy(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('original_backtest','snapshots/original/backtest.py')
        original=importlib.util.module_from_spec(spec); spec.loader.exec_module(original)
        class Once(Stub):
            used=False
            def signal(self,row):
                if self.used: return ('NONE',0,None)
                self.used=True; return ('LONG',80,'TEST')
        original.Strategy=lambda p: Once()
        x=validate(candles(3))
        x.iloc[1,x.columns.get_indexer(['open','high','low','close'])]=[101,103,100.5,102]
        p=Params(fee_bps=0,slippage_bps=0)
        summary,_,tr=original.run(x,p)
        self.assertAlmostEqual(summary['end_equity']-2000,3.888888888889)
        self.assertAlmostEqual(tr.pnl.sum(),.138888888889)

if __name__=='__main__': unittest.main()
