import sys, json, time, hashlib
sys.path.insert(0, r'C:\Users\Administrator\IDEProjects\demo_claude')
from damai_monitor import DamaiMonitor, APP_KEY, MTOP_HOST, GETDETAIL_API

ITEM = '1061170881710'
m = DamaiMonitor(ITEM)
m._seed_token()
tok = m._get_token()
headers = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
                  'AppleWebKit/605.1.15 Mobile/15E148',
    'Referer': f'https://m.damai.cn/damai/detail/item.html?itemId={ITEM}',
}
t = str(int(time.time() * 1000))
data = json.dumps({"itemId": ITEM})
sign = hashlib.md5(f'{tok}&{t}&{APP_KEY}&{data}'.encode()).hexdigest()
params = {'jsv': '2.7.2', 'appKey': APP_KEY, 't': t, 'sign': sign,
          'api': GETDETAIL_API, 'v': '1.0', 'type': 'originaljson',
          'dataType': 'json', 'data': data}
r = m.session.get(f'{MTOP_HOST}/h5/{GETDETAIL_API}/1.0/',
                  params=params, headers=headers, timeout=10)
j = r.json()
d = j['data']
print('=== top-level keys ===')
print(list(d.keys()))

for key in ['buyButton', 'item', 'price', 'venue']:
    print(f'\n=== {key} ===')
    print(json.dumps(d.get(key), ensure_ascii=False, indent=2)[:1500])
