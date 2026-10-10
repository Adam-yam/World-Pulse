import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
import difflib
import unicodedata
import time
from zoneinfo import ZoneInfo
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
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat()

def child_text(item, *names):
    for name in names:
        for child in item:
            if child.tag.split('}')[-1] == name:
                return ''.join(child.itertext()).strip()
    return ''


def is_breaking(title):
    return bool(re.match(r'^\s*(?:[\[【(（〈《]\s*(?:속보|速報|快訊|快讯|快報)\s*[\]】)）〉》]|(?:속보|速報|快訊|快讯|快報)\s*[:：])', title))

ZONES = {'KR': 'Asia/Seoul', 'JP': 'Asia/Tokyo', 'TW': 'Asia/Taipei'}

def timestamp(item):
    try:
        value = dt.datetime.fromisoformat(item.get('publishedAt') or '')
        return value if value.tzinfo else None
    except (ValueError, TypeError):
        return None

def breaking_items(items, code='KR', current=None):
    current = current or dt.datetime.now(dt.timezone.utc)
    local = current.astimezone(ZoneInfo(ZONES[code]))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=1)
    candidates = [dict(item, breaking=True) for item in items
                  if is_breaking(item.get('title', '')) and timestamp(item) is not None
                  and start <= timestamp(item) <= current]
    candidates.sort(key=lambda x: timestamp(x), reverse=True)
    return unique(candidates, limit=30)

def archive_articles(old, fresh, current):
    merged = {}
    for item in old + fresh:
        published = timestamp(item)
        if published is not None and current - dt.timedelta(days=3) <= published <= current:
            merged[canonical_url(item['url'])] = item
    return sorted(merged.values(), key=timestamp, reverse=True)[:3000]


def parse_feed(raw, source, trend=False):
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Unsupported XML declarations')
    root = ET.fromstring(raw)
    result = []
    for item in [node for node in root.iter() if node.tag.split('}')[-1] in ('item', 'entry')][:500]:
        title = clean(child_text(item, 'title'))
        link = child_text(item, 'link')
        if not link:
            link = next((x.get('href', '') for x in item if x.tag.split('}')[-1] == 'link' and x.get('rel', 'alternate') == 'alternate'), '')
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
            publisher = clean(child_text(item, 'source')) or source
            if publisher and title.endswith(' - ' + publisher):
                title = title[:-(len(publisher) + 3)]
            result.append({'id': hashlib.sha256(link.encode()).hexdigest()[:16], 'title': title,
                           'url': link, 'source': publisher, 'publishedAt': date(child_text(item, 'pubDate', 'date', 'published', 'updated')),
                           'breaking': is_breaking(title)})
    if not result:
        raise ValueError('No valid items in RSS feed')
    return result

def fetch(url, name, trend=False):
    request = urllib.request.Request(url, headers={'User-Agent': 'WorldPulse/0.1 RSS Reader', 'Accept': 'application/rss+xml, application/xml, text/xml'})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError('RSS response too large')
            return parse_feed(raw, name, trend)
        except (OSError, ET.ParseError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def title_key(title):
    title = unicodedata.normalize('NFKC', title)
    title = re.sub(r'[\[【][^\]】]*[\]】]', '', title)
    title = re.sub(r'\([^)]*(?:종합|상보|영상|사진|보완)[^)]*\)', '', title)
    return re.sub(r'[^\w]', '', title).casefold()

def similar_title(a, b):
    if a == b:
        return True
    if min(len(a), len(b)) < 14:
        return False
    if difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= .79:
        return True
    sa, sb = {a[i:i+3] for i in range(len(a)-2)}, {b[i:i+3] for i in range(len(b)-2)}
    return bool(sa and sb) and len(sa & sb) / len(sa | sb) >= .56

def canonical_url(url):
    parsed = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid', 'outputtype', 'ref')]
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip('/'), urllib.parse.urlencode(sorted(query)), ''))

def unique(items, limit=100):
    seen_urls, keys, result = set(), [], []
    for item in items:
        key = title_key(item['title'])
        url_key = canonical_url(item['url'])
        if url_key in seen_urls or any(similar_title(key, previous) for previous in keys):
            continue
        seen_urls.add(url_key)
        keys.append(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result

def balanced_articles(snapshots, current=None):
    current = current or dt.datetime.now(dt.timezone.utc)
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(hours=1)
    groups = []
    for snapshot in snapshots.values():
        group = []
        for article in snapshot.get('articles', []):
            try:
                published = dt.datetime.fromisoformat(article.get('publishedAt') or '')
                if start <= published < end and published <= current:
                    group.append(article)
            except (ValueError, TypeError):
                continue
        groups.append(group)
    interleaved = [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]
    return unique(interleaved)


def collect():
    run_time = dt.datetime.now(dt.timezone.utc)
    config = json.loads((DATA / 'sources.json').read_text())
    try:
        data = json.loads((DATA / 'news.json').read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {'schemaVersion': 1, 'countries': {}}
    futures = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for code, cfg in config.items():
            futures[code] = ([pool.submit(fetch, s['url'], s.get('publisher', s['name'])) for s in cfg['news']],
                             pool.submit(fetch, cfg['trends'], 'Google Trends', True))
        for code, (news_futures, trend_future) in futures.items():
            previous = data['countries'].get(code, {})
            state = dict(previous)
            state['attemptedAt'] = now()
            errors, articles = [], []
            old_sources = previous.get('sourceSnapshots', {})
            snapshots = {src['name']: old_sources[src['name']] for src in config[code]['news'] if src['name'] in old_sources}
            successes = []
            for src, future in zip(config[code]['news'], news_futures):
                try:
                    current = future.result()
                    stamp = now()
                    retained = snapshots.get(src['name'], {}).get('articles', [])
                    snapshots[src['name']] = {'updatedAt': stamp, 'articles': archive_articles(retained, current, run_time)}
                    successes.append(stamp)
                except Exception as exc:
                    errors.append(src['name'] + ': ' + type(exc).__name__)
                articles.extend(snapshots.get(src['name'], {}).get('articles', []))
            state['sourceSnapshots'] = snapshots
            if successes:
                state['articles'] = balanced_articles(snapshots, run_time)
                end = run_time.replace(minute=0, second=0, microsecond=0)
                state['newsWindowStart'] = (end - dt.timedelta(hours=1)).isoformat()
                state['newsWindowEnd'] = end.isoformat()
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
    write_data(data, run_time)
    return data

def write_data(data, current=None):
    current = current or dt.datetime.now(dt.timezone.utc)
    for code, state in data.get('countries', {}).items():
        candidates = [a for source in state.get('sourceSnapshots', {}).values() for a in source.get('articles', [])]
        candidates.extend(state.get('articles', []))
        candidates.extend(state.get('breakingArticles', []))
        state['breakingArticles'] = breaking_items(candidates, code, current)
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    target = DATA / 'news.json'
    temp = target.with_suffix('.tmp')
    temp.write_text(text, encoding='utf-8')
    temp.replace(target)
    (DATA / 'snapshot.js').write_text('window.NEWS_DATA=' + text.replace('<', '\\u003c') + ';\n', encoding='utf-8')

if __name__ == '__main__':
    collect()
