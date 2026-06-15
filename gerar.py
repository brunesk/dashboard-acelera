"""
Gera index.html com dados do Meta + Hotmart
Roda localmente ou via GitHub Actions (usa variáveis de ambiente)
Uso: python gerar.py [dias]
"""
import urllib.request, urllib.parse, json, subprocess, sys, os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DIAS       = int(sys.argv[1]) if len(sys.argv) > 1 else 14
AD_ACCOUNT = 'act_913802749957339'
ADSET_ID   = '120253420414220339'

def load_env():
    env = {}
    for candidate in ['.env', '../.env']:
        if os.path.exists(candidate):
            with open(candidate) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, v = line.split('=', 1)
                        env[k.strip()] = v.strip()
            break
    for key in ['META_ACCESS_TOKEN', 'HOTMART_BASIC']:
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env

def get_hotmart_token(basic):
    r = subprocess.run(['curl','-s','-X','POST',
        'https://api-sec-vlc.hotmart.com/security/oauth/token?grant_type=client_credentials',
        '-H', f'Authorization: {basic}'], capture_output=True, text=True)
    return json.loads(r.stdout)['access_token']

def fetch_hotmart_all(token):
    items, page_token = [], None
    while True:
        params = {'max_results': 100}
        if page_token: params['page_token'] = page_token
        url = 'https://developers.hotmart.com/payments/api/v1/sales/history?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as r: data = json.loads(r.read())
        items.extend(data.get('items', []))
        page_token = data.get('page_info', {}).get('next_page_token')
        if not page_token: break
    return items

def fetch_meta_daily(token, since, until):
    params = {
        'access_token': token, 'level': 'account', 'time_increment': '1',
        'time_range': json.dumps({'since': since, 'until': until}),
        'fields': 'date_start,spend,impressions,clicks,ctr,cpm,actions,action_values'
    }
    url = f'https://graph.facebook.com/v19.0/{AD_ACCOUNT}/insights?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url) as r: return json.loads(r.read()).get('data', [])
    except urllib.error.HTTPError as e:
        err = json.loads(e.read().decode())
        if 'expired' in err.get('error', {}).get('message', ''):
            print('AVISO: Token Meta expirado.')
        return None

def fetch_meta_ads(token, since, until):
    url = f'https://graph.facebook.com/v19.0/{ADSET_ID}/ads?fields=id,name,status&access_token={token}'
    try:
        with urllib.request.urlopen(url) as r: ads = json.loads(r.read()).get('data', [])
    except: return None
    results = []
    for ad in ads:
        params = urllib.parse.urlencode({
            'access_token': token,
            'time_range': json.dumps({'since': since, 'until': until}),
            'fields': 'spend,impressions,clicks,ctr,cpm,cpc,actions,action_values'
        })
        try:
            with urllib.request.urlopen(f'https://graph.facebook.com/v19.0/{ad["id"]}/insights?{params}') as r:
                ins = json.loads(r.read()).get('data', [])
        except: ins = []
        if ins:
            d = ins[0]
            spend     = float(d.get('spend', 0))
            purchases = next((int(a['value'])   for a in d.get('actions', [])       if a['action_type']=='purchase'), 0)
            revenue   = next((float(a['value']) for a in d.get('action_values', []) if a['action_type']=='purchase'), 0)
            results.append({
                'name': ad['name'], 'status': ad['status'], 'spend': spend,
                'impressions': int(d.get('impressions', 0)), 'ctr': float(d.get('ctr', 0)),
                'cpm': float(d.get('cpm', 0)), 'purchases': purchases, 'revenue': revenue,
                'roas': revenue/spend if spend>0 else 0,
                'cpa': spend/purchases if purchases>0 else 0,
            })
    return sorted(results, key=lambda x: x['spend'], reverse=True)

# ── Buscar dados ──────────────────────────────────────────────────────────────
env = load_env()
BRT = timezone(timedelta(hours=-3))
now = datetime.now(BRT)
start = (now - timedelta(days=DIAS)).replace(hour=0, minute=0, second=0, microsecond=0)
since_str = start.strftime('%Y-%m-%d')
until_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
start_ms  = int(start.timestamp() * 1000)
end_ms    = int((now - timedelta(days=1)).replace(hour=23,minute=59,second=59).timestamp()*1000)

print(f'Período: {start.strftime("%d/%m")} a {(now-timedelta(days=1)).strftime("%d/%m/%Y")}')
print('Buscando Hotmart...')
hm_token  = get_hotmart_token(env['HOTMART_BASIC'])
hm_all    = fetch_hotmart_all(hm_token)
print('Buscando Meta...')
meta_token = env['META_ACCESS_TOKEN']
meta_daily = fetch_meta_daily(meta_token, since_str, until_str)
ads_data   = fetch_meta_ads(meta_token, since_str, until_str) if meta_daily is not None else None

# Processar Hotmart
hm = defaultdict(lambda: {'v':0,'bruto':0.0,'liq':0.0})
for item in hm_all:
    p = item['purchase']
    if p['approved_date'] < start_ms or p['approved_date'] > end_ms: continue
    day = datetime.fromtimestamp(p['approved_date']/1000, BRT).strftime('%Y-%m-%d')
    if p['status'] in ('COMPLETE','APPROVED'):
        price = float(p['price']['value'])
        fee   = float(p.get('hotmart_fee',{}).get('total',0))
        hm[day]['v']+=1; hm[day]['bruto']+=price; hm[day]['liq']+=price-fee

# Processar Meta
meta = {}
if meta_daily:
    for d in meta_daily:
        spend = float(d.get('spend',0))
        purchases = next((int(a['value']) for a in d.get('actions',[]) if a['action_type']=='purchase'),0)
        meta[d['date_start']] = {'spend':spend,'purchases':purchases}

all_days = sorted(set(list(hm.keys())+list(meta.keys())))
dias_fmt = [datetime.strptime(d,'%Y-%m-%d').strftime('%d/%m') for d in all_days]

hm_liq_series  = [round(hm.get(d,{}).get('liq',0),0)   for d in all_days]
gasto_series   = [round(meta.get(d,{}).get('spend',0),0) for d in all_days]
lucro_series   = [round(hm.get(d,{}).get('liq',0)-meta.get(d,{}).get('spend',0),0) for d in all_days]
vendas_series  = [hm.get(d,{}).get('v',0) for d in all_days]

tv  = sum(h['v']     for h in hm.values())
tb  = sum(h['bruto'] for h in hm.values())
tl  = sum(h['liq']   for h in hm.values())
ts  = sum(m['spend'] for m in meta.values())
tlr = tl - ts
roas   = tb/ts if ts>0 else 0
ticket = tb/tv if tv>0 else 0
meta_ok = meta_daily is not None

# ── HTML ──────────────────────────────────────────────────────────────────────
def brl(v): return f'R${v:,.0f}'.replace(',','X').replace('.', ',').replace('X','.')
def pct_roas(v): return f'{v:.2f}x'

ads_rows = ''
if ads_data:
    max_spend = ads_data[0]['spend'] if ads_data else 1
    medals = ['🥇','🥈','🥉','4º','5º']
    for i, a in enumerate(ads_data):
        rc = '#16a34a' if a['roas']>=1.5 else '#ca8a04' if a['roas']>=1.0 else '#dc2626'
        sb = '#dcfce7' if a['status']=='ACTIVE' else '#f1f5f9'
        sc = '#16a34a' if a['status']=='ACTIVE' else '#64748b'
        bp = int(a['spend']/max_spend*100)
        ads_rows += f'''<tr class="border-b border-slate-100 hover:bg-slate-50">
          <td class="py-3 px-4"><div class="flex items-center gap-2"><span>{medals[i] if i<5 else ""}</span><div><p class="font-semibold text-slate-800 text-sm">{a["name"]}</p><div class="mt-1 h-1 bg-slate-100 rounded-full w-32"><div class="h-full rounded-full bg-blue-400" style="width:{bp}%"></div></div></div></div></td>
          <td class="py-3 px-4 text-center"><span class="text-xs font-semibold px-2 py-0.5 rounded-full" style="background:{sb};color:{sc}">{a["status"]}</span></td>
          <td class="py-3 px-4 text-right font-semibold text-slate-700">{brl(a["spend"])}</td>
          <td class="py-3 px-4 text-right text-slate-600">{a["impressions"]:,}</td>
          <td class="py-3 px-4 text-right text-slate-600">{a["ctr"]:.1f}%</td>
          <td class="py-3 px-4 text-right text-slate-600">{brl(a["cpm"])}</td>
          <td class="py-3 px-4 text-right text-slate-600">{a["purchases"]}</td>
          <td class="py-3 px-4 text-right font-bold" style="color:{rc}">{a["roas"]:.2f}x</td>
          <td class="py-3 px-4 text-right text-slate-600">{brl(a["cpa"]) if a["cpa"]>0 else "—"}</td>
        </tr>'''

daily_rows = ''
for i, d in enumerate(all_days):
    h = hm.get(d,{}); m = meta.get(d,{})
    liq=h.get('liq',0); bruto=h.get('bruto',0); gasto=m.get('spend',0)
    lucro=liq-gasto; roas_d=bruto/gasto if gasto>0 else 0
    lc='text-green-600' if lucro>=0 else 'text-red-500'
    rc='text-green-600' if roas_d>=1.5 else 'text-amber-600' if roas_d>=1.0 else 'text-slate-400'
    daily_rows += f'''<tr class="border-b border-slate-100 hover:bg-slate-50">
      <td class="py-3 px-4 font-semibold text-slate-700">{dias_fmt[i]}</td>
      <td class="py-3 px-4 text-right text-slate-600">{h.get("v",0)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(bruto)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(liq)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(gasto) if gasto else "—"}</td>
      <td class="py-3 px-4 text-right font-bold {lc}">{("+" if lucro>=0 else "")}{brl(lucro)}</td>
      <td class="py-3 px-4 text-right font-semibold {rc}">{pct_roas(roas_d) if gasto>0 else "—"}</td>
    </tr>'''

periodo = f'{start.strftime("%d/%m")} – {(now-timedelta(days=1)).strftime("%d/%m/%Y")}'
atualizado = now.strftime('%d/%m/%Y às %H:%M (BRT)')
aviso_meta = '' if meta_ok else '<div class="bg-amber-50 border border-amber-200 rounded-2xl px-5 py-3 flex items-center gap-3 mb-6 text-amber-800 text-sm"><span>⚠</span> Dados Meta indisponíveis — token expirado. Atualize META_ACCESS_TOKEN no GitHub Secrets.</div>'

criativo_section = ''
if ads_data:
    criativo_section = f'''<div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100"><h2 class="text-lg font-bold text-slate-800">Criativos</h2><p class="text-sm text-slate-400 mt-0.5">Ordenado por investimento · ROAS = atribuição pixel Meta</p></div>
    <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide"><th class="text-left py-3 px-4">Criativo</th><th class="text-center py-3 px-4">Status</th><th class="text-right py-3 px-4">Gasto</th><th class="text-right py-3 px-4">Impressões</th><th class="text-right py-3 px-4">CTR</th><th class="text-right py-3 px-4">CPM</th><th class="text-right py-3 px-4">Compras</th><th class="text-right py-3 px-4">ROAS</th><th class="text-right py-3 px-4">CPA</th></tr></thead><tbody>{ads_rows}</tbody></table></div>
    <div class="px-6 py-3 bg-slate-50 border-t text-xs text-slate-400">ROAS real do período (Hotmart bruto / Meta gasto): <strong class="text-slate-600">{roas:.2f}x</strong></div>
    </div>'''

html = f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1e3a8a">
<title>Dashboard — Acelera Shopee</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
* {{ box-sizing:border-box }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#f8fafc }}
.card {{ background:white; border-radius:16px; box-shadow:0 1px 3px rgba(0,0,0,.07); border:1px solid #f1f5f9 }}
</style>
</head>
<body class="min-h-screen">

<div style="background:linear-gradient(135deg,#1e3a8a,#1d4ed8,#2563eb)" class="text-white px-4 py-7 md:px-6 md:py-8">
  <div class="max-w-6xl mx-auto">
    <div class="flex items-start justify-between flex-wrap gap-3 mb-7">
      <div>
        <div class="flex items-center gap-2.5 mb-1">
          <div class="w-8 h-8 bg-white/20 rounded-xl flex items-center justify-center">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
          </div>
          <h1 class="text-xl font-black tracking-tight">Acelera Shopee</h1>
        </div>
        <p class="text-blue-200 text-xs">Meta Ads + Hotmart · {periodo}</p>
      </div>
      <div class="text-right">
        <div class="inline-flex items-center gap-1.5 bg-white/10 rounded-full px-3 py-1 text-xs text-blue-200">
          <span class="w-1.5 h-1.5 bg-green-400 rounded-full inline-block"></span>
          Atualizado {atualizado}
        </div>
      </div>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2.5">
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">Vendas</p>
        <p class="text-3xl font-black">{tv}</p>
        <p class="text-blue-300 text-xs mt-0.5">Hotmart</p>
      </div>
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">Rec. Bruta</p>
        <p class="text-xl font-black">{brl(tb)}</p>
        <p class="text-blue-300 text-xs mt-0.5">Hotmart</p>
      </div>
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">Gasto Meta</p>
        <p class="text-xl font-black">{brl(ts) if ts else "—"}</p>
        <p class="text-blue-300 text-xs mt-0.5">investido</p>
      </div>
      <div class="{'bg-green-400/30 border-green-300/40' if tlr>=0 else 'bg-red-400/30 border-red-300/40'} rounded-2xl p-3.5 border">
        <p class="{'text-green-200' if tlr>=0 else 'text-red-200'} text-[10px] font-semibold uppercase tracking-wide mb-1">Lucro Real</p>
        <p class="text-xl font-black {'text-green-300' if tlr>=0 else 'text-red-300'}">{"+" if tlr>=0 else ""}{brl(tlr)}</p>
        <p class="{'text-green-300' if tlr>=0 else 'text-red-300'} text-xs mt-0.5">{brl(tlr/DIAS)}/dia</p>
      </div>
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">ROAS Real</p>
        <p class="text-3xl font-black">{pct_roas(roas) if roas else "—"}</p>
        <p class="text-blue-300 text-xs mt-0.5">bruto/gasto</p>
      </div>
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">Ticket Médio</p>
        <p class="text-xl font-black">{brl(ticket)}</p>
        <p class="text-blue-300 text-xs mt-0.5">por venda</p>
      </div>
    </div>
  </div>
</div>

<div class="max-w-6xl mx-auto px-4 py-6 md:px-6 space-y-5">
  {aviso_meta}

  <div class="card p-5">
    <div class="flex items-center justify-between mb-5 flex-wrap gap-2">
      <div><h2 class="font-bold text-slate-800">Desempenho Diário</h2><p class="text-xs text-slate-400 mt-0.5">Receita Líq. HM vs Gasto Meta vs Lucro</p></div>
      <div class="flex items-center gap-4 text-xs text-slate-500">
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full inline-block bg-blue-500"></span>Rec. Líq.</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full inline-block bg-red-400"></span>Meta</span>
        <span class="flex items-center gap-1"><span class="w-2.5 h-2.5 rounded-full inline-block bg-emerald-500"></span>Lucro</span>
      </div>
    </div>
    <div style="height:260px"><canvas id="cDiario"></canvas></div>
  </div>

  <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
    <div class="card p-5">
      <h2 class="font-bold text-slate-800 mb-0.5">Vendas / Dia</h2>
      <p class="text-xs text-slate-400 mb-4">Compras no Hotmart</p>
      <div style="height:170px"><canvas id="cVendas"></canvas></div>
    </div>
    <div class="card p-5">
      <h2 class="font-bold text-slate-800 mb-0.5">Lucro Acumulado</h2>
      <p class="text-xs text-slate-400 mb-4">Progressão no período</p>
      <div style="height:170px"><canvas id="cAcum"></canvas></div>
    </div>
  </div>

  {criativo_section}

  <div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100"><h2 class="text-lg font-bold text-slate-800">Dia a Dia</h2></div>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide">
        <th class="text-left py-3 px-4">Dia</th><th class="text-right py-3 px-4">Vendas</th>
        <th class="text-right py-3 px-4">Bruto</th><th class="text-right py-3 px-4">Líq. HM</th>
        <th class="text-right py-3 px-4">Gasto Meta</th><th class="text-right py-3 px-4">Lucro</th>
        <th class="text-right py-3 px-4">ROAS</th>
      </tr></thead>
      <tbody>{daily_rows}</tbody>
      <tfoot><tr class="bg-slate-50 font-bold border-t-2 border-slate-200">
        <td class="py-3 px-4 text-slate-800">TOTAL</td>
        <td class="py-3 px-4 text-right text-slate-800">{tv}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(tb)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(tl)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(ts) if ts else "—"}</td>
        <td class="py-3 px-4 text-right text-green-600">+{brl(tlr)}</td>
        <td class="py-3 px-4 text-right text-green-600">{pct_roas(roas) if roas else "—"}</td>
      </tr></tfoot>
    </table></div>
  </div>
  <p class="text-center text-xs text-slate-400 pb-4">Atualizado automaticamente todo dia à meia-noite · {periodo}</p>
</div>

<script>
const L={json.dumps(dias_fmt)},liq={json.dumps(hm_liq_series)},g={json.dumps(gasto_series)},lu={json.dumps(lucro_series)},v={json.dumps(vendas_series)};
const ac=lu.reduce((a,x,i)=>{{a.push((a[i-1]||0)+x);return a}},[]);
const pt=x=>'R$'+x.toLocaleString('pt-BR',{{maximumFractionDigits:0}});
const cfg={{responsive:true,maintainAspectRatio:false,interaction:{{mode:'index',intersect:false}}}};
new Chart(cDiario,{{data:{{labels:L,datasets:[
  {{type:'line',label:'Rec. Líq.',data:liq,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.08)',fill:true,tension:.4,pointRadius:3,borderWidth:2.5}},
  {{type:'line',label:'Gasto Meta',data:g,borderColor:'#f87171',backgroundColor:'rgba(248,113,113,.08)',fill:true,tension:.4,pointRadius:3,borderWidth:2.5}},
  {{type:'bar',label:'Lucro',data:lu,backgroundColor:lu.map(x=>x>=0?'rgba(16,185,129,.75)':'rgba(239,68,68,.7)'),borderRadius:4}}
]}},options:{{...cfg,plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:c=>` ${{c.dataset.label}}: ${{pt(c.parsed.y)}}`}}}}}},scales:{{x:{{grid:{{display:false}}}},y:{{grid:{{color:'#f1f5f9'}},ticks:{{callback:pt}}}}}}}}));
new Chart(cVendas,{{type:'bar',data:{{labels:L,datasets:[{{data:v,backgroundColor:'#bfdbfe',borderColor:'#3b82f6',borderWidth:1.5,borderRadius:5}}]}},options:{{...cfg,plugins:{{legend:{{display:false}}}},scales:{{x:{{grid:{{display:false}}}},y:{{ticks:{{stepSize:2}}}}}}}}));
new Chart(cAcum,{{type:'line',data:{{labels:L,datasets:[{{data:ac,borderColor:'#10b981',backgroundColor:'rgba(16,185,129,.1)',fill:true,tension:.4,pointRadius:3,borderWidth:2.5}}]}},options:{{...cfg,plugins:{{legend:{{display:false}}}},scales:{{x:{{grid:{{display:false}}}},y:{{ticks:{{callback:pt}}}}}}}}));
</script>
</body>
</html>'''

with open('index.html', 'w', encoding='utf-8') as f:
    f.write(html)

status = 'Meta + Hotmart' if meta_ok else 'só Hotmart (token Meta expirado)'
print(f'index.html gerado — {status}')
