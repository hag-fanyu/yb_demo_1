#!/usr/bin/env python3
import sys
sys.stdout.reconfigure(encoding='utf-8')
import uiautomator2 as u2
import re, time

d = u2.connect()
time.sleep(2)

print('=== Bottom tab elements ===')
for text in ['首页', '我的', '搜索', '发现', '推荐', '更多']:
    el = d(text=text)
    if el.exists(timeout=1):
        info = el.info
        print(f'  Found: text={text!r}  clickable={info.get("clickable", "?")}')

for desc in ['首页', '我的', '搜索', '发现']:
    el = d(description=desc)
    if el.exists(timeout=1):
        print(f'  Found: description={desc!r}')

for text in ['我的', '首页', '搜索']:
    el = d(textContains=text)
    if el.exists(timeout=1):
        count = el.count
        print(f'  Found: textContains={text!r}  count={count}')

print()
print('=== All visible text (first 50) ===')
xml = d.dump_hierarchy()
texts = re.findall(r'text="([^"]+)"', xml)
seen = set()
i = 0
for t in texts:
    if t.strip() and t not in seen:
        seen.add(t)
        print(f'  [{i}] {t}')
        i += 1
        if i >= 50:
            break

print()
print('=== All resource-id (non-empty, unique) ===')
resids = re.findall(r'resource-id="([^"]+)"', xml)
seen2 = set()
for r in resids:
    if r and r not in seen2:
        seen2.add(r)
        print(f'  {r}')
