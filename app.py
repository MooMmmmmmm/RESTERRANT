"""Private, read-only MCP demo. Listen on loopback; never return credentials."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json, urllib.request, tomllib, copy, random, threading, mimetypes, re, os, argparse, getpass, itertools
from decimal import InvalidOperation

ROOT = Path(__file__).parent
PORT = 8770
SEED = json.loads((ROOT / 'data/menu-seed.json').read_text(encoding='utf-8'))
SOURCES = {x['id']: x['local'] for x in json.loads((ROOT / 'assets/sources.json').read_text(encoding='utf-8'))}
LOCK = threading.Lock()
STATE = {'catalog': {}, 'coupons': {}, 'time': ''}

class MCPError(Exception):
    pass

def ensure_assets(ids):
    sources = json.loads((ROOT / 'assets/sources.json').read_text(encoding='utf-8'))
    for source in sources:
        if source['id'] not in ids:
            continue
        path = ROOT / 'assets' / source['local']
        if path.exists():
            continue
        url = source.get('url') or source.get('image') or source.get('source')
        if not url or not url.startswith('https://menu-img.mcd.cn/'):
            raise MCPError('素材来源地址不符')
        with urllib.request.urlopen(url,timeout=25) as response:
            path.write_bytes(response.read())

class MCP:
    def __init__(self, use_codex_config=False):
        token = os.environ.get('MCD_MCP_TOKEN', '').strip()
        if use_codex_config:
            config = tomllib.loads((Path.home() / '.codex/config.toml').read_text(encoding='utf-8-sig'))['mcp_servers']['mcd-mcp']
            if config.get('url') != 'https://mcp.mcd.cn':
                raise MCPError('本地配置的服务地址不符')
            token = config.get('http_headers', {}).get('Authorization', '').removeprefix('Bearer ').strip()
        if not token:
            token = getpass.getpass('请输入个人麦当劳 MCP Token（不显示、不保存）: ').strip()
        if not token or '\n' in token or '\r' in token:
            raise MCPError('Token 未填写或格式无效')
        self.headers = {'Authorization':'Bearer '+token}
        self.headers.update({'Content-Type':'application/json', 'Accept':'application/json, text/event-stream'})
        self.sequence = 0
        response, session = self.rpc('initialize', {'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'private-meal-demo','version':'1.0'}}, initialize=True)
        if session:
            self.headers['Mcp-Session-Id'] = session
        self.headers['MCP-Protocol-Version'] = response['protocolVersion']
        self.rpc('notifications/initialized', notification=True)

    def rpc(self, method, params=None, notification=False, initialize=False):
        self.sequence += 1
        payload = {'jsonrpc':'2.0','method':method}
        if params is not None:
            payload['params'] = params
        if not notification:
            payload['id'] = self.sequence
        req = urllib.request.Request('https://mcp.mcd.cn', data=json.dumps(payload).encode(), headers=self.headers, method='POST')
        with urllib.request.urlopen(req, timeout=25) as response:
            raw = response.read().decode('utf-8')
            session = response.headers.get('Mcp-Session-Id')
        if notification:
            return None
        if raw.lstrip().startswith('{'):
            message = json.loads(raw)
        else:
            events = [line[5:].strip() for line in raw.splitlines() if line.startswith('data:')]
            message = next(json.loads(line) for line in reversed(events) if line.startswith('{'))
        if message.get('error'):
            raise MCPError('MCP 请求失败')
        return (message['result'], session) if initialize else message['result']

    def call(self, name, args):
        if name not in {'query-meals','query-store-coupons','query-meal-detail','calculate-price'}:
            raise MCPError('该功能未获授权')
        result = self.rpc('tools/call', {'name':name,'arguments':args})
        structured = result.get('structuredContent')
        if structured is None:
            for block in result.get('content', []):
                text = block.get('text', '')
                match = re.search(r'\{"success"\s*:', text)
                if match:
                    structured = json.JSONDecoder().raw_decode(text[match.start():])[0]
                    break
        if not structured or not structured.get('success'):
            raise MCPError('当前餐品或价格暂时无法核验，请刷新后重试')
        return structured

CLIENT = None
ARGS = {'beType':1,'orderType':1}
DETAILS = {}

def default_modification(data):
    values = []
    if data.get('supportModify') and not (data.get('modification') or {}).get('items'):
        raise MCPError('餐品默认特调资料不完整')
    for group in (data.get('modification') or {}).get('items', []):
        for option in group.get('values', []):
            quantity = option.get('selectedQuantity')
            if type(quantity) is not int or quantity < 0:
                raise MCPError('默认特调数量缺失或无效')
            key = option.get('selectedKey') if quantity else option.get('unselectedKey')
            if quantity and not key:
                raise MCPError('默认特调选项不完整')
            if key:
                values.append({'code':option['code'],'key':key,'quantity':quantity if quantity else 1})
    return {'values':values} if values else None

def ensure_item(code):
    food = STATE['catalog'][code]
    if 'item' in food:
        return food['item']
    key = (ARGS.get('storeCode'), code)
    detail = DETAILS.get(key)
    if detail is None:
        detail = CLIENT.call('query-meal-detail', {**ARGS,'code':code})['data']
    if str(detail.get('code')) != code:
        raise MCPError('详情未匹配所选食品')
    if detail.get('supportModify') and not (detail.get('modification') or {}).get('items'):
        raise MCPError('餐品默认特调资料不完整')
    item = {'productCode':code,'quantity':1}
    modification = default_modification(detail)
    if modification:
        item['modification'] = modification
    contents, rounds = [], []
    for row in detail.get('rounds', []):
        choices = []
        for choice in row.get('choices', []):
            if choice.get('isDefault') != 1:
                continue
            quantity = choice.get('quantity')
            if type(quantity) is not int or quantity < 1:
                raise MCPError('套餐默认数量不明确，请另选餐品')
            part = {'code':choice['code'],'quantity':quantity}
            modification = default_modification(choice)
            if modification:
                part['modification'] = modification
            choices.append(part)
            contents.extend([choice['name']]*quantity)
        count = sum(part['quantity'] for part in choices)
        minimum,maximum = row.get('minQuantity'),row.get('maxQuantity')
        if type(minimum) is not int or type(maximum) is not int or not minimum <= count <= maximum:
            raise MCPError('套餐默认组成不完整，请另选餐品')
        if choices:
            rounds.append({'round':str(row['id']),'comboItemList':choices})
    if rounds:
        item['roundList'] = rounds
    food.update(item=item,contents=contents,supportModify=bool(detail.get('supportModify')),
                foodNames=contents or [food['name']])
    DETAILS[key] = copy.deepcopy(detail)
    return item

def is_single_food(food):
    name = food.get('name', '')
    category = food.get('category', '')
    return not (re.search(r'套餐|[一二三四五六七八九十0-9]+件套|多人餐|双人|分享餐|双全盒|四宫格|双拼|随心配|随心拼|任选|随心选|开心乐园餐|单品精选|冰爽一夏|徽章|贴|捶痒痒挠', name)
                or (re.search(r'单人餐|多人餐|套餐|随心配|开心乐园', category) and not re.search(r'\d+块$', name))
                or len(food.get('contents', [])) > 1)

def refresh():
    menu = CLIENT.call('query-meals', ARGS)
    coupons = CLIENT.call('query-store-coupons', ARGS)
    current = menu['data']['meals']
    now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    available_coupons = {}
    for coupon in coupons.get('data', []):
        dates = re.findall(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', coupon.get('tradeDateTime',''))
        if len(dates) == 2 and not datetime.fromisoformat(dates[0]) <= now <= datetime.fromisoformat(dates[1]):
            continue
        for product in coupon.get('products', []):
            available_coupons.setdefault(product['productCode'], []).append(coupon)
    catalog = {}
    seeds = {food['id']:food for food in SEED['catalog']}
    categories = {}
    for category in menu['data'].get('categories', []):
        for meal in category.get('meals', []):
            categories.setdefault(str(meal['code']), category['name'])
    for code, meal in current.items():
        food = copy.deepcopy(seeds.get(code, {}))
        def amount(field):
            value = meal.get(field)
            return int(Decimal(str(value))*100) if value is not None else None
        category = food.get('category') or categories.get(code, '其他')
        image = meal.get('image', '')
        if not image.startswith('https://menu-img.mcd.cn/'):
            image = ''
        food.update(id=code,name=meal['name'],category=category,
                    price=amount('currentPrice'),original=amount('originalPrice'),
                    image='/assets/'+SOURCES[code] if code in SOURCES else image,
                    selectable=True,
                    availabilityLabel='有随单购选项；本版按不购卡核价' if meal.get('canWithOrder') else '',
                    couponOnly=False,supportModify=food.get('supportModify',False),
                    contents=food.get('contents',[]),types=food.get('types',[category]))
        food['foodNames'] = food['contents'] or [food['name']]
        if is_single_food(food):
            catalog[code] = food
    for seed in SEED['catalog']:
        code = seed['id']
        if code in catalog or code not in available_coupons or not is_single_food(seed):
            continue
        food = copy.deepcopy(seed)
        food['couponOnly'] = code not in current
        food['image'] = '/assets/' + SOURCES[code]
        food['types'] = ['主食','饮品'] if food['category'] == '优惠' else [food['category']]
        food['foodNames'] = food['contents'] or [food['name']]
        catalog[code] = food
    STATE.update(catalog=catalog,coupons=available_coupons,time=menu['datetime'])
    for code, food in catalog.items():
        if food.get('couponOnly') and (not isinstance(food['price'],int) or food['price'] <= 0):
            quote = official([code])
            food['price'], food['original'] = quote['price'], quote['original']

def public_catalog():
    fields = ['id','name','category','image','price','original','supportModify','contents','types','selectable','availabilityLabel']
    return {'store':{'name':'本机所选门店'},'time':STATE['time'],'foods':[{key:food[key] for key in fields if key in food} for food in STATE['catalog'].values()]}

def official(ids):
    choices = [([] if STATE['catalog'][code].get('couponOnly') else [None])+STATE['coupons'].get(code,[]) for code in ids]
    if any(not isinstance(x,list) for x in choices) or len(list(itertools.islice(itertools.product(*choices),17))) > 16:
        raise MCPError('可用券组合超过初版核验范围，未宣称最优')
    best = None
    for coupons in itertools.product(*choices):
        items, titles = [], []
        for code, coupon in zip(ids,coupons):
            if not STATE['catalog'][code].get('selectable', True):
                raise MCPError('所选食品需额外权益，本版不购买权益')
            item = copy.deepcopy(ensure_item(code))
            if coupon:
                item.update(couponId=coupon['couponId'],couponCode=coupon['couponCode'])
                titles.append(coupon['title'])
            items.append(item)
        try:
            response = CLIENT.call('calculate-price', {**ARGS,'items':items,'needTableware':False})
        except MCPError:
            raise MCPError('优惠比较未完成，请重试；未把失败方案当成无优惠') from None
        data = response['data']
        expected = Counter({item['productCode']:item['quantity'] for item in items})
        actual = Counter()
        for product in data.get('productList', []):
            actual[product.get('productCode')] += product.get('quantity', 0)
        if actual != expected or type(data.get('price')) is not int or data['price'] < 0:
            raise MCPError('报价未完整保留餐品，优惠比较未完成')
        quoted = {'ids':ids,'price':data['price'],'original':data.get('originalPrice'),'discount':data.get('discount',0),'couponTitles':titles,'time':response['datetime'],'traceId':response.get('traceId')}
        if best is None or quoted['price'] < best['price']:
            best = quoted
    if best is None:
        raise MCPError('接口未完整保留所选餐品，拒绝报价，没有删除锁定')
    return best

def plan(body):
    refresh()
    catalog = STATE['catalog']
    if not isinstance(body, dict):
        raise ValueError('请求必须为对象')
    value = Decimal(str(body.get('budget',0)))
    if not value.is_finite() or value < 1 or value > 200 or value * 100 != (value * 100).to_integral_value():
        return {'valid':False,'reason':'预算须为1至200元，最多两位小数。',**public_catalog()}
    for field in ('mandatory','added','previous'):
        values = body.get(field, [])
        if not isinstance(values,list) or len(values)>30 or any(not isinstance(code,str) for code in values):
            raise ValueError('餐品选择格式无效')
    budget = int(value * 100)
    mandatory = list(dict.fromkeys(body.get('mandatory',[])))
    added = list(dict.fromkeys(body.get('added',[])))
    fixed = list(dict.fromkeys(mandatory + added))
    if budget < 100:
        return {'valid':False,'reason':'请输入至少1元的有效预算。',**public_catalog()}
    if any(code not in catalog or not catalog[code].get('selectable', True) for code in fixed):
        return {'valid':False,'reason':'某个必吃或锁定餐品当前已不可用。请主动修改选择，没有自动删品。',**public_catalog()}
    selectable = {code:food for code,food in catalog.items() if food.get('selectable', True)}
    if not selectable:
        return {'valid':False,'reason':'本轮没有可用餐品，请稍后重试。',**public_catalog()}
    saved = official(fixed) if fixed else official([min(selectable, key=lambda code:selectable[code]['price'] if isinstance(selectable[code]['price'],int) else float('inf'))])
    pool = [code for code in selectable if code not in fixed and isinstance(catalog[code]['price'],int) and catalog[code]['price'] > 0]
    previous = set(body.get('previous',[]))
    candidates, best_score, overflow = [], (-1,-1), []
    # Fixed effort per menu item: never enumerate all subsets of a full menu.
    for _ in range(96):
        shuffled = random.sample(pool,len(pool))
        picks, amount = [], saved['price'] if fixed else 0
        for code in shuffled:
            ids = fixed + picks + [code]
            if len(ids)>30:
                break
            total = amount + catalog[code]['price']
            if total > budget:
                overflow.append((total,ids))
                overflow.sort(key=lambda x:x[0])
                del overflow[3:]
                continue
            picks.append(code)
            amount = total
        ids = fixed + picks
        if ids and amount <= budget:
            score = (len(set().union(*(set(catalog[code]['types']) for code in ids))),len(set().union(*(set(catalog[code]['foodNames']) for code in ids))))
            if score > best_score:
                best_score,candidates = score,[ids]
            elif score == best_score and ids not in candidates:
                candidates.append(ids)
    alternatives = [ids for ids in candidates if set(ids) != previous]
    random.shuffle(alternatives)
    if not alternatives:
        alternatives = candidates
        random.shuffle(alternatives)
    valid = [saved] if saved['price'] <= budget else []
    checks = alternatives[:2] + [x[1] for x in overflow[:1]]
    if not valid:
        checks = [fixed+[code] for code in sorted(pool,key=lambda code:catalog[code]['price'])[:3]] + checks
    checked = {frozenset(saved['ids'])}
    for ids in checks:
        if frozenset(ids) in checked:
            continue
        checked.add(frozenset(ids))
        try:
            quoted = official(ids)
        except MCPError:
            continue
        if quoted['price'] <= budget:
            valid.append(quoted)
    if not valid:
        return {'valid':False,'reason':f"固定食品单独核价¥{saved['price']/100:.2f}超过预算；本轮有限候选未找到优惠后达标方案。请调整预算或主动解锁，未删除食品。",**public_catalog()}
    def rank(quote):
        return (len(set().union(*(set(catalog[code]['types']) for code in quote['ids']))),len(set().union(*(set(catalog[code]['foodNames']) for code in quote['ids']))))
    rich = max(valid,key=rank)
    return {'valid':True,'options':{'meal':rich},'scope':'菜单全量展示；本地有界候选搜索及有限官方试算，不承诺全店最优',**public_catalog()}

def confirm(body):
    if not isinstance(body,dict):
        raise ValueError('确认格式无效')
    ids=body.get('ids',[])
    if not isinstance(ids,list) or not 1<=len(ids)<=30 or any(not isinstance(code,str) for code in ids) or len(set(ids))!=len(ids):
        raise ValueError('确认餐单无效')
    budget=Decimal(str(body.get('budget',0)))
    if not budget.is_finite() or budget<1 or budget>200 or budget*100!=(budget*100).to_integral_value():
        raise ValueError('确认预算无效')
    refresh()
    if any(code not in STATE['catalog'] for code in ids):
        return {'valid':False,'reason':'餐单食品当前不可用，未删品、未下单。请主动调整。',**public_catalog()}
    quote=official(ids)
    if quote['price']>int(budget*100):
        return {'valid':False,'reason':'最新试算已超过预算，未下单，请调整。',**public_catalog()}
    return {'valid':True,'quote':quote,**public_catalog()}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.headers.get('Host') not in {f'127.0.0.1:{PORT}',f'localhost:{PORT}'}:
            self.send({'error':'仅本机可用'},403); return
        if self.path == '/api/catalog':
            with LOCK:
                self.send(public_catalog())
            return
        if self.path == '/':
            path = ROOT / 'web/index.html'
        elif self.path in {'/live-demo.js','/live-demo.css'}:
            path = ROOT / 'web' / self.path[1:]
        elif re.fullmatch(r'/assets/mcp-\d+\.(png|gif)', self.path):
            path = ROOT / self.path[1:]
            code = self.path.split('mcp-')[-1].split('.')[0]
            if code not in SOURCES or SOURCES[code] != path.name:
                self.send({'error':'not_found'},404); return
            if not path.exists():
                try:
                    with LOCK:
                        ensure_assets({code})
                except (MCPError,OSError):
                    self.send({'error':'素材暂不可用'},503); return
        else:
            self.send({'error':'not_found'},404); return
        if not path.exists():
            self.send({'error':'not_found'},404); return
        self.send_response(200)
        self.send_header('Content-Type', (mimetypes.guess_type(str(path))[0] or 'application/octet-stream') + ('; charset=utf-8' if path.suffix in {'.html','.js','.css'} else ''))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def do_POST(self):
        origin = self.headers.get('Origin')
        if self.path not in {'/api/plan','/api/confirm'} or self.headers.get('Host') not in {f'127.0.0.1:{PORT}',f'localhost:{PORT}'} or origin not in {None,f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'}:
            self.send({'error':'请求未获授权'},403); return
        if not self.headers.get('Content-Type','').startswith('application/json'):
            self.send({'error':'请求格式无效'},400); return
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get('Content-Length','0')),8192)))
            with LOCK:
                self.send(confirm(body) if self.path=='/api/confirm' else plan(body))
        except (MCPError,ValueError,KeyError,InvalidOperation,urllib.error.URLError):
            self.send({'valid':False,'reason':'本次MCP核验未通过。保留预算和锁定，请稍后重试；没有使用模拟价。','verificationFailed':True},422)


def main():
    global CLIENT, PORT
    parser = argparse.ArgumentParser(description='麦麦搭配局：本机真实MCP配餐，不下单')
    parser.add_argument('--port', type=int, default=8770)
    parser.add_argument('--use-codex-config', action='store_true', help='明确选择读取本人本地Codex麦当劳配置')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('端口须为1024至65535')
    PORT = args.port
    try:
        store = getpass.getpass('请输入门店编号（不回显、不保存）: ').strip()
        if not re.fullmatch(r'[0-9]+', store):
            raise ValueError('门店编号无效')
        ARGS['storeCode'] = store
        CLIENT = MCP(args.use_codex_config)
        refresh()
        ensure_assets(set(STATE['catalog']))
        server = ThreadingHTTPServer(('127.0.0.1',PORT), Handler)
    except (MCPError,KeyError,OSError,ValueError):
        print('启动失败：请检查个人MCP授权、网络或端口。未输出凭证。')
        return 1
    print(f'麦麦搭配局 http://127.0.0.1:{PORT}/ ; {len(STATE["catalog"])} 个当前候选 ; 仅推荐与核价',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
