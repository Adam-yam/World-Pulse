import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def clean(value):
    return html.unescape(re.sub(r'<[^>]+>', '', value or '')).strip()

def valid_url(value):
    return urllib.parse.urlsplit(value or '').scheme in ('http', 'https')

def date(value):
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None

def parse_feed(raw, source, trend=False):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Unsupported XML declarations')
    root = ET.fromstring(raw)
    result = []
    for item in root.findall('.//item')[:60]:
        title = clean(item.findtext('title'))
        link = (item.findtext('link') or '').strip()
        if not title:
            continue
        if trend and not valid_url(link):
            link = 'https://www.google.com/search?q=' + urllib.parse.quote(title)
        if not valid_url(link):
            continue
        if trend:
            traffic = next((clean(x.text) for x in item if x.tag.endswith('}approx_traffic')), '')
            result.append({'title': title, 'url': link, 'traffic': traffic})
        else:
            publisher = clean(item.findtext('source')) or source
            if publisher and title.endswith(' - ' + publisher):
                title = title[:-(len(publisher) + 3)]
            result.append({'id': hashlib.sha256(link.encode()).hexdigest()[:16], 'title': title,
                           'url': link, 'source': publisher, 'publishedAt': date(item.findtext('pubDate')),
                           'breaking': bool(re.search(r'\[속보\]|【速報】|\[速報\]|^속보\s*[:：]|^速報\s*[:：]', title))})
    if not result:
        raise ValueError('No valid items in RSS feed')
    return result

def fetch(url, name, trend=False):
    request = urllib.request.Request(url, headers={'User-Agent': 'EastPulse/0.1 RSS Reader', 'Accept': 'application/rss+xml, application/xml, text/xml'})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError('RSS response too large')
    return parse_feed(raw, name, trend)

def unique(items):
    seen_urls, seen_titles, result = set(), set(), []
    for item in items:
        key = re.sub(r'\W+', '', item['title']).casefold()
        if item['url'] in seen_urls or key in seen_titles:
            continue
        seen_urls.add(item['url'])
        seen_titles.add(key)
        result.append(item)
    return result[:40]

def collect():
    config = json.loads((DATA / 'sources.json').read_text())
    try:
        data = json.loads((DATA / 'news.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {'schemaVersion': 1, 'countries': {}}
    futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for code, cfg in config.items():
            futures[code] = ([pool.submit(fetch, s['url'], s['name']) for s in cfg['news']],
                             pool.submit(fetch, cfg['trends'], 'Google Trends', True))
        for code, (news_futures, trend_future) in futures.items():
            previous = data['countries'].get(code, {})
            state = dict(previous)
            state['attemptedAt'] = now()
            errors, articles = [], []
            old_sources = previous.get('sourceSnapshots', {})
            snapshots = dict(old_sources)
            successes = []
            for src, future in zip(config[code]['news'], news_futures):
                try:
                    current = future.result()
                    stamp = now()
                    snapshots[src['name']] = {'updatedAt': stamp, 'articles': current}
                    successes.append(stamp)
                except Exception as exc:
                    errors.append(src['name'] + ': ' + type(exc).__name__)
                articles.extend(snapshots.get(src['name'], {}).get('articles', []))
            state['sourceSnapshots'] = snapshots
            if successes:
                state['articles'] = unique(articles)
                state['newsUpdatedAt'] = max(successes)
            state['newsErrors'] = errors
            try:
                state['trends'] = trend_future.result()[:20]
                state['trendsUpdatedAt'] = now()
                state['trendErrors'] = []
            except Exception as exc:
                state['trendErrors'] = ['Google Trends: ' + type(exc).__name__]
            data['countries'][code] = state
            print(code, 'news:', len(state.get('articles', [])), 'trends:', len(state.get('trends', [])), 'errors:', len(errors) + len(state['trendErrors']))
    data['attemptedAt'] = now()
    write_data(data)
    return data

def write_data(data):
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    target = DATA / 'news.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(target)
    (DATA / 'snapshot.js').write_text('window.NEWS_DATA=' + text.replace('<', '\\u003c') + ';\n', encoding='utf-8')

if __name__ == '__main__':
    collect()
