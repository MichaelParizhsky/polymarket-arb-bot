"""
Enhanced FastAPI dashboard with tabs for bot + meta-agent.
Visit http://localhost:5000
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_portfolio = None
_bot_start_time = time.time()
_cycle_count = 0


def register(portfolio, start_time: float) -> None:
    global _portfolio, _bot_start_time
    _portfolio = portfolio
    _bot_start_time = start_time


# ------------------------------------------------------------------ #
#  Bot API endpoints                                                   #
# ------------------------------------------------------------------ #

@app.get("/api/status")
def status():
    if not _portfolio:
        return {"status": "starting"}
    p = _portfolio
    uptime = int(time.time() - _bot_start_time)
    total_pnl = round(p.total_pnl(), 2)
    realized = round(p.realized_closed_pnl(), 2)
    trades_per_hour = round(len(p.trades) / max(uptime / 3600, 0.01), 1)
    return {
        "status": "running",
        "paper_trading": True,
        "uptime_seconds": uptime,
        "uptime": _fmt_uptime(uptime),
        "cycle_count": _cycle_count,
        "balance": round(p.usdc_balance, 2),
        "starting_balance": round(p.starting_balance, 2),
        "total_value": round(p.total_value(), 2),
        "pnl": total_pnl,
        "pnl_pct": round((total_pnl / p.starting_balance) * 100, 3),
        "realized_pnl": realized,
        "realized_pnl_pct": round((realized / p.starting_balance) * 100, 3),
        "open_positions": len(p.positions),
        "closed_positions": len(p.closed_positions),
        "total_trades": len(p.trades),
        "exposure": round(p.exposure(), 2),
        "fees_paid": round(p.total_fees_paid(), 2),
        "win_rate": p.win_rate(),
        "trades_per_hour": trades_per_hour,
    }


@app.get("/api/pnl_history")
def pnl_history():
    if not _portfolio:
        return []
    return _portfolio.pnl_history


@app.get("/api/positions")
def positions():
    if not _portfolio:
        return []
    return [
        {
            "token_id": tid[:16] + "...",
            "question": pos.market_question[:70],
            "outcome": pos.outcome,
            "contracts": round(pos.contracts, 4),
            "avg_cost": round(pos.avg_cost, 4),
            "cost_basis": round(pos.cost_basis, 2),
            "strategy": pos.strategy,
            "opened_at": int(pos.opened_at),
        }
        for tid, pos in _portfolio.positions.items()
    ]


@app.get("/api/closed_positions")
def closed_positions(limit: int = 100):
    if not _portfolio:
        return []
    recent = list(reversed(_portfolio.closed_positions))[:limit]
    return recent


@app.get("/api/trades")
def trades(limit: int = 100):
    if not _portfolio:
        return []
    recent = list(reversed(_portfolio.trades))[:limit]
    return [
        {
            "trade_id": t.trade_id,
            "strategy": t.strategy,
            "side": t.side,
            "contracts": round(t.contracts, 4),
            "price": round(t.price, 4),
            "usdc_amount": round(t.usdc_amount, 2),
            "fee": round(t.fee, 4),
            "timestamp": int(t.timestamp),
            "notes": t.notes[:80],
        }
        for t in recent
    ]


@app.get("/api/strategy_pnl")
def strategy_pnl():
    if not _portfolio:
        return {}
    return {k: round(v, 2) for k, v in _portfolio.strategy_pnl().items()}


@app.get("/api/strategy_trades")
def strategy_trades():
    """Trade counts per strategy over time buckets."""
    if not _portfolio:
        return {}
    counts: dict[str, int] = {}
    for t in _portfolio.trades:
        counts[t.strategy] = counts.get(t.strategy, 0) + 1
    return counts


@app.get("/api/logs")
def logs(since: float = 0, limit: int = 200):
    from src.utils.logger import get_log_buffer
    all_logs = get_log_buffer()
    filtered = [l for l in all_logs if l["t"] > since]
    return filtered[-limit:]


@app.get("/api/logs/stream")
async def logs_stream():
    """SSE stream of log lines."""
    from src.utils.logger import get_log_buffer
    async def generator():
        last_count = 0
        while True:
            buf = get_log_buffer()
            if len(buf) > last_count:
                for entry in buf[last_count:]:
                    yield {"data": json.dumps(entry)}
                last_count = len(buf)
            await asyncio.sleep(0.5)
    return EventSourceResponse(generator())


# ------------------------------------------------------------------ #
#  Meta-agent API endpoints                                            #
# ------------------------------------------------------------------ #

@app.get("/api/meta/history")
def meta_history():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)[:10]
    results = []
    for f in files:
        try:
            with open(f) as fp:
                data = json.load(fp)
            results.append({
                "file": os.path.basename(f),
                "timestamp": data.get("timestamp", 0),
                "proposed_changes": data.get("proposed_changes", {}),
                "applied_changes": data.get("applied_changes", []),
                "analysis_preview": data.get("analysis", "")[:300],
                "portfolio_pnl": data.get("portfolio_snapshot", {}).get("portfolio", {}).get("total_pnl_usdc", 0),
            })
        except Exception:
            pass
    return results


@app.get("/api/meta/latest")
def meta_latest():
    files = sorted(glob.glob("logs/meta_agent_*.json"), reverse=True)
    if not files:
        return {"found": False}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return {"found": True, **data}
    except Exception:
        return {"found": False}


# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


# ------------------------------------------------------------------ #
#  Dashboard HTML                                                      #
# ------------------------------------------------------------------ #

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Arb Bot</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#e0e0e0;font-family:'Segoe UI',sans-serif;font-size:13px}
header{background:#111;border-bottom:1px solid #222;padding:12px 20px;display:flex;align-items:center;gap:16px}
header h1{color:#00e5ff;font-size:1.1rem;letter-spacing:.05em}
#mode-badge{font-size:.7rem;padding:3px 10px;border-radius:4px;background:#1a3a4a;color:#00e5ff}
#uptime-info{color:#555;font-size:.75rem;margin-left:auto}

.tabs{display:flex;background:#111;border-bottom:1px solid #1e1e1e;padding:0 20px}
.tab{padding:10px 18px;cursor:pointer;color:#666;font-size:.8rem;border-bottom:2px solid transparent;transition:all .2s}
.tab:hover{color:#aaa}
.tab.active{color:#00e5ff;border-bottom-color:#00e5ff}

.page{display:none;padding:20px;animation:fadein .2s}
.page.active{display:block}
@keyframes fadein{from{opacity:0}to{opacity:1}}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:18px}
.card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px}
.card .lbl{color:#555;font-size:.65rem;text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.card .val{font-size:1.3rem;font-weight:700;color:#fff}
.card .sub{color:#444;font-size:.65rem;margin-top:3px}
.card .val.green{color:#00e676}.card .val.red{color:#ff5252}.card .val.blue{color:#00e5ff}.card .val.yellow{color:#ffd740}.card .val.purple{color:#ce93d8}

.pnl-split{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px;margin-bottom:18px}
.pnl-split h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:12px}
.pnl-row{display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #1a1a1a}
.pnl-row:last-child{border-bottom:none}
.pnl-label{color:#888;font-size:.78rem}
.pnl-value{font-size:.95rem;font-weight:700}
.pnl-note{color:#444;font-size:.65rem;margin-top:2px}

.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px}
.chart-box{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px}
.chart-box h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
.chart-box canvas{max-height:200px}

.section{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:14px;margin-bottom:14px}
.section h3{font-size:.72rem;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#444;font-weight:500;padding:5px 8px;border-bottom:1px solid #1e1e1e;font-size:.7rem}
td{padding:5px 8px;border-bottom:1px solid #141414;font-size:.75rem}
tr:last-child td{border-bottom:none}
tr:hover td{background:#181818}
.buy{color:#00e676}.sell{color:#ff5252}.win{color:#00e676}.loss{color:#ff5252}
.badge{display:inline-block;padding:1px 7px;border-radius:3px;font-size:.65rem;font-weight:600}
.badge.rebalancing{background:#1a2a1a;color:#00e676}
.badge.combinatorial{background:#1a1a2a;color:#7986cb}
.badge.latency_arb{background:#2a1a1a;color:#ff7043}
.badge.market_making{background:#2a2a1a;color:#ffd740}
.badge.resolution{background:#1a2a2a;color:#4dd0e1}
.badge.event_driven{background:#2a1a2a;color:#ce93d8}

.strat-bars{display:flex;flex-direction:column;gap:8px}
.strat-row{display:flex;align-items:center;gap:10px}
.strat-row .name{width:150px;font-size:.75rem;color:#888}
.strat-row .bar-wrap{flex:1;background:#0d0d0d;border-radius:4px;height:20px;overflow:hidden}
.strat-row .bar{height:100%;border-radius:4px;display:flex;align-items:center;padding:0 8px;font-size:.7rem;font-weight:700;min-width:50px;transition:width .5s}
.bar.pos{background:#003d1a;color:#00e676}.bar.neg{background:#3d0000;color:#ff5252}

#log-feed{background:#0d0d0d;border:1px solid #1e1e1e;border-radius:8px;height:500px;overflow-y:auto;padding:10px;font-family:monospace;font-size:.72rem}
.log-line{padding:1px 0;border-bottom:1px solid #111;line-height:1.5}
.log-line .ts{color:#444;margin-right:8px}
.log-line .lvl{margin-right:8px;font-weight:700}
.log-line .lvl.INFO{color:#00e5ff}.log-line .lvl.WARNING{color:#ffd740}.log-line .lvl.ERROR{color:#ff5252}.log-line .lvl.DEBUG{color:#555}.log-line .lvl.SUCCESS{color:#00e676}

.meta-card{background:#141414;border:1px solid #1e1e1e;border-radius:8px;padding:16px;margin-bottom:14px}
.meta-card h3{color:#7986cb;margin-bottom:8px;font-size:.85rem}
.meta-analysis{color:#ccc;font-size:.78rem;line-height:1.6;white-space:pre-wrap;max-height:300px;overflow-y:auto}
.change-table td:nth-child(3){color:#00e676}
.ts-small{color:#555;font-size:.65rem}
.no-data{color:#333;text-align:center;padding:30px;font-size:.8rem}
#last-update{color:#333;font-size:.65rem;text-align:right;padding:6px 20px}
</style>
</head>
<body>

<header>
  <h1>Polymarket Arb Bot</h1>
  <span id="mode-badge">PAPER</span>
  <span id="uptime-info">loading...</span>
</header>

<div class="tabs">
  <div class="tab active" onclick="showTab('overview')">Overview</div>
  <div class="tab" onclick="showTab('live')">Live Feed</div>
  <div class="tab" onclick="showTab('positions')">Positions</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('meta')">Meta-Agent</div>
</div>

<!-- OVERVIEW TAB -->
<div class="page active" id="tab-overview">

  <!-- Top stats row -->
  <div class="cards">
    <div class="card"><div class="lbl">Cash Balance</div><div class="val blue" id="balance">--</div></div>
    <div class="card"><div class="lbl">Total Value</div><div class="val" id="total-value">--</div><div class="sub" id="total-pnl-sub">--</div></div>
    <div class="card">
      <div class="lbl">Realized P&amp;L ✓</div>
      <div class="val" id="realized-pnl">--</div>
      <div class="sub" id="realized-pnl-pct">-- | <span id="closed-count">0</span> closed</div>
    </div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="win-rate">--</div><div class="sub">closed positions</div></div>
    <div class="card"><div class="lbl">Open Positions</div><div class="val yellow" id="pos-count">--</div><div class="sub" id="exposure-sub">--</div></div>
    <div class="card"><div class="lbl">Trades / hr</div><div class="val purple" id="trades-per-hr">--</div><div class="sub" id="total-trades-sub">-- total</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val red" id="fees">--</div></div>
  </div>

  <div class="chart-grid">
    <div class="chart-box">
      <h3>Portfolio Value Over Time</h3>
      <canvas id="pnlChart"></canvas>
    </div>
    <div class="chart-box">
      <h3>Trades Per Strategy</h3>
      <canvas id="stratChart"></canvas>
    </div>
  </div>

  <div class="section">
    <h3>Strategy P&L</h3>
    <div class="strat-bars" id="strat-bars"><div class="no-data">Waiting for trades...</div></div>
  </div>
</div>

<!-- LIVE FEED TAB -->
<div class="page" id="tab-live">
  <div class="cards" style="grid-template-columns:repeat(5,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Cycle</div><div class="val blue" id="live-cycle">--</div></div>
    <div class="card"><div class="lbl">Uptime</div><div class="val" id="live-uptime">--</div></div>
    <div class="card"><div class="lbl">Realized P&amp;L</div><div class="val" id="live-realized">--</div></div>
    <div class="card"><div class="lbl">Win Rate</div><div class="val" id="live-winrate">--</div></div>
    <div class="card"><div class="lbl">Trades</div><div class="val" id="live-trades">--</div></div>
  </div>
  <div class="section">
    <h3>Bot Log Stream <span style="color:#555;font-weight:normal">(last 500 lines)</span>
      <label style="float:right;color:#555;font-size:.7rem"><input type="checkbox" id="autoscroll" checked> Auto-scroll</label>
    </h3>
    <div id="log-feed"></div>
  </div>
</div>

<!-- POSITIONS TAB -->
<div class="page" id="tab-positions">
  <div class="section">
    <h3>Open Positions (<span id="open-pos-count">0</span>)</h3>
    <div id="positions-table"><div class="no-data">No open positions</div></div>
  </div>
  <div class="section">
    <h3>Closed Positions — Recent 100 <span style="color:#555;font-weight:normal;font-size:.65rem">These are REAL results</span></h3>
    <div id="closed-table"><div class="no-data">No closed positions yet</div></div>
  </div>
</div>

<!-- TRADES TAB -->
<div class="page" id="tab-trades">
  <div class="cards" style="grid-template-columns:repeat(4,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Total Trades</div><div class="val blue" id="t-total">--</div></div>
    <div class="card"><div class="lbl">Buy Trades</div><div class="val green" id="t-buys">--</div></div>
    <div class="card"><div class="lbl">Sell Trades</div><div class="val red" id="t-sells">--</div></div>
    <div class="card"><div class="lbl">Fees Paid</div><div class="val yellow" id="t-fees">--</div></div>
  </div>
  <div class="section">
    <h3>Recent Trades (last 100)</h3>
    <div id="trades-table"><div class="no-data">No trades yet</div></div>
  </div>
</div>

<!-- META-AGENT TAB -->
<div class="page" id="tab-meta">
  <div class="cards" style="grid-template-columns:repeat(3,1fr);margin-bottom:14px">
    <div class="card"><div class="lbl">Analyses Run</div><div class="val blue" id="meta-count">--</div></div>
    <div class="card"><div class="lbl">Last Run</div><div class="val" id="meta-last">--</div></div>
    <div class="card"><div class="lbl">Next Run</div><div class="val yellow" id="meta-next">--</div></div>
  </div>
  <div id="meta-latest-card">
    <div class="no-data">No meta-agent analysis yet.</div>
  </div>
  <div class="section" style="margin-top:14px">
    <h3>Analysis History</h3>
    <div id="meta-history"></div>
  </div>
</div>

<div id="last-update">--</div>

<script>
const $=id=>document.getElementById(id);
const fmt=(n,d=2)=>n==null?'--':'$'+Number(n).toFixed(d).replace(/\B(?=(\d{3})+(?!\d))/g,',');
const fmtPnl=n=>{if(n==null)return'--';const s=n>=0?'+':'-';return s+'$'+Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g,',')};
const fmtN=n=>n==null?'--':Number(n).toFixed(4);
const ts=t=>new Date(t*1000).toLocaleTimeString();
const tsDate=t=>new Date(t*1000).toLocaleString();
const badge=s=>`<span class="badge ${s}">${s}</span>`;
const pnlClass=n=>n>=0?'green':'red';

let currentTab='overview';
function showTab(name){
  const tabs=['overview','live','positions','trades','meta'];
  document.querySelectorAll('.tab').forEach((t,i)=>{t.classList.toggle('active',tabs[i]===name)});
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  $('tab-'+name).classList.add('active');
  currentTab=name;
}

const chartDefaults={responsive:true,maintainAspectRatio:true,plugins:{legend:{display:false}},scales:{x:{display:false,grid:{color:'#1a1a1a'}},y:{grid:{color:'#1a1a1a'},ticks:{color:'#555',font:{size:10}}}}};

const pnlCtx=$('pnlChart').getContext('2d');
const pnlChart=new Chart(pnlCtx,{type:'line',data:{labels:[],datasets:[{label:'Portfolio Value',data:[],borderColor:'#00e5ff',backgroundColor:'rgba(0,229,255,.05)',borderWidth:1.5,pointRadius:0,fill:true,tension:.3},{label:'Realized P&L',data:[],borderColor:'#00e676',backgroundColor:'transparent',borderWidth:1.5,pointRadius:0,tension:.3}]},options:{...chartDefaults,plugins:{legend:{display:true,labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

const stratCtx=$('stratChart').getContext('2d');
const stratChart=new Chart(stratCtx,{type:'doughnut',data:{labels:[],datasets:[{data:[],backgroundColor:['#00e676','#7986cb','#ff7043','#ffd740','#4dd0e1','#ce93d8'],borderColor:'#0a0a0a',borderWidth:2}]},options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{position:'right',labels:{color:'#666',font:{size:10},boxWidth:10}}}}});

function updatePnlChart(history){
  if(!history.length)return;
  const step=Math.max(1,Math.floor(history.length/150));
  const sampled=history.filter((_,i)=>i%step===0||i===history.length-1);
  pnlChart.data.labels=sampled.map(p=>ts(p.t));
  pnlChart.data.datasets[0].data=sampled.map(p=>p.value);
  pnlChart.data.datasets[1].data=sampled.map(p=>p.pnl);
  pnlChart.update('none');
}

function updateStratChart(counts){
  const entries=Object.entries(counts);
  stratChart.data.labels=entries.map(([k])=>k);
  stratChart.data.datasets[0].data=entries.map(([,v])=>v);
  stratChart.update('none');
}

async function fetchAll(){
  try{
    const [status,pnlH,stratPnl,stratTrades]=await Promise.all([
      fetch('/api/status').then(r=>r.json()),
      fetch('/api/pnl_history').then(r=>r.json()),
      fetch('/api/strategy_pnl').then(r=>r.json()),
      fetch('/api/strategy_trades').then(r=>r.json()),
    ]);
    updateStatus(status);
    updatePnlChart(pnlH);
    updateStratPnl(stratPnl);
    updateStratChart(stratTrades);

    if(currentTab==='positions'){
      const [open,closed]=await Promise.all([
        fetch('/api/positions').then(r=>r.json()),
        fetch('/api/closed_positions').then(r=>r.json()),
      ]);
      updatePositions(open,closed);
    }
    if(currentTab==='trades'){const d=await fetch('/api/trades').then(r=>r.json());updateTrades(d,status);}
    if(currentTab==='meta'){fetchMeta();}

    $('last-update').textContent='Updated: '+new Date().toLocaleTimeString();
  }catch(e){$('last-update').textContent='Connection error...';}
}

function updateStatus(s){
  $('uptime-info').textContent='Uptime: '+s.uptime+' | Cycles: '+s.cycle_count;
  $('balance').textContent=fmt(s.balance);

  const tv=s.total_value||0,tp=s.pnl||0,tpp=s.pnl_pct||0;
  $('total-value').textContent=fmt(tv);
  $('total-pnl-sub').textContent=(tp>=0?'+':'')+tp.toFixed(2)+' ('+tpp.toFixed(2)+'%)';
  $('total-pnl-sub').style.color=tp>=0?'#00e676':'#ff5252';

  const rp=s.realized_pnl||0,rpp=s.realized_pnl_pct||0;
  $('realized-pnl').textContent=fmtPnl(rp);
  $('realized-pnl').className='val '+(rp>=0?'green':'red');
  $('realized-pnl-pct').innerHTML=(rpp>=0?'+':'')+rpp.toFixed(2)+'% | <span id="closed-count">'+s.closed_positions+'</span> closed';

  const wr=s.win_rate||0;
  $('win-rate').textContent=wr.toFixed(1)+'%';
  $('win-rate').className='val '+(wr>=50?'green':'red');

  $('pos-count').textContent=s.open_positions;
  $('exposure-sub').textContent='Exposure: '+fmt(s.exposure);

  $('trades-per-hr').textContent=s.trades_per_hour||'--';
  $('total-trades-sub').textContent=(s.total_trades||0)+' total';

  $('fees').textContent=fmt(s.fees_paid);

  // live tab
  $('live-cycle').textContent=s.cycle_count;
  $('live-uptime').textContent=s.uptime;
  $('live-realized').textContent=fmtPnl(rp);
  $('live-realized').className='val '+(rp>=0?'green':'red');
  $('live-winrate').textContent=wr.toFixed(1)+'%';
  $('live-winrate').className='val '+(wr>=50?'green':'red');
  $('live-trades').textContent=s.total_trades;
}

function updateStratPnl(data){
  const entries=Object.entries(data);
  if(!entries.length){$('strat-bars').innerHTML='<div class="no-data">Waiting for trades...</div>';return;}
  const max=Math.max(...entries.map(([,v])=>Math.abs(v)),1);
  $('strat-bars').innerHTML=entries.sort((a,b)=>b[1]-a[1]).map(([name,val])=>{
    const pct=Math.abs(val)/max*100,cls=val>=0?'pos':'neg',sign=val>=0?'+':'';
    return`<div class="strat-row"><div class="name">${name}</div><div class="bar-wrap"><div class="bar ${cls}" style="width:${Math.max(pct,5)}%">${sign}$${val.toFixed(2)}</div></div></div>`;
  }).join('');
}

function updatePositions(open,closed){
  $('open-pos-count').textContent=open.length;
  if(!open.length){
    $('positions-table').innerHTML='<div class="no-data">No open positions</div>';
  }else{
    $('positions-table').innerHTML=`<table>
      <tr><th>Market</th><th>Outcome</th><th>Contracts</th><th>Avg Cost</th><th>Cost Basis</th><th>Strategy</th><th>Opened</th></tr>
      ${open.map(p=>`<tr>
        <td title="${p.question}">${p.question}</td>
        <td>${p.outcome}</td>
        <td>${p.contracts}</td>
        <td>${fmtN(p.avg_cost)}</td>
        <td>${fmt(p.cost_basis)}</td>
        <td>${badge(p.strategy)}</td>
        <td class="ts-small">${ts(p.opened_at)}</td>
      </tr>`).join('')}
    </table>`;
  }

  if(!closed.length){
    $('closed-table').innerHTML='<div class="no-data">No closed positions yet — positions close when fully sold</div>';
  }else{
    const totalR=closed.reduce((s,p)=>s+p.realized_pnl,0);
    const wins=closed.filter(p=>p.realized_pnl>0).length;
    $('closed-table').innerHTML=`
      <div style="display:flex;gap:20px;margin-bottom:10px;font-size:.78rem">
        <span>Total Realized: <strong class="${totalR>=0?'win':'loss'}">${fmtPnl(totalR)}</strong></span>
        <span>Win Rate: <strong class="${wins/closed.length>=.5?'win':'loss'}">${(wins/closed.length*100).toFixed(1)}%</strong></span>
        <span style="color:#555">(${wins}W / ${closed.length-wins}L of ${closed.length} closed)</span>
      </div>
      <table>
        <tr><th>Market</th><th>Outcome</th><th>Strategy</th><th>Realized P&L</th><th>Result</th><th>Closed</th><th>Duration</th></tr>
        ${closed.map(p=>{
          const dur=Math.round((p.closed_at-p.opened_at)/60);
          const durStr=dur<60?dur+'m':Math.round(dur/60)+'h '+dur%60+'m';
          const isWin=p.realized_pnl>0;
          return`<tr>
            <td title="${p.market_question||''}">${(p.market_question||'').slice(0,55)}</td>
            <td>${p.outcome||''}</td>
            <td>${badge(p.strategy)}</td>
            <td class="${isWin?'win':'loss'}">${fmtPnl(p.realized_pnl)}</td>
            <td><span style="color:${isWin?'#00e676':'#ff5252'};font-weight:700">${isWin?'WIN':'LOSS'}</span></td>
            <td class="ts-small">${ts(p.closed_at)}</td>
            <td class="ts-small">${durStr}</td>
          </tr>`;
        }).join('')}
      </table>`;
  }
}

function updateTrades(data,status){
  const buys=data.filter(t=>t.side==='BUY').length;
  $('t-total').textContent=status.total_trades;
  $('t-buys').textContent=buys;
  $('t-sells').textContent=data.length-buys;
  $('t-fees').textContent=fmt(status.fees_paid);
  if(!data.length){$('trades-table').innerHTML='<div class="no-data">No trades yet</div>';return;}
  $('trades-table').innerHTML=`<table>
    <tr><th>ID</th><th>Time</th><th>Strategy</th><th>Side</th><th>Contracts</th><th>Price</th><th>Amount</th><th>Notes</th></tr>
    ${data.map(t=>`<tr>
      <td>${t.trade_id}</td>
      <td class="ts-small">${ts(t.timestamp)}</td>
      <td>${badge(t.strategy)}</td>
      <td class="${t.side.toLowerCase()}">${t.side}</td>
      <td>${t.contracts}</td>
      <td>${fmtN(t.price)}</td>
      <td>${fmt(t.usdc_amount)}</td>
      <td style="color:#555">${t.notes}</td>
    </tr>`).join('')}
  </table>`;
}

async function fetchMeta(){
  const [hist,latest]=await Promise.all([
    fetch('/api/meta/history').then(r=>r.json()),
    fetch('/api/meta/latest').then(r=>r.json()),
  ]);
  $('meta-count').textContent=hist.length;
  $('meta-last').textContent=hist.length?tsDate(hist[0].timestamp):'Never';
  if(hist.length){
    const nextTs=(hist[0].timestamp||0)+1800;
    const diff=Math.round((nextTs-Date.now()/1000)/60);
    $('meta-next').textContent=diff>0?'in ~'+diff+'m':'soon';
  }

  if(latest.found){
    const ch=latest.proposed_changes||{};
    const rows=Object.entries(ch).map(([k,v])=>`<tr><td>${k}</td><td>${latest.current_values?.[k]||'?'}</td><td>${v}</td></tr>`).join('');
    $('meta-latest-card').innerHTML=`
      <div class="meta-card">
        <h3>Latest Analysis — ${tsDate(latest.timestamp)}</h3>
        <div class="meta-analysis">${latest.analysis||''}</div>
        ${rows?`<br><table class="change-table"><tr><th>Parameter</th><th>Was</th><th>Proposed</th></tr>${rows}</table>`:'<p style="color:#555;margin-top:8px;font-size:.75rem">No parameter changes suggested.</p>'}
      </div>`;
  }

  if(hist.length){
    $('meta-history').innerHTML=`<table>
      <tr><th>Time</th><th>Portfolio P&L</th><th>Changes Suggested</th><th>Preview</th></tr>
      ${hist.map(h=>`<tr>
        <td class="ts-small">${tsDate(h.timestamp)}</td>
        <td class="${h.portfolio_pnl>=0?'buy':'sell'}">${fmtPnl(h.portfolio_pnl)}</td>
        <td>${Object.keys(h.proposed_changes||{}).length} suggested / <span class="win">${(h.applied_changes||[]).length} applied</span></td>
        <td style="color:#555">${h.analysis_preview}</td>
      </tr>`).join('')}
    </table>`;
  }else{
    $('meta-history').innerHTML='<div class="no-data">No analyses yet.</div>';
  }
}

const evtSource=new EventSource('/api/logs/stream');
evtSource.onmessage=e=>{
  const entry=JSON.parse(e.data);
  const feed=$('log-feed');
  const d=document.createElement('div');
  d.className='log-line';
  const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit',fractionalSecondDigits:2});
  d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
  feed.appendChild(d);
  if($('autoscroll').checked)feed.scrollTop=feed.scrollHeight;
  while(feed.children.length>500)feed.removeChild(feed.firstChild);
};

fetch('/api/logs?limit=200').then(r=>r.json()).then(logs=>{
  const feed=$('log-feed');
  logs.forEach(entry=>{
    const d=document.createElement('div');
    d.className='log-line';
    const t=new Date(entry.t*1000).toLocaleTimeString('en',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    d.innerHTML=`<span class="ts">${t}</span><span class="lvl ${entry.level}">${entry.level.substring(0,4)}</span>${entry.msg}`;
    feed.appendChild(d);
  });
  feed.scrollTop=feed.scrollHeight;
});

fetchAll();
setInterval(fetchAll,3000);
</script>
</body>
</html>"""
