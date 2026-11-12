"""Chronological single-instrument linear-perpetual research simulator."""
import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import numpy as np
import pandas as pd
from strategy import Strategy, Params
from trading.data import validate, BAR
from trading.ledger import Ledger, Halts


def load(path):
    return validate(pd.read_csv(path))


def run(df, p, strategy=None, start=None):
    p.validate()
    data=validate(df); strategy=strategy or Strategy(p)
    features=strategy.prepare(data)
    ledger=Ledger(p.initial_capital,p.fee_bps/10000,p.slippage_bps/10000)
    halts=Halts(p); trades=[]; curve=[]; signals=[]; pending=None; meta={}
    start=pd.to_datetime(start,utc=True) if start is not None else data.index[0]
    halted=False
    def exit_qty(q, price, ts, reason):
        result=ledger.close(q,price,ts,reason)
        if result is not None:
            trades.append({**meta,**result,'exit_time':ts,'reason':reason,
                           'exit_price':ledger.fills[-1]['price']})
    for ts,r in data.iterrows():
        if ts < start:
            # Preserve pre-start indicators and the last completed setup.
            row=features.loc[ts+BAR]
            side,conf,reg=strategy.signal(row)
            atrv=row.get('m15_atr',np.nan)
            pending=(side,conf,reg,atrv) if side!='NONE' and np.isfinite(atrv) and atrv>0 else None
            continue
        if not curve:
            curve.append((ts,p.initial_capital))
        halted=halts.update(ts,ledger.equity(r.open))
        # Settlement applies only to positions held before this opening boundary.
        rate=r.get('funding_event_rate',np.nan)
        if pd.notna(rate):
            ledger.fund(rate,r.open,ts)
        halted=halts.update(ts,ledger.equity(r.open))
        if pending and not ledger.position and not halted:
            side,conf,reg,atrv=pending
            direction=1 if side=='LONG' else -1
            entry=r.open*(1+direction*ledger.slip)
            distance=atrv*p.atr_stop_mult; stop=entry-direction*distance
            stop_fill=stop*(1-direction*ledger.slip)
            unit_risk=direction*(entry-stop_fill)+(entry+stop_fill)*ledger.fee
            if stop>0 and unit_risk>0:
                qty=min(ledger.balance*p.risk_per_trade/unit_risk,ledger.balance*p.max_leverage/entry)
                if qty>0:
                    meta=dict(entry_time=ts,entry_price=entry,side=side,confidence=conf,regime=reg,risk_cash=qty*unit_risk)
                    ledger.open(side,r.open,qty,stop,ts)
                    tp1=entry+direction*1.5*distance; tp2=entry+direction*2.5*distance
                    tp1_hit=False; bars=0
        pending=None
        if ledger.position:
            pos=ledger.position; bars+=1; long=pos.side=='LONG'
            stop_hit=r.low<=pos.stop if long else r.high>=pos.stop
            if stop_hit:
                price=min(r.open,pos.stop) if long else max(r.open,pos.stop)
                exit_qty(pos.qty,price,ts+BAR,'STOP')
            else:
                if not tp1_hit and (r.high>=tp1 if long else r.low<=tp1):
                    exit_qty(pos.initial_qty/2,tp1,ts+BAR,'TP1'); tp1_hit=True
                    pos.stop=pos.entry*(1.001 if long else .999)
                    # Conservative unknown path: raised stop can hit after TP1 in same bar.
                    if (r.low<=pos.stop if long else r.high>=pos.stop):
                        exit_qty(pos.qty,pos.stop,ts+BAR,'STOP_AFTER_TP1')
                if ledger.position and (r.high>=tp2 if long else r.low<=tp2):
                    exit_qty(pos.qty,tp2,ts+BAR,'TP2')
                if ledger.position and bars>=p.max_bars:
                    exit_qty(pos.qty,r.close,ts+BAR,'TIME')
        equity=ledger.equity(r.close)
        # Charge this closing mark to its bar's UTC day/week before resetting.
        halted=halts.update(ts+BAR-pd.Timedelta(nanoseconds=1),equity)
        decision=ts+BAR
        side,conf,reg=strategy.signal(features.loc[decision])
        signals.append(dict(timestamp=decision,side=side,confidence=conf,regime=reg,halted=bool(halted)))
        atrv=features.loc[decision].get('m15_atr',np.nan)
        if side!='NONE' and np.isfinite(atrv) and atrv>0 and not ledger.position:
            pending=(side,conf,reg,atrv)
        curve.append((decision,equity))
    if ledger.position:
        exit_qty(ledger.position.qty,data.iloc[-1].close,data.index[-1]+BAR,'END')
        curve[-1]=(curve[-1][0],ledger.balance)
        halted=halts.update(data.index[-1]+BAR-pd.Timedelta(nanoseconds=1),ledger.balance)
    ed=pd.DataFrame(curve,columns=['timestamp','equity']); tr=pd.DataFrame(trades)
    eq=ledger.balance
    dd=float((ed.equity/ed.equity.cummax()-1).min()) if len(ed) else 0
    rets=ed.equity.pct_change().dropna() if len(ed) else pd.Series(dtype=float)
    years=(ed.timestamp.iloc[-1]-ed.timestamp.iloc[0]).total_seconds()/31557600 if len(ed)>1 else 0
    growth=np.log(eq/p.initial_capital)/years if years and eq>0 else np.nan
    cagr=float(np.expm1(growth)*100) if np.isfinite(growth) and growth<700 else None
    wins=tr.pnl[tr.pnl>0].sum() if len(tr) else 0
    losses=-tr.pnl[tr.pnl<0].sum() if len(tr) else 0
    summary=dict(start_equity=p.initial_capital,end_equity=eq,total_return_pct=(eq/p.initial_capital-1)*100,
                 max_drawdown_pct=dd*100,cagr_pct=cagr,trades=len(tr),
                 win_rate_pct=float((tr.pnl>0).mean()*100) if len(tr) else 0,
                 profit_factor=float(wins/losses) if losses else None,
                 sharpe_5m_annualized=float(np.sqrt(365*288)*rets.mean()/rets.std()) if len(rets)>1 and rets.std()>0 else 0,
                 halted=bool(halted),funding_mode='explicit_events' if 'funding_event_rate' in data else 'not_tested',
                 parameters=asdict(p),execution='next_open; stop_first; adverse_slippage; zero_latency')
    ed.attrs['fills']=ledger.fills; ed.attrs['funding']=ledger.funding_events; ed.attrs['signals']=signals
    return summary,ed,tr


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--csv',required=True)
    ap.add_argument('--start'); ap.add_argument('--end'); ap.add_argument('--capital',type=float,default=2000)
    ap.add_argument('--out',default='runs/test'); ap.add_argument('--fee-bps',type=float,default=4)
    ap.add_argument('--slippage-bps',type=float,default=2); a=ap.parse_args()
    df=load(a.csv)
    if a.end:
        df=df.loc[df.index+BAR<=pd.to_datetime(a.end,utc=True)]
    p=Params(initial_capital=a.capital,fee_bps=a.fee_bps,slippage_bps=a.slippage_bps)
    summary,eq,tr=run(df,p,start=a.start)
    summary['input_sha256']=hashlib.sha256(Path(a.csv).read_bytes()).hexdigest()
    out=Path(a.out); out.mkdir(parents=True,exist_ok=False)
    (out/'summary.json').write_text(json.dumps(summary,indent=2,allow_nan=False))
    eq.to_csv(out/'equity.csv',index=False); tr.to_csv(out/'trades.csv',index=False)
    for name in ('fills','funding','signals'):
        pd.DataFrame(eq.attrs[name]).to_csv(out/f'{name}.csv',index=False)
    print(json.dumps(summary,indent=2,allow_nan=False))


if __name__=='__main__': main()
