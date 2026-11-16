/* Read-only local monitoring. No trading request exists in this client. */
const $ = id => document.getElementById(id);
const app = { mode: 'paper', page: 'dashboard', state: null, tradePage: 1, tradeTotal: 0, busy: false };
const names = {dashboard:'Dashboard',trading:'Trading',positions:'Open Positions',history:'Trade History',signals:'Strategy Signals',analytics:'Analytics',risk:'Risk Management',health:'System Health',settings:'Settings'};

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (className) el.className = className;
  return el;
}
function put(id, child) { $(id).replaceChildren(child); }
function empty(message) { return node('p', message, 'empty'); }
function amount(value, currency = '') {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const digits = Math.abs(n) < 1 && n !== 0 ? 4 : 2;
  return `${currency ? currency + ' ' : ''}${new Intl.NumberFormat('en-IN', {maximumFractionDigits:digits,minimumFractionDigits:Math.min(digits,2)}).format(n)}`;
}
function stamp(value) { return value ? new Date(value).toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}) : '—'; }
function changeClass(value) { return value == null ? '' : Number(value) > 0 ? 'positive' : Number(value) < 0 ? 'negative' : ''; }
function statusTag(value) {
  const upper = String(value ?? 'UNKNOWN').toUpperCase();
  const style = ['QUALIFIED','APPROVED','REDUCED','CONNECTED','OPEN','OK','LIVE'].includes(upper) ? 'good' :
    ['REJECTED','BLOCKED','ERROR','INVALIDATED','QUARANTINED'].includes(upper) ? 'bad' : 'warn';
  return node('span', upper, `tag ${style}`);
}
function cards(id, items) {
  const wrap = document.createDocumentFragment();
  for (const item of items) {
    const card = node('article', null, 'metric');
    card.append(node('span', item.label, 'label'));
    const value = node('strong', item.value ?? '—', `value ${item.tone || ''}`);
    if (item.exact != null) value.title = String(item.exact);
    card.append(value, node('span', item.note || '', 'note'));
    wrap.append(card);
  }
  put(id, wrap);
}
function table(id, rows, columns, message) {
  if (!rows || !rows.length) { put(id, empty(message)); return; }
  const outer = node('div', null, 'table-wrap');
  const grid = node('table');
  const thead = node('thead'), header = node('tr');
  for (const col of columns) header.append(node('th', col.title, col.numeric ? 'numeric' : ''));
  thead.append(header); grid.append(thead);
  const body = node('tbody');
  for (const row of rows) {
    const tr = node('tr');
    for (const col of columns) {
      const td = node('td', null, col.numeric ? 'numeric' : '');
      const value = col.render ? col.render(row) : row[col.key];
      td.append(value instanceof Node ? value : document.createTextNode(value ?? '—'));
      tr.append(td);
    }
    body.append(tr);
  }
  grid.append(body); outer.append(grid); put(id, outer);
}
function message(text, tone = '') { $('notice').textContent = text || ''; $('notice').className = `notice ${tone}`; }
async function api(path, options) {
  const response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}
function showPage(page) {
  app.page = page;
  for (const section of document.querySelectorAll('.page')) section.classList.toggle('active', section.id === page);
  for (const button of document.querySelectorAll('#nav button')) button.classList.toggle('active', button.dataset.page === page);
  $('page-title').textContent = names[page];
  $('page-subtitle').textContent = page === 'settings' ? 'Monitoring settings and execution boundaries' :
    app.mode === 'real' ? 'Real account monitoring · execution disabled' : 'Paper and strategy monitoring';
  if (page === 'history') loadTrades();
  if (page === 'analytics' && app.state) renderCharts(app.state);
}
function renderStatus(s) {
  const journal = s.health.journal.status;
  const feed = s.health.market_data.every(v => v === 'LIVE') ? 'LIVE' :
    s.health.market_data.some(v => v === 'ERROR') ? 'ERROR' : 'STALE';
  const pill = $('system-status'); pill.textContent = journal === 'OK' ? `JOURNAL OK · FEED ${feed}` : `JOURNAL ${journal}`;
  pill.className = `status-pill ${journal !== 'OK' ? 'bad' : feed === 'LIVE' ? 'ok' : 'warn'}`;
  $('mode-status').textContent = `Mode ${s.mode_label}`;
  $('connection-status').textContent = `Account ${s.account_source}`;
  $('last-update').textContent = `Last data update ${stamp(s.health.last_processing_at)}`;
  $('account-source').textContent = s.account_message;
  $('settings-source').textContent = `Current account connection: ${s.account_message}. Journal: ${journal}.`;
  if (s.mode === 'real') message('REAL ACCOUNT — READ ONLY. Live execution is disabled. Paper and real balances are never combined.', 'warn');
  else if (s.account_source !== 'CONNECTED') message(s.account_message + '. No simulated balance or trade has been invented.', 'warn');
  else message('');
}
function renderAccount(s) {
  const a = s.account || {}, currency = a.currency || '';
  const real = s.mode === 'real';
  const equity = real ? a.margin_balance : a.equity;
  cards('account-cards', [
    {label:real?'USDT margin balance':'Total portfolio equity',value:amount(equity,currency),exact:equity,note:real?'Exchange USDT asset only':'M8 current equity'},
    {label:'Available balance',value:amount(a.available_balance,currency),exact:a.available_balance,note:real?'Exchange account':'After M8 reservations'},
    {label:real?'Wallet balance':'Used capital',value:amount(real?a.wallet_balance:a.used_capital,currency),note:real?'USDT wallet':'Margin + reserved capital'},
    {label:'Realized P&L',value:amount(a.realized_pnl,currency),tone:changeClass(a.realized_pnl),note:real?'Unavailable from account snapshot':'M8 realized'},
    {label:'Unrealized P&L',value:amount(a.unrealized_pnl,currency),tone:changeClass(a.unrealized_pnl),note:real?'Exchange reported':'M8 mark to market'},
    {label:"Today's P&L",value:amount(a.today_pnl,currency),tone:changeClass(a.today_pnl),note:real?'Unavailable from account snapshot':'Current equity vs day start'},
    {label:'Cumulative P&L',value:amount(a.cumulative_pnl,currency),tone:changeClass(a.cumulative_pnl),note:real?'Unavailable from account snapshot':'Current equity vs M8 initial cash'},
    {label:'Total fees',value:amount(a.fees,currency),note:real?'Unavailable from account snapshot':'M8 booked fees'},
    {label:'Funding cash flow',value:amount(a.funding,currency),tone:changeClass(a.funding),note:real?'Unavailable from account snapshot':'M8 booked funding'},
  ]);
}
function renderMarkets(s) {
  const fragment = document.createDocumentFragment();
  for (const market of s.markets) {
    const card = node('article', null, 'market-card');
    card.append(node('div', market.symbol, 'symbol'), node('div', amount(market.price,'USDT'), 'price'));
    const info = node('div', null, 'meta');
    const pct = market.change_24h_pct == null ? '24h —' : `24h ${market.change_24h_pct >= 0 ? '+' : ''}${market.change_24h_pct.toFixed(2)}%`;
    info.append(node('span', pct, `change ${changeClass(market.change_24h_pct)}`), statusTag(market.feed_status));
    card.append(info, node('div', `Finalized ${stamp(market.last_finalized_candle)}`, 'subline'));
    const latest = [...s.signals].reverse().find(x => x.symbol.replace('/','') === market.symbol && x.m6_regime);
    card.append(node('div', latest ? `Last evaluated regime: ${latest.m6_regime} · ${stamp(latest.m6_as_of)}` : 'Regime not available', 'subline'));
    fragment.append(card);
  }
  put('market-cards', fragment);
}
const positionColumns = [
  {title:'Asset',key:'symbol'}, {title:'Side',render:r=>statusTag(r.side)},
  {title:'Entry',render:r=>amount(r.entry_price),numeric:true},
  {title:'Current',render:r=>amount(r.current_price),numeric:true},
  {title:'Quantity',render:r=>amount(r.quantity),numeric:true},
  {title:'Stop loss',render:r=>amount(r.stop_loss),numeric:true},
  {title:'Take profit',render:r=>amount(r.take_profit),numeric:true},
  {title:'Unrealized P&L',render:r=>node('span',amount(r.unrealized_pnl),changeClass(r.unrealized_pnl)),numeric:true},
  {title:'Status',render:r=>statusTag(r.status)},
];
const signalColumns = [
  {title:'Observed',render:r=>stamp(r.available_at)},
  {title:'Asset',render:r=>r.symbol.replace('/','')}, {title:'Side',render:r=>r.side||'—'},
  {title:'Pattern',render:r=>{const box=node('span',r.pattern||'—');box.append(node('span',`${r.pattern_family||''} · ${r.timeframe||''}`,'subline'));return box;}},
  {title:'M5 / M6',render:r=>{const box=node('span',`${r.m5_trend||'—'} / ${r.m6_regime||'—'}`);box.append(node('span',r.m5_location_eligible?'Location eligible':'Location unavailable','subline'));return box;}},
  {title:'M7',render:r=>statusTag(r.lifecycle)},
  {title:'M8',render:r=>r.risk_status?statusTag(r.risk_status):'—'},
  {title:'Blockers / evidence',render:r=>{const box=node('span',(r.blocking_reasons||[]).join(', ')||'No blockers');box.append(node('span',(r.reason_codes||[]).join(', '),'subline'));box.title=(r.reason_codes||[]).join(', ');return box;}},
];
function renderTables(s) {
  table('overview-positions', s.positions.slice(0,4), positionColumns, s.mode==='paper'?'No open simulated positions':'No connected exchange positions');
  table('positions-table', s.positions, positionColumns, s.mode==='paper'?'No open simulated positions':'Exchange account not connected or no open positions');
  table('overview-signals', s.signals.slice(-4).reverse(), signalColumns, 'No M7 decisions recorded in the selected journal');
  table('signals-table', [...s.signals].reverse(), signalColumns, 'No M7 decisions recorded in the selected journal');
  table('orders-table', s.orders, [
    {title:'Order ID',render:r=>node('span',r.id.slice(0,16)+'…','mono')},
    {title:'Asset',key:'symbol'}, {title:'Side',key:'side'},
    {title:'Quantity',render:r=>amount(r.quantity),numeric:true},
    {title:'Created',render:r=>stamp(r.created_at)},
    {title:'State',render:r=>statusTag(r.state)}], 'No simulated orders in the connected paper runtime');
}
function renderRisk(s) {
  const r=s.risk||{},currency=s.account?.currency||'';
  cards('risk-cards',[
    {label:'Per-trade planned-loss cap',value:r.risk_fraction?`${(Number(r.risk_fraction)*100).toFixed(0)}%`:'—',note:'M8 current-equity policy'},
    {label:'Current per-trade budget',value:amount(r.risk_budget_now,currency)},
    {label:'Aggregate stop risk',value:amount(r.stop_risk,currency)},
    {label:'Remaining risk capacity',value:amount(r.remaining_aggregate_stop_risk,currency),note:'Aggregate stop-risk headroom'},
    {label:'Gross exposure',value:amount(r.gross_exposure,currency)},
    {label:'Risk rejections / blocks',value:r.rejection_count??'—'},
  ]);
  table('risk-table',[...(r.decisions||[])].reverse(),[
    {title:'Time',render:d=>stamp(d.available_at)}, {title:'Asset',key:'symbol'},
    {title:'Status',render:d=>statusTag(d.status)},
    {title:'Quantity',render:d=>amount(d.quantity),numeric:true},
    {title:'Planned loss',render:d=>amount(d.planned_loss),numeric:true},
    {title:'Budget',render:d=>amount(d.risk_budget),numeric:true},
    {title:'Reasons',render:d=>(d.reasons||[]).join(', ')||'—'},
  ],'No M8 risk evaluations in the connected paper runtime');
}
function renderHealth(s) {
  const h=s.health;
  cards('health-cards',[
    {label:'Market data',value:h.market_data.every(x=>x==='LIVE')?'LIVE':h.market_data.join(' / '),note:'BTC / ETH finalized candles'},
    {label:'BTC feed',value:h.market_data[0]}, {label:'ETH feed',value:h.market_data[1]},
    {label:'Runtime heartbeat',value:'UNKNOWN',note:'No heartbeat contract is published'},
    {label:'Journal',value:h.journal.status,note:h.journal.message},
    {label:'Paper execution',value:h.paper_execution},
  ]);
  const list=node('dl',null,'kv');
  for(const [label,value] of [['Journal head',h.journal.head],['Committed outcomes',h.journal.applied],['SQLite quick check',h.journal.integrity],['Last processing',stamp(h.last_processing_at)],['Account connection',h.account_connection],['Latest error',h.journal.status==='ERROR'?h.journal.message:'None reported']]){
    const pair=node('div');pair.append(node('dt',label),node('dd',value??'—'));list.append(pair);
  }
  put('health-detail',list);
}
function renderAnalytics(s) {
  const a=s.analytics||{},currency=s.account?.currency||'';
  cards('analytics-cards',[
    {label:'Completed trades',value:a.total_trades??'—'},
    {label:'Win rate',value:a.win_rate_pct==null?'—':`${a.win_rate_pct.toFixed(1)}%`},
    {label:'Profit factor',value:amount(a.profit_factor)},
    {label:'Max drawdown',value:a.max_drawdown_pct==null?'—':`${amount(a.max_drawdown_pct)}%`},
    {label:'Average winning trade',value:amount(a.average_win,currency)},
    {label:'Average losing trade',value:amount(a.average_loss,currency)},
    {label:'Average realized R',value:amount(a.average_realized_r),note:'Net P&L / original planned loss; not expected return'},
  ]);
  const grouped=(target,items)=>{
    if(!items){put(target,empty('No completed trade attribution available'));return;}
    const dl=node('dl',null,'kv');
    for(const [name,item] of Object.entries(items)){const row=node('div');row.append(node('dt',`${name} · ${item.trades} trades`),node('dd',amount(item.net_pnl,currency)));dl.append(row);}put(target,dl);
  };
  grouped('side-analytics',a.by_side);grouped('asset-analytics',a.by_asset);
}
function draw(id, emptyId, values, kind='line') {
  const canvas=$(id), messageEl=$(emptyId);
  if(!values?.length){canvas.hidden=true;messageEl.textContent='Insufficient account history for this chart.';return;}
  canvas.hidden=false;messageEl.textContent='';
  const width=canvas.clientWidth||500,height=250,dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
  const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,width,height);
  const nums=values.map(Number).filter(Number.isFinite);if(!nums.length)return;
  let low=Math.min(...nums),high=Math.max(...nums);if(low===high){low-=1;high+=1;}
  const x=i=>30+i*(width-45)/Math.max(1,nums.length-1),y=v=>height-25-(v-low)/(high-low)*(height-48);
  ctx.strokeStyle='#334658';ctx.lineWidth=1;for(let i=0;i<4;i++){const yy=18+i*(height-43)/3;ctx.beginPath();ctx.moveTo(30,yy);ctx.lineTo(width-12,yy);ctx.stroke();}
  if(kind==='bars') {const base=y(Math.max(low,Math.min(0,high)));const bar=Math.max(2,(width-50)/nums.length*.65);nums.forEach((v,i)=>{ctx.fillStyle=v>=0?'#64c9b5':'#f38b87';ctx.fillRect(x(i)-bar/2,Math.min(y(v),base),bar,Math.max(2,Math.abs(base-y(v))));});}
  else {ctx.strokeStyle='#64c9b5';ctx.lineWidth=2;ctx.beginPath();nums.forEach((v,i)=>i?ctx.lineTo(x(i),y(v)):ctx.moveTo(x(i),y(v)));ctx.stroke();}
}
function renderCharts(s){draw('overview-chart','overview-empty',s.equity_curve.map(p=>p.equity));draw('equity-chart','equity-empty',s.equity_curve.map(p=>p.equity));draw('daily-chart','daily-empty',s.daily_pnl.map(p=>p.pnl),'bars');}
function render(s){app.state=s;renderStatus(s);renderAccount(s);renderMarkets(s);renderTables(s);renderRisk(s);renderHealth(s);renderAnalytics(s);renderCharts(s);}
async function refresh(){if(app.busy)return;app.busy=true;try{const s=await api(`/api/state?mode=${app.mode}`);render(s);if(app.page==='history')await loadTrades();}catch(e){message(`Dashboard data unavailable: ${e.message}`,'bad');$('system-status').textContent='DATA ERROR';$('system-status').className='status-pill bad';}finally{app.busy=false;}}
async function loadTrades(){
  const q=new URLSearchParams({mode:app.mode,page:String(app.tradePage),page_size:'20',search:$('trade-search').value,asset:$('trade-asset').value,side:$('trade-side').value,result:$('trade-result').value,start:$('trade-from').value,end:$('trade-to').value});
  try{const data=await api('/api/trades?'+q);app.tradeTotal=data.total;$('trade-count').textContent=`${data.total} matching trades`;$('page-label').textContent=`Page ${data.page} of ${Math.max(1,Math.ceil(data.total/data.page_size))}`;$('page-prev').disabled=data.page<=1;$('page-next').disabled=data.page*data.page_size>=data.total;
    table('history-table',data.items,[
      {title:'Trade ID',render:t=>{const b=node('button',t.id.slice(0,15)+'…','row-button mono');b.addEventListener('click',()=>showTrade(t.id));return b;}},
      {title:'Asset',key:'symbol'}, {title:'Side',render:t=>statusTag(t.side)},
      {title:'Entry',render:t=>`${stamp(t.entry_at)} · ${amount(t.entry_price)}`},
      {title:'Exit',render:t=>`${stamp(t.exit_at)} · ${amount(t.exit_price)}`},
      {title:'Qty',render:t=>amount(t.quantity),numeric:true},
      {title:'Stop / TP',render:t=>`${amount(t.stop_loss)} / ${amount(t.take_profit)}`},
      {title:'Gross',render:t=>amount(t.gross_pnl),numeric:true},
      {title:'Fees / Funding',render:t=>`${amount(t.fees)} / ${amount(t.funding)}`,numeric:true},
      {title:'Net P&L',render:t=>node('span',amount(t.net_pnl),changeClass(t.net_pnl)),numeric:true},
      {title:'Exit reason',key:'exit_reason'}, {title:'Status',render:t=>statusTag(t.status)}],
      app.mode==='real'?'Exchange trade history is not available from the read-only account snapshot.':'No completed simulated trades match these filters.');
  }catch(e){put('history-table',empty(`Trade history unavailable: ${e.message}`));}
}
async function showTrade(id){try{const t=await api(`/api/trades/${encodeURIComponent(id)}?mode=${app.mode}`);const dl=node('dl',null,'kv');for(const [key,value] of Object.entries(t)){const row=node('div');row.append(node('dt',key.replaceAll('_',' ')),node('dd',value??'—'));dl.append(row);}put('trade-detail-body',dl);$('trade-detail').showModal();}catch(e){message(e.message,'bad');}}
async function initialize(){const session=await api('/api/session');$('login').hidden=session.authenticated;$('app').hidden=!session.authenticated;if(session.authenticated)refresh();}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();$('login-error').textContent='';try{await api('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('password').value})});$('password').value='';$('login').hidden=true;$('app').hidden=false;refresh();}catch(err){$('login-error').textContent=err.message;}});
$('logout').addEventListener('click',async()=>{await api('/api/logout',{method:'POST'});$('app').hidden=true;$('login').hidden=false;app.state=null;});
$('mode').addEventListener('change',()=>{app.mode=$('mode').value;app.tradePage=1;refresh();});
for(const b of document.querySelectorAll('#nav button'))b.addEventListener('click',()=>showPage(b.dataset.page));
for(const b of document.querySelectorAll('.jump'))b.addEventListener('click',()=>showPage(b.dataset.jump));
for(const id of ['trade-search','trade-from','trade-to','trade-asset','trade-side','trade-result'])$(id).addEventListener('input',()=>{app.tradePage=1;loadTrades();});
$('page-prev').addEventListener('click',()=>{if(app.tradePage>1){app.tradePage--;loadTrades();}});
$('page-next').addEventListener('click',()=>{app.tradePage++;loadTrades();});
$('detail-close').addEventListener('click',()=>$('trade-detail').close());
window.addEventListener('resize',()=>{if(app.state)renderCharts(app.state);});
initialize().catch(e=>{ $('login').hidden=false;$('login-error').textContent=e.message;});
setInterval(()=>{if(!$('app').hidden)refresh();},15000);
