"""자막 유틸: SRT 읽기/쓰기, 단어 단위 자막을 문장으로 묶기, 유튜브 자막 가져오기(yt-dlp)."""
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

TIME_RE = re.compile(r'(\d+):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{3})')
SENT_END = re.compile(r'[.?!]["\')\]]*$')
ABBREV = {'mr.', 'mrs.', 'ms.', 'dr.', 'st.', 'jr.', 'sr.', 'vs.', 'e.g.', 'i.e.'}
NOISE_RE = re.compile(r'\[[^\]]*\]|\([^)]*\)|>>|♪')  # [Music] (applause) >> 화자 표시
YT_ID_RE = re.compile(r'(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})|^([A-Za-z0-9_-]{11})$')


class CaptionError(Exception):
    pass


def srt_cues(text):
    cues = []
    for block in re.split(r'\n\s*\n', text.replace('\r', '')):
        lines = block.strip('\n').split('\n')
        for k, line in enumerate(lines):
            m = TIME_RE.search(line)
            if m:
                g = [int(x) for x in m.groups()]
                cues.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                             g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000,
                             ' '.join(' '.join(lines[k + 1:]).split())))
                break
    return cues


def srt_time(t):
    ms = round(t * 1000)
    return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'


def write_srt(path, cues):
    path.write_text(''.join(f'{i}\n{srt_time(a)} --> {srt_time(b)}\n{t}\n\n' for i, (a, b, t) in enumerate(cues, 1)),
                    encoding='utf-8')


def sentences(words):
    """[(start, end, word)] → [(start, end, sentence)].
    문장 부호가 있으면 . ? ! 에서 끊고, 부호가 거의 없는 자동 자막은 말의 쉼에서 끊는다."""
    words = [w for w in words if w[2]]
    punctuated = sum(1 for w in words if SENT_END.search(w[2])) * 40 >= len(words)
    groups = by_punctuation(words) if punctuated else by_pause(words)
    return [(g[0][0], g[-1][1], ' '.join(w for _, _, w in g)) for g in groups]


def by_punctuation(words):
    out, cur = [], []
    for a, b, w in words:
        if cur and a - cur[-1][1] > 1.0:
            out.append(cur)
            cur = []
        cur.append((a, b, w))
        if (SENT_END.search(w) and w.lower() not in ABBREV) or (len(cur) >= 20 and w.endswith(',')) or len(cur) >= 35:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def by_pause(words, lo=5, hi=14):
    """lo~hi 단어 안에서 처음 나오는 0.6초 이상 쉼(없으면 가장 긴 쉼) 뒤에서 끊는다. 1.5초 넘게 쉬면 짧아도 끊음"""
    gap = [b[0] - a[0] for a, b in zip(words, words[1:])] + [99.0]  # 단어 i 다음 쉼 (시작 시각 간격)
    out, s = [], 0
    while s < len(words):
        early = [i for i in range(s, min(s + lo - 1, len(words))) if gap[i] >= 1.5]
        window = range(s + lo - 1, min(s + hi, len(words)))
        if early:
            end = early[0]
        elif not window:
            end = len(words) - 1
        else:
            end = next((i for i in window if gap[i] >= 0.6), max(window, key=gap.__getitem__))
        out.append(words[s:end + 1])
        s = end + 1
    return out


def json3_events(path):
    for ev in json.loads(Path(path).read_text(encoding='utf-8')).get('events', []):
        if ev.get('segs') and not ev.get('aAppend'):
            yield ev['tStartMs'] / 1000, ev.get('dDurationMs', 0) / 1000, ev['segs']


def json3_words(path):
    """유튜브 json3 자막 → [(start, end, word)].
    자동 자막은 단어마다 시각(tOffsetMs)이 있고, 수동 자막은 줄 단위라 글자 수 비율로 시각을 나눠 준다."""
    words = []
    for t0, dur, segs in json3_events(path):
        if len(segs) > 1 and any('tOffsetMs' in s for s in segs):
            for s in segs:
                w = s.get('utf8', '').strip()
                if w and not NOISE_RE.fullmatch(w):
                    a = t0 + s.get('tOffsetMs', 0) / 1000
                    words.append([a, min(a + 0.6, t0 + dur), w])  # 끝 시각이 없어서 0.6초로 가정 → 쉼을 잴 수 있게
        else:
            toks = NOISE_RE.sub(' ', ''.join(s.get('utf8', '') for s in segs)).split()
            total, a = sum(len(t) + 1 for t in toks), t0
            for tok in toks:
                d = dur * (len(tok) + 1) / total
                words.append([a, a + d, tok])
                a += d
    words.sort(key=lambda w: w[0])
    for w, nxt in zip(words, words[1:]):
        w[1] = min(w[1], nxt[0])
    return [tuple(w) for w in words]


def json3_lines(path):
    """줄 단위 [(start, end, text)] — 번역 자막처럼 단어 시각이 의미 없을 때"""
    cues = [(t0, t0 + dur, ' '.join(NOISE_RE.sub(' ', ''.join(s.get('utf8', '') for s in segs)).split()))
            for t0, dur, segs in json3_events(path)]
    cues = [c for c in cues if c[2]]
    return [(a, min(b, nxt[0]) if nxt else b, t) for (a, b, t), nxt in zip(cues, cues[1:] + [None])]


def youtube_id(text):
    m = YT_ID_RE.search(text.strip())
    return m and (m.group(1) or m.group(2))


def fetch_youtube(vid, outdir):
    """영상은 받지 않고 정보와 자막만 받아 outdir/<id>.en.srt(문장 단위), <id>.ko.srt(있으면)로 저장한다."""
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        raise CaptionError('yt-dlp가 필요합니다: brew install yt-dlp')
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [ytdlp, '--skip-download', '--no-playlist', '--ignore-errors', '--no-progress', '--no-warnings',
               '--write-info-json', '--write-subs', '--write-auto-subs', '--sub-langs', 'en.*,ko', '--sub-format', 'json3',
               '-o', f'{tmp}/v.%(ext)s', f'https://www.youtube.com/watch?v={vid}']
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            raise CaptionError('유튜브 응답이 너무 늦습니다. 잠시 후 다시 시도하세요')
        info_path = Path(tmp) / 'v.info.json'
        if not info_path.exists():
            last = (r.stderr.strip().splitlines() or ['알 수 없는 오류'])[-1]
            raise CaptionError(f'영상 정보를 가져오지 못했습니다: {last}')
        info = json.loads(info_path.read_text(encoding='utf-8'))
        if info.get('playable_in_embed') is False:
            raise CaptionError('퍼가기가 막힌 영상이라 앱에서 재생할 수 없습니다 (유튜브에서만 재생 가능)')
        manual = set(info.get('subtitles') or {})
        files = {p.name[2:-6]: p for p in Path(tmp).glob('v.*.json3')}  # v.<lang>.json3
        en = sorted((l for l in files if l == 'en' or l.startswith('en-')), key=lambda l: (l not in manual, l != 'en-orig', l))
        if not en:
            raise CaptionError('영어 자막이 없는 영상입니다')
        cues = sentences(json3_words(files[en[0]]))
        if not cues:
            raise CaptionError('영어 자막이 비어 있습니다')
        outdir.mkdir(parents=True, exist_ok=True)
        write_srt(outdir / f'{vid}.en.srt', cues)
        has_ko = 'ko' in files
        if has_ko:
            write_srt(outdir / f'{vid}.ko.srt', json3_lines(files['ko']))
    item = {'id': vid, 'title': info.get('title') or vid, 'channel': info.get('channel') or info.get('uploader') or '',
            'duration': info.get('duration'), 'captions': 'manual' if en[0] in manual else 'auto', 'ko': has_ko,
            'sentences': len(cues), 'added': int(time.time())}
    if not has_ko and 'ko' in {**(info.get('automatic_captions') or {}), **(info.get('subtitles') or {})}:
        # 자동 번역 자막은 유튜브가 자주 요청 제한(429)을 건다
        item['note'] = '한국어 자막은 유튜브가 잠시 막아서 못 가져왔습니다. 나중에 같은 주소를 다시 추가하면 받아옵니다'
    return item
