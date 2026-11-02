#!/usr/bin/env python3
"""Package the Pages app, locked with a password by default; optionally refresh recommendations from YouTube RSS."""
import argparse
import base64
import concurrent.futures
import copy
import datetime as dt
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
NS = {'a': 'http://www.w3.org/2005/Atom', 'yt': 'http://www.youtube.com/xml/schemas/2015'}
KEY_ENV = 'FE_PAGES_KEY'
KEY_FILE = Path.home() / 'Library' / 'Application Support' / 'FriendsEnglish' / 'pages.key'
ITERATIONS = 600_000  # PBKDF2-HMAC-SHA256; each device pays this once when logging in


def urlsafe(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def make_key(password, iterations=ITERATIONS):
    """Derive the site key as 'fe1.<iterations>.<salt>.<key>'. The salt is published; the key is secret."""
    salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac('sha256', unicodedata.normalize('NFC', password).encode(), salt, iterations)
    return f'fe1.{iterations}.{urlsafe(salt)}.{urlsafe(key)}'


def parse_key(text):
    try:
        tag, iterations, salt, key = text.strip().split('.')
        iterations = int(iterations)
        salt, key = (base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)) for p in (salt, key))
        if tag != 'fe1' or iterations < 1000 or len(salt) < 16 or len(key) != 32:
            raise ValueError
    except ValueError:  # also binascii.Error and a wrong number of parts
        raise ValueError('Invalid Pages key; create a new one with --set-password') from None
    return {'iterations': iterations, 'salt': salt, 'key': key}


def load_key():
    text = os.environ.get(KEY_ENV) or (KEY_FILE.read_text() if KEY_FILE.is_file() else '')
    if not text.strip():
        raise SystemExit(f'No Pages key in ${KEY_ENV} or {KEY_FILE}.\n'
                         'Run "python3 build_pages.py --set-password" (GitHub Actions reads the PAGES_KEY secret), '
                         'or add --public for an unlocked local preview.')
    try:
        return parse_key(text)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def set_password():
    password = getpass.getpass('New password (10+ characters, Enter = generate one): ')
    if not password:
        password = '-'.join(''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(4)) for _ in range(4))
        print(f'Generated password: {password}  (save it in your password manager)')
    elif len(password) < 10:
        raise SystemExit('Use at least 10 characters: the locked files are public, so a short password can be guessed offline.')
    elif getpass.getpass('Repeat: ') != password:
        raise SystemExit('The passwords differ.')
    key = make_key(password)
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.touch(mode=0o600)
    KEY_FILE.chmod(0o600)
    KEY_FILE.write_text(key + '\n')
    print(f'Saved the site key to {KEY_FILE}. After the next deploy every device logs in with the new password.')
    if shutil.which('gh') and input('Save it as the PAGES_KEY secret of this GitHub repo too? [Y/n] ').strip().lower() in ('', 'y', 'yes'):
        subprocess.run(['gh', 'secret', 'set', 'PAGES_KEY'], input=key.encode(), cwd=ROOT, check=True)
    else:
        print(f'GitHub Actions needs it: gh secret set PAGES_KEY < "{KEY_FILE}"')


def seal(output, names, key):
    """Encrypt files in place with AES-256-GCM through Deno's Web Crypto, the API the browser decrypts with."""
    deno = shutil.which('deno')
    if not deno:
        raise SystemExit('Locked builds need Deno: brew install deno')
    subprocess.run([deno, 'run', '--quiet', '--no-prompt', '--no-config', f'--allow-read={output}', f'--allow-write={output}',
                    str(ROOT / 'seal.mjs'), str(output), *names], input=key, check=True)


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


def build(output, refresh=False, lock=None):
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
    if lock:
        # The app, catalog and subtitles are published only as ciphertext; gate.html asks for the password.
        seal(output, ['index.html', 'catalog.json', *sorted(copied)], lock['key'])
        gate = (ROOT / 'gate.html').read_text(encoding='utf-8')
        if gate.count('/*LOCK*/null') != 1:
            raise ValueError('gate.html needs exactly one /*LOCK*/null placeholder')
        config = {'iter': lock['iterations'], 'salt': base64.b64encode(lock['salt']).decode(),
                  'app': base64.b64encode((output / 'index.html').read_bytes()).decode()}
        (output / 'index.html').write_text(gate.replace('/*LOCK*/null', json.dumps(config)), encoding='utf-8')
    files = [p for p in output.rglob('*') if p.is_file()]
    print(f'Built {output} ({"locked" if lock else "UNLOCKED"}): {len(files)} files, '
          f'{sum(p.stat().st_size for p in files):,} bytes, {len(catalog["videos"])} recommendations, {len(copied)} subtitles')
    return catalog


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'dist')
    parser.add_argument('--refresh', action='store_true', help='Refresh the four public YouTube channel feeds')
    parser.add_argument('--public', action='store_true', help='Build without the password lock (local preview only)')
    parser.add_argument('--set-password', action='store_true', help='Choose the site password and save the key')
    args = parser.parse_args()
    if args.set_password:
        set_password()
    else:
        build(args.out, args.refresh, None if args.public else load_key())
