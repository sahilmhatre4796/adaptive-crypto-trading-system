import argparse,json,os
import numpy as np,pandas as pd
from strategy import Strategy,Params

def load(path):
    df=pd.read_csv(path)
    if 'timestamp' not in df:
        for c in ['time','datetime','date','open_time']:
            if c in df: df=df.rename(columns={c:'timestamp'}); break
    if 'timestamp' not in df: raise ValueError('CSV needs timestamp column')
    # Binance milliseconds support
    ts=pd.to_datetime(df.timestamp,utc=True,errors='coerce')
    if ts.isna().mean()>0.5:
        ts=pd.to_datetime(pd.to_numeric(df.timestamp,errors='coerce'),unit='ms',utc=True)
    df['timestamp']=ts
    return df.dropna(subset=['timestamp']).sort_values('timestamp')

def run(df,p):
    s=Strategy(p); x=s.prepare(df)
    equity=p.initial_capital; peak=equity; cash=equity; pos=None; trades=[]; eq=[]; daily_start=equity; weekly_start=equity; day=None; week=None; halted=False
    fee=p.fee_bps/10000; slip=p.slippage_bps/10000
    for i,(ts,r) in enumerate(x.iterrows()):
        if not np.isfinite(r.close): continue
        d=ts.date(); w=ts.isocalendar().week
        if day!=d: day=d; daily_start=equity
        if week!=w: week=w; weekly_start=equity
        if equity<=peak*(1-p.max_drawdown): halted=True
        if equity<=daily_start*(1-p.daily_loss_limit) or equity<=weekly_start*(1-p.weekly_loss_limit): halted=True
        # mark existing position
        if pos:
            if pos['side']=='LONG': equity=pos['cash'] + pos['qty']*(r.close-pos['entry'])
            else: equity=pos['cash'] + pos['qty']*(pos['entry']-r.close)
            # exits
            stop=pos['stop']; tp1=pos['tp1']; tp2=pos['tp2']; hit=None; px=None
            if pos['side']=='LONG':
                if r.low<=stop: hit='STOP'; px=stop
                elif not pos['tp1_hit'] and r.high>=tp1: hit='TP1'; px=tp1
                elif r.high>=tp2: hit='TP2'; px=tp2
            else:
                if r.high>=stop: hit='STOP'; px=stop
                elif not pos['tp1_hit'] and r.low<=tp1: hit='TP1'; px=tp1
                elif r.low<=tp2: hit='TP2'; px=tp2
            if hit:
                frac=0.5 if hit=='TP1' else 1.0
                q=pos['qty']*frac
                if pos['side']=='LONG': gross=q*(px-pos['entry'])
                else: gross=q*(pos['entry']-px)
                costs=(abs(q*px)+abs(q*pos['entry']))*fee + abs(q*px)*slip
                cash_after=pos['cash']+gross-costs
                if hit=='TP1':
                    pos['cash']=cash_after; pos['qty']-=q; pos['tp1_hit']=True; pos['stop']=pos['entry']*(1+0.001 if pos['side']=='LONG' else 1-0.001)
                    equity=cash_after
                else:
                    pnl=cash_after-pos['cash']
                    trades.append({**pos['meta'],'exit_time':ts.isoformat(),'exit_price':px,'reason':hit,'pnl':pnl,'pnl_pct':pnl/max(1,pos['cash'])})
                    equity=cash_after; pos=None; cash=equity
        if pos is None and not halted:
            side,conf,reg=s.signal(r)
            if side!='NONE':
                entry=float(r.open)*(1+slip if side=='LONG' else 1-slip)
                atrv=float(r.m15_atr) if np.isfinite(r.m15_atr) else float(r.atr)
                if not np.isfinite(atrv) or atrv<=0: continue
                stop=entry-atrv*p.atr_stop_mult if side=='LONG' else entry+atrv*p.atr_stop_mult
                risk_per_unit=abs(entry-stop); risk_cash=equity*p.risk_per_trade; qty=risk_cash/risk_per_unit
                notional=qty*entry; qty=min(qty,equity*p.max_leverage/entry)
                if qty<=0: continue
                rpu=abs(entry-stop); tp1=entry+(1.5*rpu if side=='LONG' else -1.5*rpu); tp2=entry+(2.5*rpu if side=='LONG' else -2.5*rpu)
                cash=equity; pos={'side':side,'entry':entry,'qty':qty,'cash':cash,'stop':stop,'tp1':tp1,'tp2':tp2,'tp1_hit':False,'meta':{'entry_time':ts.isoformat(),'entry_price':entry,'side':side,'confidence':conf,'regime':reg,'risk_cash':risk_cash}}
        peak=max(peak,equity); eq.append((ts,equity))
    if pos:
        r=x.iloc[-1]; px=float(r.close); q=pos['qty']; gross=q*(px-pos['entry']) if pos['side']=='LONG' else q*(pos['entry']-px); costs=(abs(q*px)+abs(q*pos['entry']))*fee+abs(q*px)*slip; final=pos['cash']+gross-costs; trades.append({**pos['meta'],'exit_time':x.index[-1].isoformat(),'exit_price':px,'reason':'END','pnl':final-pos['cash'],'pnl_pct':(final-pos['cash'])/max(1,pos['cash'])}); equity=final
    ed=pd.DataFrame(eq,columns=['timestamp','equity']).drop_duplicates('timestamp')
    tr=pd.DataFrame(trades)
    if len(ed):
        roll=ed.equity.cummax(); dd=ed.equity/roll-1; maxdd=float(dd.min())
        years=max((ed.timestamp.iloc[-1]-ed.timestamp.iloc[0]).total_seconds()/31557600,1e-9); cagr=(equity/p.initial_capital)**(1/years)-1
        rets=ed.equity.pct_change().dropna(); sharpe=float(np.sqrt(365*24*12)*rets.mean()/rets.std()) if rets.std()>0 else 0
    else: maxdd=cagr=sharpe=0
    wins=int((tr.pnl>0).sum()) if len(tr) else 0; losses=int((tr.pnl<=0).sum()) if len(tr) else 0
    gross_win=float(tr.loc[tr.pnl>0,'pnl'].sum()) if len(tr) else 0; gross_loss=float(-tr.loc[tr.pnl<0,'pnl'].sum()) if len(tr) else 0
    summary={'start_equity':p.initial_capital,'end_equity':float(equity),'total_return_pct':float((equity/p.initial_capital-1)*100),'cagr_pct':float(cagr*100),'max_drawdown_pct':float(maxdd*100),'sharpe_5m_annualized':float(sharpe),'trades':len(tr),'win_rate_pct':float(100*wins/len(tr)) if len(tr) else 0,'profit_factor':float(gross_win/gross_loss) if gross_loss else None,'fees_slippage_model':{'fee_bps':p.fee_bps,'slippage_bps':p.slippage_bps},'halted':halted}
    return summary,ed,tr

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--csv',required=True); ap.add_argument('--start'); ap.add_argument('--end'); ap.add_argument('--capital',type=float,default=2000); ap.add_argument('--out',default='runs/test'); ap.add_argument('--fee-bps',type=float,default=4); ap.add_argument('--slippage-bps',type=float,default=2); a=ap.parse_args()
    df=load(a.csv)
    if a.start: df=df[df.timestamp>=pd.Timestamp(a.start,tz='UTC')]
    if a.end: df=df[df.timestamp<pd.Timestamp(a.end,tz='UTC')]
    p=Params(); p.initial_capital=a.capital; p.fee_bps=a.fee_bps; p.slippage_bps=a.slippage_bps
    os.makedirs(a.out,exist_ok=True); summary,eq,tr=run(df,p)
    json.dump(summary,open(os.path.join(a.out,'summary.json'),'w'),indent=2); eq.to_csv(os.path.join(a.out,'equity.csv'),index=False); tr.to_csv(os.path.join(a.out,'trades.csv'),index=False)
    print(json.dumps(summary,indent=2))
if __name__=='__main__': main()
