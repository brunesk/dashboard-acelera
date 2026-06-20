"""
Gera index.html com dados do Meta + Hotmart
- KPIs e gráficos: mês vigente (do dia 1 até ontem)
- Tabela inferior: fechamento de cada mês anterior
"""
import urllib.request, urllib.parse, json, subprocess, sys, os, calendar
from datetime import datetime, timezone, timedelta
from collections import defaultdict

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
    for key in ['META_ACCESS_TOKEN', 'HOTMART_BASIC', 'WORKFLOW_TOKEN']:
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

# ── Datas ─────────────────────────────────────────────────────────────────────
env = load_env()
BRT = timezone(timedelta(hours=-3))
now = datetime.now(BRT)
today = now.replace(hour=0, minute=0, second=0, microsecond=0)

curr_month_key = today.strftime('%Y-%m')
month_start = today.replace(day=1)
month_start_str = month_start.strftime('%Y-%m-%d')
today_str      = today.strftime('%Y-%m-%d')
month_start_ms = int(month_start.timestamp() * 1000)
today_end_ms   = int(today.replace(hour=23, minute=59, second=59).timestamp() * 1000)

print(f'Mês vigente: {month_start.strftime("%d/%m")} – {today.strftime("%d/%m/%Y")}')

# ── Hotmart ───────────────────────────────────────────────────────────────────
print('Buscando Hotmart...')
hm_token = get_hotmart_token(env['HOTMART_BASIC'])
hm_all   = fetch_hotmart_all(hm_token)

hm_by_month = defaultdict(lambda: {'v':0,'bruto':0.0,'liq':0.0})
hm_curr_day = defaultdict(lambda: {'v':0,'bruto':0.0,'liq':0.0})

for item in hm_all:
    p = item['purchase']
    if p['status'] not in ('COMPLETE','APPROVED'): continue
    ts = p['approved_date']
    dt = datetime.fromtimestamp(ts/1000, BRT)
    m_key = dt.strftime('%Y-%m')
    d_key = dt.strftime('%Y-%m-%d')
    price = float(p['price']['value'])
    fee   = float(p.get('hotmart_fee',{}).get('total',0))

    hm_by_month[m_key]['v']     += 1
    hm_by_month[m_key]['bruto'] += price
    hm_by_month[m_key]['liq']   += price - fee

    if month_start_ms <= ts <= today_end_ms:
        hm_curr_day[d_key]['v']     += 1
        hm_curr_day[d_key]['bruto'] += price
        hm_curr_day[d_key]['liq']   += price - fee

all_months  = sorted(hm_by_month.keys())
prev_months = [m for m in all_months if m < curr_month_key]

# ── Meta ──────────────────────────────────────────────────────────────────────
print('Buscando Meta (mês vigente)...')
meta_token = env['META_ACCESS_TOKEN']

meta_curr_raw = fetch_meta_daily(meta_token, month_start_str, today_str)
meta_ok       = meta_curr_raw is not None

meta_curr_day = {}
if meta_curr_raw:
    for d in meta_curr_raw:
        spend     = float(d.get('spend', 0))
        purchases = next((int(a['value']) for a in d.get('actions',[]) if a['action_type']=='purchase'), 0)
        meta_curr_day[d['date_start']] = {'spend': spend, 'purchases': purchases}

meta_by_month = {}
if prev_months and meta_ok:
    print('Buscando Meta (histórico mensal)...')
    hist_since = f'{prev_months[0]}-01'
    last_prev  = prev_months[-1]
    yr, mo     = int(last_prev[:4]), int(last_prev[5:7])
    last_day   = calendar.monthrange(yr, mo)[1]
    hist_until = f'{last_prev}-{last_day:02d}'

    meta_hist_raw = fetch_meta_daily(meta_token, hist_since, hist_until)
    if meta_hist_raw:
        for d in meta_hist_raw:
            m = d['date_start'][:7]
            if m not in meta_by_month:
                meta_by_month[m] = {'spend': 0.0, 'purchases': 0}
            meta_by_month[m]['spend']     += float(d.get('spend', 0))
            meta_by_month[m]['purchases'] += next((int(a['value']) for a in d.get('actions',[]) if a['action_type']=='purchase'), 0)

ads_data = fetch_meta_ads(meta_token, month_start_str, today_str) if meta_ok else None

# ── KPIs mês vigente ──────────────────────────────────────────────────────────
curr_days  = sorted(set(list(hm_curr_day.keys()) + list(meta_curr_day.keys())))
dias_fmt   = [datetime.strptime(d,'%Y-%m-%d').strftime('%d/%m') for d in curr_days]

hm_liq_series = [round(hm_curr_day.get(d,{}).get('liq',0), 0)    for d in curr_days]
gasto_series  = [round(meta_curr_day.get(d,{}).get('spend',0), 0) for d in curr_days]
lucro_series  = [round(hm_curr_day.get(d,{}).get('liq',0) - meta_curr_day.get(d,{}).get('spend',0), 0) for d in curr_days]
vendas_series = [hm_curr_day.get(d,{}).get('v',0) for d in curr_days]

# ── Semanas ──────────────────────────────────────────────────────────────────
def week_of_month(day_str):
    return (datetime.strptime(day_str, '%Y-%m-%d').day - 1) // 7 + 1

today_wk = week_of_month(today_str) if curr_days else 1
weekly_data = defaultdict(lambda: {'v':0,'bruto':0.0,'liq':0.0,'spend':0.0,'days':[]})
for _d in curr_days:
    _wk = week_of_month(_d)
    _h = hm_curr_day.get(_d, {}); _m = meta_curr_day.get(_d, {})
    weekly_data[_wk]['v']     += _h.get('v', 0)
    weekly_data[_wk]['bruto'] += _h.get('bruto', 0.0)
    weekly_data[_wk]['liq']   += _h.get('liq', 0.0)
    weekly_data[_wk]['spend'] += _m.get('spend', 0.0)
    weekly_data[_wk]['days'].append(_d)

weekly_labels_js = json.dumps([f'Sem {wk}' for wk in sorted(weekly_data.keys())])
weekly_lucro_js  = json.dumps([round(weekly_data[wk]['liq'] - weekly_data[wk]['spend'], 0)
                                for wk in sorted(weekly_data.keys())])

tv     = sum(h['v']     for h in hm_curr_day.values())
tb     = sum(h['bruto'] for h in hm_curr_day.values())
tl     = sum(h['liq']   for h in hm_curr_day.values())
ts     = sum(m['spend'] for m in meta_curr_day.values())
tlr    = tl - ts
roas   = tb/ts if ts>0 else 0
ticket = tb/tv if tv>0 else 0

# ── Helpers HTML ──────────────────────────────────────────────────────────────
def brl(v): return f'R${v:,.0f}'.replace(',','X').replace('.', ',').replace('X','.')
def pct_roas(v): return f'{v:.2f}x'

MESES_PT = {1:'Jan',2:'Fev',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
            7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}
def fmt_month(m_key):
    yr, mo = int(m_key[:4]), int(m_key[5:7])
    return f'{MESES_PT[mo]}/{yr}'

# ── Criativos ────────────────────────────────────────────────────────────────
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

# ── Tabela dia a dia (mês vigente) ────────────────────────────────────────────
daily_rows = ''
for i, d in enumerate(curr_days):
    h = hm_curr_day.get(d, {}); m = meta_curr_day.get(d, {})
    liq=h.get('liq',0); bruto=h.get('bruto',0); gasto=m.get('spend',0)
    lucro=liq-gasto; roas_d=bruto/gasto if gasto>0 else 0
    lc = 'text-green-600' if lucro>=0 else 'text-red-500'
    rc = 'text-green-600' if roas_d>=1.5 else 'text-amber-600' if roas_d>=1.0 else 'text-slate-400'
    daily_rows += f'''<tr class="border-b border-slate-100 hover:bg-slate-50">
      <td class="py-3 px-4 font-semibold text-slate-700">{dias_fmt[i]}</td>
      <td class="py-3 px-4 text-right text-slate-600">{h.get("v",0)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(bruto)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(liq)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(gasto) if gasto else "—"}</td>
      <td class="py-3 px-4 text-right font-bold {lc}">{("+" if lucro>=0 else "")}{brl(lucro)}</td>
      <td class="py-3 px-4 text-right font-semibold {rc}">{pct_roas(roas_d) if gasto>0 else "—"}</td>
    </tr>'''

# ── Tabela fechamento mensal ───────────────────────────────────────────────────
monthly_rows = ''
total_hv=total_hb=total_hl=total_ms=total_ml = 0
for m in reversed(prev_months):
    hm_m   = hm_by_month.get(m, {})
    meta_m = meta_by_month.get(m, {})
    tv_m   = hm_m.get('v', 0)
    tb_m   = hm_m.get('bruto', 0.0)
    tl_m   = hm_m.get('liq', 0.0)
    ts_m   = meta_m.get('spend', 0.0)
    tlr_m  = tl_m - ts_m
    roas_m = tb_m/ts_m if ts_m>0 else 0
    lc = 'text-green-600' if tlr_m>=0 else 'text-red-500'
    rc = 'text-green-600' if roas_m>=1.5 else 'text-amber-600' if roas_m>=1.0 else 'text-slate-400'
    total_hv+=tv_m; total_hb+=tb_m; total_hl+=tl_m; total_ms+=ts_m; total_ml+=tlr_m
    monthly_rows += f'''<tr class="border-b border-slate-100 hover:bg-slate-50 transition-colors">
      <td class="py-3 px-4 font-semibold text-slate-700">{fmt_month(m)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{tv_m}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(tb_m)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(tl_m)}</td>
      <td class="py-3 px-4 text-right text-slate-600">{brl(ts_m) if ts_m else "—"}</td>
      <td class="py-3 px-4 text-right font-bold {lc}">{("+" if tlr_m>=0 else "")}{brl(tlr_m)}</td>
      <td class="py-3 px-4 text-right font-semibold {rc}">{pct_roas(roas_m) if ts_m>0 else "—"}</td>
    </tr>'''

total_roas_hist = total_hb/total_ms if total_ms>0 else 0
total_lc = 'text-green-600' if total_ml>=0 else 'text-red-500'

# ── Strings de contexto ───────────────────────────────────────────────────────
periodo_vigente = f'{month_start.strftime("%d/%m")} – {today.strftime("%d/%m/%Y")}'
atualizado      = now.strftime('%d/%m/%Y às %H:%M (BRT)')
aviso_meta = '' if meta_ok else '<div class="bg-amber-50 border border-amber-200 rounded-2xl px-5 py-3 flex items-center gap-3 mb-5 text-amber-800 text-sm"><span>⚠</span> Dados Meta indisponíveis — token expirado. Atualize META_ACCESS_TOKEN no GitHub Secrets.</div>'

criativo_section = ''
if ads_data:
    criativo_section = f'''<div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100"><h2 class="text-lg font-bold text-slate-800">Criativos — {periodo_vigente}</h2><p class="text-sm text-slate-400 mt-0.5">Mês vigente · ordenado por investimento · ROAS = atribuição pixel Meta</p></div>
    <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide"><th class="text-left py-3 px-4">Criativo</th><th class="text-center py-3 px-4">Status</th><th class="text-right py-3 px-4">Gasto</th><th class="text-right py-3 px-4">Impressões</th><th class="text-right py-3 px-4">CTR</th><th class="text-right py-3 px-4">CPM</th><th class="text-right py-3 px-4">Compras</th><th class="text-right py-3 px-4">ROAS</th><th class="text-right py-3 px-4">CPA</th></tr></thead><tbody>{ads_rows}</tbody></table></div>
    <div class="px-6 py-3 bg-slate-50 border-t text-xs text-slate-400">ROAS real mês vigente (Hotmart bruto / Meta gasto): <strong class="text-slate-600">{roas:.2f}x</strong></div>
    </div>'''

monthly_section = ''
if monthly_rows:
    total_rc = 'text-green-600' if total_roas_hist>=1.5 else 'text-amber-600' if total_roas_hist>=1.0 else 'text-slate-400'
    monthly_section = f'''<div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100">
      <h2 class="text-lg font-bold text-slate-800">Fechamento Mensal</h2>
      <p class="text-sm text-slate-400 mt-0.5">Resultado consolidado de cada mês anterior</p>
    </div>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide">
        <th class="text-left py-3 px-4">Mês</th>
        <th class="text-right py-3 px-4">Vendas</th>
        <th class="text-right py-3 px-4">Bruto</th>
        <th class="text-right py-3 px-4">Líq. HM</th>
        <th class="text-right py-3 px-4">Gasto Meta</th>
        <th class="text-right py-3 px-4">Lucro</th>
        <th class="text-right py-3 px-4">ROAS</th>
      </tr></thead>
      <tbody>{monthly_rows}</tbody>
      <tfoot><tr class="bg-slate-50 font-bold border-t-2 border-slate-200">
        <td class="py-3 px-4 text-slate-800">TOTAL HIST.</td>
        <td class="py-3 px-4 text-right text-slate-800">{total_hv}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(total_hb)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(total_hl)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(total_ms) if total_ms else "—"}</td>
        <td class="py-3 px-4 text-right {total_lc}">{("+" if total_ml>=0 else "")}{brl(total_ml)}</td>
        <td class="py-3 px-4 text-right {total_rc}">{pct_roas(total_roas_hist) if total_ms>0 else "—"}</td>
      </tr></tfoot>
    </table></div>
    </div>'''

# ── Seção semanal ─────────────────────────────────────────────────────────────
weekly_rows = ''
for _wk in sorted(weekly_data.keys()):
    _wd = weekly_data[_wk]
    _days = sorted(_wd['days'])
    _period = (datetime.strptime(_days[0], '%Y-%m-%d').strftime('%d/%m')
               + ' – ' + datetime.strptime(_days[-1], '%Y-%m-%d').strftime('%d/%m'))
    _lucro_w = _wd['liq'] - _wd['spend']
    _roas_w  = _wd['bruto'] / _wd['spend'] if _wd['spend'] > 0 else 0
    _lc  = 'text-green-600' if _lucro_w >= 0 else 'text-red-500'
    _rc  = 'text-green-600' if _roas_w >= 1.5 else 'text-amber-600' if _roas_w >= 1.0 else 'text-slate-400'
    _badge = (' <span class="text-[10px] bg-blue-100 text-blue-600 rounded-full px-1.5 py-0.5 ml-1 font-semibold">atual</span>'
              if _wk == today_wk else '')
    _row_bg    = ' bg-blue-50/40' if _wk == today_wk else ''
    _sign      = '+' if _lucro_w >= 0 else ''
    _roas_str  = pct_roas(_roas_w) if _wd['spend'] > 0 else '—'
    _meta_str  = brl(_wd['spend']) if _wd['spend'] else '—'
    weekly_rows += (
        f'<tr class="border-b border-slate-100 hover:bg-slate-50{_row_bg}">'
        f'<td class="py-3 px-4 font-semibold text-slate-700">Semana {_wk}{_badge}</td>'
        f'<td class="py-3 px-4 text-slate-400 text-xs">{_period}</td>'
        f'<td class="py-3 px-4 text-right text-slate-600">{_wd["v"]}</td>'
        f'<td class="py-3 px-4 text-right text-slate-600">{brl(_wd["bruto"])}</td>'
        f'<td class="py-3 px-4 text-right text-slate-600">{brl(_wd["liq"])}</td>'
        f'<td class="py-3 px-4 text-right text-slate-600">{_meta_str}</td>'
        f'<td class="py-3 px-4 text-right font-bold {_lc}">{_sign}{brl(_lucro_w)}</td>'
        f'<td class="py-3 px-4 text-right font-semibold {_rc}">{_roas_str}</td>'
        f'</tr>'
    )

weekly_section = f'''<div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100">
      <h2 class="text-lg font-bold text-slate-800">Desempenho Semanal</h2>
      <p class="text-sm text-slate-400 mt-0.5">Mês vigente · semanas de 7 dias a partir do dia 01</p>
    </div>
    <div class="px-5 pt-5 pb-1" style="height:130px"><canvas id="cSemanal"></canvas></div>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide">
        <th class="text-left py-3 px-4">Semana</th>
        <th class="text-left py-3 px-4">Período</th>
        <th class="text-right py-3 px-4">Vendas</th>
        <th class="text-right py-3 px-4">Bruto</th>
        <th class="text-right py-3 px-4">Líq. HM</th>
        <th class="text-right py-3 px-4">Gasto Meta</th>
        <th class="text-right py-3 px-4">Lucro</th>
        <th class="text-right py-3 px-4">ROAS</th>
      </tr></thead>
      <tbody>{weekly_rows}</tbody>
    </table></div>
    </div>'''

# ── Botão atualizar (chama Make.com webhook → Make.com chama GitHub Actions) ───
_js = (
    'function triggerUpdate(){'
    'var b=document.getElementById("btnAtualizar");'
    'b.disabled=true;b.innerHTML="⏳ Iniciando...";'
    'fetch("https://hook.us2.make.com/xec8oo29t9nftow0p3qnkcr4aslfnhhr",'
    '{"method":"POST","headers":{"Content-Type":"application/json"},'
    '"body":JSON.stringify({"trigger":"dashboard"})})'
    '.then(function(){'
    'b.innerHTML="✓ Iniciado! Aguarde ~1 min";'
    'b.style.background="rgba(22,163,74,.5)";'
    'setTimeout(function(){b.innerHTML="↻ Atualizar agora";b.style.background="";b.disabled=false;},90000);'
    '}).catch(function(){'
    'b.innerHTML="✗ Erro de conexão";b.disabled=false;});}'
)
update_btn = (
    '<button id="btnAtualizar" onclick="triggerUpdate()" '
    'style="margin-top:8px;display:inline-flex;align-items:center;gap:6px;'
    'background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.25);'
    'border-radius:999px;padding:4px 12px;font-size:12px;font-weight:600;'
    'color:white;cursor:pointer;transition:background .2s">'
    '↻ Atualizar agora</button>'
    '<script>' + _js + '</script>'
)

# ── HTML ──────────────────────────────────────────────────────────────────────
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
          <div>
            <h1 class="text-xl font-black tracking-tight">Acelera Shopee</h1>
            <p class="text-blue-300 text-[11px]">Mês vigente: {periodo_vigente}</p>
          </div>
        </div>
      </div>
      <div class="text-right flex flex-col items-end gap-1">
        <div class="inline-flex items-center gap-1.5 bg-white/10 rounded-full px-3 py-1 text-xs text-blue-200">
          <span class="w-1.5 h-1.5 bg-green-400 rounded-full inline-block"></span>
          Atualizado {atualizado}
        </div>
        {update_btn}
      </div>
    </div>
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2.5">
      <div class="bg-white/10 rounded-2xl p-3.5 border border-white/20">
        <p class="text-blue-200 text-[10px] font-semibold uppercase tracking-wide mb-1">Vendas</p>
        <p class="text-3xl font-black">{tv}</p>
        <p class="text-blue-300 text-xs mt-0.5">mês atual</p>
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
        <p class="{'text-green-300' if tlr>=0 else 'text-red-300'} text-xs mt-0.5">{brl(tlr/len(curr_days)) if curr_days else "—"}/dia</p>
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

  <!-- Gráficos mês vigente -->
  <div class="card p-5">
    <div class="flex items-center justify-between mb-5 flex-wrap gap-2">
      <div><h2 class="font-bold text-slate-800">Desempenho Diário — Mês Vigente</h2><p class="text-xs text-slate-400 mt-0.5">Receita Líq. HM vs Gasto Meta vs Lucro</p></div>
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
      <p class="text-xs text-slate-400 mb-4">Compras no Hotmart — mês vigente</p>
      <div style="height:170px"><canvas id="cVendas"></canvas></div>
    </div>
    <div class="card p-5">
      <h2 class="font-bold text-slate-800 mb-0.5">Lucro Acumulado</h2>
      <p class="text-xs text-slate-400 mb-4">Progressão no mês vigente</p>
      <div style="height:170px"><canvas id="cAcum"></canvas></div>
    </div>
  </div>

  {weekly_section}

  {criativo_section}

  <!-- Tabela dia a dia mês vigente -->
  <div class="card overflow-hidden">
    <div class="px-6 py-5 border-b border-slate-100"><h2 class="text-lg font-bold text-slate-800">Dia a Dia — Mês Vigente</h2></div>
    <div class="overflow-x-auto"><table class="w-full text-sm">
      <thead><tr class="bg-slate-50 text-slate-500 text-xs uppercase tracking-wide">
        <th class="text-left py-3 px-4">Dia</th><th class="text-right py-3 px-4">Vendas</th>
        <th class="text-right py-3 px-4">Bruto</th><th class="text-right py-3 px-4">Líq. HM</th>
        <th class="text-right py-3 px-4">Gasto Meta</th><th class="text-right py-3 px-4">Lucro</th>
        <th class="text-right py-3 px-4">ROAS</th>
      </tr></thead>
      <tbody>{daily_rows}</tbody>
      <tfoot><tr class="bg-slate-50 font-bold border-t-2 border-slate-200">
        <td class="py-3 px-4 text-slate-800">TOTAL MÊS</td>
        <td class="py-3 px-4 text-right text-slate-800">{tv}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(tb)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(tl)}</td>
        <td class="py-3 px-4 text-right text-slate-800">{brl(ts) if ts else "—"}</td>
        <td class="py-3 px-4 text-right text-green-600">{"+" if tlr>=0 else ""}{brl(tlr)}</td>
        <td class="py-3 px-4 text-right text-green-600">{pct_roas(roas) if roas else "—"}</td>
      </tr></tfoot>
    </table></div>
  </div>

  {monthly_section}

  <p class="text-center text-xs text-slate-400 pb-4">Atualizado automaticamente todo dia à meia-noite · {atualizado}</p>
</div>
__SCRIPT__
</body>
</html>'''

script_block = '<script>\n'
script_block += 'var L   = ' + json.dumps(dias_fmt) + ';\n'
script_block += 'var liq = ' + json.dumps(hm_liq_series) + ';\n'
script_block += 'var g   = ' + json.dumps(gasto_series) + ';\n'
script_block += 'var lu  = ' + json.dumps(lucro_series) + ';\n'
script_block += 'var v   = ' + json.dumps(vendas_series) + ';\n'
script_block += 'var wL  = ' + weekly_labels_js + ';\n'
script_block += 'var wLu = ' + weekly_lucro_js + ';\n'
script_block += r"""
var ac = lu.reduce(function(a,x,i){ a.push((a[i-1]||0)+x); return a; }, []);
var pt = function(x){ return 'R$'+x.toLocaleString('pt-BR',{maximumFractionDigits:0}); };

new Chart(document.getElementById('cDiario'), {
  data: { labels: L, datasets: [
    {type:'line', label:'Rec. Líq.',  data:liq, borderColor:'#3b82f6', backgroundColor:'rgba(59,130,246,.08)', fill:true, tension:.4, pointRadius:3, borderWidth:2.5},
    {type:'line', label:'Gasto Meta', data:g,   borderColor:'#f87171', backgroundColor:'rgba(248,113,113,.08)', fill:true, tension:.4, pointRadius:3, borderWidth:2.5},
    {type:'bar',  label:'Lucro',      data:lu,  backgroundColor:lu.map(function(x){return x>=0?'rgba(16,185,129,.75)':'rgba(239,68,68,.7)';}), borderRadius:4}
  ]},
  options: { responsive:true, maintainAspectRatio:false, interaction:{mode:'index',intersect:false},
    plugins:{ legend:{display:false}, tooltip:{callbacks:{label:function(c){return ' '+c.dataset.label+': '+pt(c.parsed.y);}}} },
    scales:{ x:{grid:{display:false}}, y:{grid:{color:'#f1f5f9'},ticks:{callback:pt}} }
  }
});

new Chart(document.getElementById('cVendas'), {
  type: 'bar',
  data: { labels: L, datasets: [{data:v, backgroundColor:'#bfdbfe', borderColor:'#3b82f6', borderWidth:1.5, borderRadius:5}] },
  options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}},
    scales:{ x:{grid:{display:false}}, y:{ticks:{stepSize:2}} }
  }
});

new Chart(document.getElementById('cAcum'), {
  type: 'line',
  data: { labels: L, datasets: [{data:ac, borderColor:'#10b981', backgroundColor:'rgba(16,185,129,.1)', fill:true, tension:.4, pointRadius:3, borderWidth:2.5}] },
  options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}},
    scales:{ x:{grid:{display:false}}, y:{ticks:{callback:pt}} }
  }
});

if (document.getElementById('cSemanal')) {
  new Chart(document.getElementById('cSemanal'), {
    type: 'bar',
    data: { labels: wL, datasets: [{
      data: wLu,
      backgroundColor: wLu.map(function(x){ return x>=0?'rgba(16,185,129,.75)':'rgba(239,68,68,.7)'; }),
      borderRadius: 6
    }]},
    options: { responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}},
      scales:{ x:{grid:{display:false}}, y:{ticks:{callback:pt}} }
    }
  });
}
</script>"""

html = html.replace('__SCRIPT__', script_block)

with open('index.html', 'w', encoding='utf-8') as f:
    f.write(html)

status = 'Meta + Hotmart' if meta_ok else 'só Hotmart (token Meta expirado)'
print(f'index.html gerado — {status}')
