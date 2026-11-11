#!/usr/bin/env python3
"""Friends 영어공부 앱 로컬 서버.

    python3 serve.py [LIBRARY_DIR] [--port 8765] [--lan] [--no-open] [--data DIR]

LIBRARY_DIR(기본 ~/Movies/Friends) 안의 영상과 짝이 되는 자막(.srt/.vtt/.smi)을 묶어
에피소드 목록을 만들고, 영상은 Range 요청을 지원하며 스트리밍한다 (Safari/iPad 재생·탐색에 필요).
자막은 같은 폴더(또는 하위 Subs/ 폴더)에서 파일명 앞부분이나 S01E01 코드가 같으면 짝지어진다.

유튜브는 영상을 받지 않고 공식 플레이어로 재생하며, 자막만 yt-dlp로 가져와 DATA/youtube/ 에 둔다.
진도·단어장·유튜브 목록은 DATA/state.json 에 저장해 여러 기기가 같이 쓴다.
"""
import argparse
import fcntl
import json
import os
import re
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from captions import CaptionError, fetch_youtube, youtube_id

APP_DIR = Path(__file__).resolve().parent
VIDEO_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
               '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo'}
CONTENT_TYPES = {**VIDEO_TYPES, '.png': 'image/png'}
BROWSER_OK = {'.mp4', '.m4v', '.mov', '.webm'}
SUB_EXTS = {'.srt', '.vtt', '.smi', '.sami'}
SUB_DIRS = {'subs', 'subtitles', 'sub'}
EP_RE = re.compile(r'[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})|\b(\d{1,2})x(\d{2,3})\b')
JUNK_RE = re.compile(r'\b(\d{3,4}p|blu-?ray|bdrip|brrip|web-?(dl|rip)|hdtv|dvdrip|x26[45]|h\.?26[45]|hevc|remux)\b.*', re.I)


def episode_code(name):
    m = EP_RE.search(name)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(4)))


def nice_title(stem):
    m = EP_RE.search(stem)
    rest = re.sub(r'[._]+', ' ', stem[m.end():] if m else stem)
    return JUNK_RE.sub('', rest).strip(' -[]()')


def scan(root, exts=VIDEO_TYPES):
    """(영상 경로 리스트, {영상이 있을 폴더: [자막 경로]})"""
    videos, subs = [], {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        d = Path(dirpath)
        home = d.parent if d.name.lower() in SUB_DIRS else d
        for f in sorted(filenames):
            ext = os.path.splitext(f)[1].lower()
            if f.startswith('.'):
                continue
            if ext in exts:
                videos.append(d / f)
            elif ext in SUB_EXTS:
                subs.setdefault(home, []).append(d / f)
    return videos, subs


def subtitles_for(video, subs):
    code = episode_code(video.stem)
    return [s for s in subs.get(video.parent, [])
            if s.name.startswith(video.stem + '.') or (code and episode_code(s.stem) == code)]


def episodes(root):
    """[(영상, [자막...])] — 같은 이름의 mp4가 있으면 mkv 같은 원본은 뺀다."""
    videos, subs = scan(root)
    playable = {(v.parent, v.stem) for v in videos if v.suffix.lower() in BROWSER_OK}
    return [(v, subtitles_for(v, subs)) for v in videos
            if v.suffix.lower() in BROWSER_OK or (v.parent, v.stem) not in playable]


def library(root):
    def url(p):
        return '/media/' + quote(p.relative_to(root).as_posix())

    out = []
    for v, subs in episodes(root):
        code = episode_code(v.stem)
        out.append({'id': v.relative_to(root).as_posix(), 'kind': 'file',
                    'season': code[0] if code else None,
                    'episode': code[1] if code else None,
                    'title': nice_title(v.stem),
                    'video': url(v),
                    'subs': [url(s) for s in subs]})
    out.sort(key=lambda e: (e['season'] is None, e['season'] or 0, e['episode'] or 0, e['id']))
    return out


def youtube_entry(item):
    vid = item['id']
    return {'id': 'yt:' + vid, 'kind': 'youtube', 'yt': vid, 'title': item['title'], 'channel': item.get('channel', ''),
            'duration': item.get('duration'), 'season': None, 'episode': None, 'note': item.get('note'),
            'subs': [f'/yt/{vid}.en.srt'] + ([f'/yt/{vid}.ko.srt'] if item.get('ko') else [])}


def note_sync(data):
    """보관함 동기화 설정 'owner/repo 토큰' (DATA/notes-sync, build_pages.py --set-sync가 만든다). 없거나 틀리면 None."""
    try:
        text = (data / 'notes-sync').read_text(encoding='utf-8').strip()
    except OSError:
        return None
    return text if re.fullmatch(r'[\w.-]+/[\w.-]+ [A-Za-z0-9_]{20,255}', text) else None


class State:
    """진도·단어장·유튜브 목록 (JSON 파일 하나). 바꿀 때마다 파일을 다시 읽어서 서버가 둘 떠 있어도 덮어쓰지 않는다."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            pass
        except ValueError:  # 깨진 파일은 지우지 않고 옆으로 치운다
            path.rename(path.with_name(f'{path.name}.broken'))

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        for k, empty in (('notes', list), ('pos', dict), ('delay', dict), ('youtube', list)):
            data.setdefault(k, empty())
        data.setdefault('last', None)
        return data

    def change(self, fn):
        with self.lock, open(self.path.with_name(self.path.name + '.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self.read()
            fn(data)
            tmp = self.path.with_name(self.path.name + '.tmp')
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, self.path)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def host_ok(self):
        # localhost로만 열었을 때는 Host 헤더도 확인 (DNS rebinding 방지). Tailscale 주소(*.ts.net)와 --allow-host는 허용
        host = self.headers.get('Host', '').rsplit(':', 1)[0].lower()
        return (self.server.lan or host in ('localhost', '127.0.0.1') or host.endswith('.ts.net')
                or host in self.server.allow_hosts)

    def route(self):
        if not self.host_ok():
            return self.send_error(403)
        srv = self.server
        path = unquote(urlsplit(self.path).path)
        if self.command in ('POST', 'DELETE'):
            return self.write_api(path)
        if path in ('/', '/index.html'):
            return self.send_body((APP_DIR / 'index.html').read_bytes(), 'text/html; charset=utf-8')
        if path == '/icon.png':
            return self.send_file(APP_DIR, 'icon.png', {'.png'})
        if path == '/catalog.json':
            return self.send_body((APP_DIR / 'catalog.json').read_bytes(), 'application/json; charset=utf-8')
        if path.startswith('/study-captions/'):
            return self.send_file(APP_DIR / 'study-captions', path[len('/study-captions/'):], {'.srt'})
        if path == '/api/library':
            eps = library(srv.root) + [youtube_entry(x) for x in srv.state.read()['youtube']]
            return self.send_json({'root': str(srv.root), 'episodes': eps})
        if path == '/api/state':
            st = srv.state.read()
            return self.send_json({**{k: st[k] for k in ('notes', 'pos', 'delay', 'last')}, 'noteSync': note_sync(srv.data)})
        if path.startswith('/media/'):
            return self.send_file(srv.root, path[len('/media/'):], set(VIDEO_TYPES) | SUB_EXTS)
        if path.startswith('/yt/'):
            return self.send_file(srv.data / 'youtube', path[len('/yt/'):], {'.srt'})
        self.send_error(404)

    def write_api(self, path):
        # 다른 사이트가 몰래 보내는 요청(CSRF) 차단: 같은 출처에서 온 JSON만 받는다
        if (self.headers.get('Sec-Fetch-Site', 'same-origin') not in ('same-origin', 'none')
                or not self.headers.get('Content-Type', '').startswith('application/json')):
            return self.send_error(403)
        try:
            size = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(size) or b'{}') if size <= 1 << 20 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.send_error(400)
        state, cmd = self.server.state, self.command
        m_note = re.fullmatch(r'/api/notes/([\w-]+)', path)
        m_yt = re.fullmatch(r'/api/youtube/([A-Za-z0-9_-]{11})', path)

        if cmd == 'POST' and path == '/api/notes' and isinstance(body.get('id'), str):
            def add(d):
                if all(n.get('id') != body['id'] for n in d['notes']):
                    d['notes'].insert(0, body)
            state.change(add)
        elif cmd == 'DELETE' and m_note:
            state.change(lambda d: d.update(notes=[n for n in d['notes'] if n.get('id') != m_note.group(1)]))
        elif cmd == 'POST' and path == '/api/pos' and isinstance(body.get('ep'), str) and isinstance(body.get('t'), (int, float)):
            def put(d):
                d['pos'][body['ep']] = {'t': body['t'], 'd': body.get('d')}
                d['last'] = body['ep']
            state.change(put)
        elif cmd == 'POST' and path == '/api/delay' and isinstance(body.get('ep'), str) and isinstance(body.get('delay'), (int, float)):
            state.change(lambda d: d['delay'].update({body['ep']: body['delay']}))
        elif cmd == 'POST' and path == '/api/youtube':
            vid = youtube_id(str(body.get('url', '')))
            if not vid:
                return self.send_json({'error': '유튜브 주소를 알아보지 못했습니다'}, 400)
            try:
                item = fetch_youtube(vid, self.server.data / 'youtube')
            except CaptionError as e:
                return self.send_json({'error': str(e)}, 422)
            state.change(lambda d: d.update(youtube=[item] + [x for x in d['youtube'] if x['id'] != vid]))
            return self.send_json(youtube_entry(item))
        elif cmd == 'DELETE' and m_yt:
            vid = m_yt.group(1)
            state.change(lambda d: d.update(youtube=[x for x in d['youtube'] if x['id'] != vid]))
            for f in (self.server.data / 'youtube').glob(vid + '.*'):
                f.unlink()
        else:
            return self.send_error(404)
        self.send_json({'ok': True})

    def send_json(self, obj, code=200):
        self.send_body(json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8', code)

    def send_body(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def send_file(self, base, rel, exts):
        base = str(base)
        path = os.path.normpath(os.path.join(base, rel))
        ext = os.path.splitext(path)[1].lower()
        if not path.startswith(base + os.sep) or not os.path.isfile(path) or ext not in exts:
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers.get('Range', '').strip())
        partial = bool(m and (m.group(1) or m.group(2)))
        if partial:
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type', CONTENT_TYPES.get(ext, 'text/plain'))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # 탐색할 때 브라우저가 이전 요청을 끊는 건 정상
            self.close_connection = True

    def log_request(self, code='-', size='-'):
        pass  # 영상 Range 요청이 많아서 성공 로그는 생략


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('10.255.255.255', 1))
            return s.getsockname()[0]
        except OSError:
            return '127.0.0.1'


def main():
    ap = argparse.ArgumentParser(description='Friends 영어공부 앱 로컬 서버')
    ap.add_argument('library', nargs='?', default='~/Movies/Friends', help='영상·자막 폴더 (기본 ~/Movies/Friends)')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--lan', action='store_true', help='같은 네트워크의 아이패드 등에서도 접속 허용')
    ap.add_argument('--no-open', action='store_true', help='브라우저를 자동으로 열지 않음')
    ap.add_argument('--data', default='~/Library/Application Support/FriendsEnglish', help='진도·단어장·유튜브 자막 저장 폴더')
    ap.add_argument('--allow-host', action='append', default=[], metavar='NAME', help='다른 프록시로 접속할 때 허용할 호스트 이름')
    args = ap.parse_args()

    root = Path(os.path.abspath(os.path.expanduser(args.library)))
    data = Path(os.path.abspath(os.path.expanduser(args.data)))
    if not root.is_dir():
        print(f'[주의] 라이브러리 폴더가 없습니다: {root}\n       mkdir -p "{root}" 후 영상과 자막을 넣으세요.', file=sys.stderr)
    try:
        srv = ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1', args.port), Handler)
    except OSError as e:
        sys.exit(f'포트 {args.port}을(를) 열 수 없습니다 ({e}). --port 로 다른 포트를 지정하세요.')
    srv.root, srv.data, srv.lan = root, data, args.lan
    srv.state = State(data / 'state.json')
    srv.allow_hosts = {h.lower() for h in args.allow_host}
    url = f'http://localhost:{args.port}/'
    print(f'라이브러리: {root}\n저장 위치: {data}\n앱: {url}', flush=True)
    if args.lan:
        print(f'다른 기기: http://{lan_ip()}:{args.port}/  (같은 네트워크의 누구나 접속 가능)', flush=True)
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
