#!/usr/bin/env python3
"""Package only public app assets; optionally refresh recommendations from YouTube RSS."""
import argparse
import concurrent.futures
import copy
import datetime as dt
import json
import re
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}


def channel_videos(channel):
    cid = channel['id']
    if not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', cid):
        raise ValueError('Invalid channel ID')
    req = urllib.request.Request('https://www.youtube.com/feeds/videos.xml?channel_id=' + cid,
                                 headers={'User-Agent': 'FriendsEnglish/1.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        root = ET.fromstring(response.read(2_000_000))
    entries = []
    for node in root.findall('a:entry', NS):
        vid = node.findtext('yt:videoId', '', NS)
        title = node.findtext('a:title', '', NS)
        if not VIDEO_ID.fullmatch(vid) or not title or re.search(r'#shorts\b|\blive stream\b', title, re.I):
            continue
        entries.append({'yt': vid, 'title': title[:500], 'channel': channel['name'], 'channelId': cid,
                        'level': channel['level'], 'topics': channel['topics'],
                        'reason': channel['reason'], 'published': node.findtext('a:published', '', NS),
                        'source': 'feed', 'duration': None, 'subs': []})
    if not entries:
        raise ValueError('Feed has no usable videos')
    return entries[:10]


def refresh_catalog(catalog):
    result = copy.deepcopy(catalog)
    curated = [v for v in result['videos'] if v.get('source') != 'feed']
    previous = [v for v in result['videos'] if v.get('source') == 'feed']
    updates, successes = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [(c, pool.submit(channel_videos, c)) for c in result['channels']]
        for channel, future in futures:
            try:
                updates.extend(future.result())
                successes += 1
            except Exception as exc:
                print(f'Feed unavailable ({channel["name"]}): {exc}; preserving saved recommendations')
                updates.extend(v for v in previous if v.get('channelId') == channel['id'])
    seen = set()
    result['videos'] = []
    for video in curated + updates:
        if video['yt'] not in seen:
            seen.add(video['yt'])
            result['videos'].append(video)
    result['feedStatus'] = {'updated': successes, 'total': len(result['channels'])}
    if successes:
        result['updatedAt'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return result


def build(output, refresh=False):
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents and output.relative_to(ROOT).parts[0] not in ('dist', 'site-preview'):
        raise ValueError('Use dist/ or an output directory outside the source folder')
    output.mkdir(parents=True, exist_ok=True)
    # Never clean a directory the user may be using for something else.
    if any(output.iterdir()) and not (output / '.friends-english-build').is_file():
        raise ValueError('Output is not an empty Friends English build directory')
    for path in output.iterdir():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    (output / '.friends-english-build').write_text('public assets only\n')
    catalog = json.loads((ROOT / 'catalog.json').read_text(encoding='utf-8'))
    if refresh:
        catalog = refresh_catalog(catalog)
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    html = html.replace('name="fe-storage" content="auto"', 'name="fe-storage" content="browser"')
    (output / 'index.html').write_text(html, encoding='utf-8')
    shutil.copyfile(ROOT / 'icon.png', output / 'icon.png')
    (output / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output / '.nojekyll').touch()
    captions = output / 'study-captions'
    captions.mkdir()
    copied = set()
    for video in catalog['videos']:
        if not VIDEO_ID.fullmatch(video['yt']):
            raise ValueError('Invalid video ID')
        for sub in video.get('subs', []):
            if not re.fullmatch(r'study-captions/[A-Za-z0-9_-]{11}\.(en|ko)\.srt', sub):
                raise ValueError('Only prepared SRT files may be published')
            shutil.copyfile(ROOT / sub, output / sub)
            copied.add(sub)
    files = [p for p in output.rglob('*') if p.is_file()]
    print(f'Built {output}: {len(files)} files, {sum(p.stat().st_size for p in files):,} bytes, '
          f'{len(catalog["videos"])} recommendations, {len(copied)} subtitles')
    return catalog


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'dist')
    parser.add_argument('--refresh', action='store_true', help='Refresh the four public YouTube channel feeds')
    args = parser.parse_args()
    build(args.out, args.refresh)
