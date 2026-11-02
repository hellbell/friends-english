#!/usr/bin/env python3
"""라이브러리의 영상·자막을 브라우저에서 쓸 수 있게 정리한다 (ffmpeg 필요).

    python3 prep.py SRC [--out LIBRARY]               # mkv/avi → mp4, 내장 영어·한국어 텍스트 자막 → .en.srt/.ko.srt
    python3 prep.py LIBRARY --whisper MODEL.bin       # 영어 자막이 없는 영상은 whisper.cpp로 생성
    python3 prep.py LIBRARY --check-sync MODEL.bin    # 영어 자막 싱크를 실제 음성과 비교 (whisper.cpp)
    python3 prep.py SRC --dry-run                     # 실행할 명령만 출력

--out을 주면 SRC의 폴더 구조 그대로 LIBRARY에 mp4와 자막을 만든다. 원본은 지우지 않는다.
같은 이름의 mp4가 생기면 serve.py가 원본(mkv 등)을 목록에서 숨긴다.
H.264/HEVC 영상과 AAC/MP3 음성은 재인코딩 없이 복사하므로 대부분 몇 초면 끝난다.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from captions import sentences, srt_cues, write_srt
from serve import BROWSER_OK, VIDEO_TYPES, episodes, scan

CONVERT_EXTS = {'.mkv', '.avi', '.wmv', '.flv', '.ts', '.mpg', '.mpeg'}
TEXT_SUBS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}
WANT_LANGS = {'en', 'ko'}  # 앱이 쓰는 언어만 뽑는다 (다른 라틴 문자 자막은 영어로 잘못 잡힐 수 있음)
FFMPEG = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y']


def run(cmd, dry, quiet=False):
    print('  $', ' '.join(shlex.quote(str(c)) for c in cmd))
    if not dry:
        subprocess.run([str(c) for c in cmd], check=True, stdout=subprocess.DEVNULL if quiet else None)


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def lang_of(stream):
    lang = (stream.get('tags') or {}).get('language', '').lower()
    return {'eng': 'en', 'kor': 'ko'}.get(lang, lang or 'und')


def to_mp4(src, dst, streams, dry):
    if dst.exists():
        return
    v = next((s for s in streams if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')), None)
    auds = [s for s in streams if s['codec_type'] == 'audio']
    a = next((s for s in auds if lang_of(s) == 'en'), auds[0] if auds else None)
    if v is None:
        print('  영상 스트림이 없어 건너뜀')
        return
    cmd = FFMPEG + ['-stats', '-i', src, '-map', f"0:{v['index']}"]
    if v['codec_name'] == 'h264' and v.get('pix_fmt', 'yuv420p') in ('yuv420p', 'yuvj420p'):
        cmd += ['-c:v', 'copy']
    elif v['codec_name'] == 'hevc':
        cmd += ['-c:v', 'copy', '-tag:v', 'hvc1']
    else:  # xvid, 10bit h264 등 브라우저가 못 여는 코덱은 재인코딩 (느림)
        cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p']
    if a is not None:
        cmd += ['-map', f"0:{a['index']}"]
        cmd += ['-c:a', 'copy'] if a['codec_name'] in ('aac', 'mp3') else ['-c:a', 'aac', '-b:a', '192k', '-ac', '2']
    part = dst.with_name(dst.name + '.part')
    if not dry:
        dst.parent.mkdir(parents=True, exist_ok=True)
    run(cmd + ['-movflags', '+faststart', '-f', 'mp4', part], dry)
    if not dry:
        part.replace(dst)


def extract_subs(src, dst, streams, dry):
    """내장 텍스트 자막 중 영어·한국어를 언어마다 하나씩 dst 옆에 .<lang>.srt 로 (forced 제외, SDH는 일반 자막이 없을 때만)"""
    def sdh(s):
        title = (s.get('tags') or {}).get('title', '').lower()
        return bool(s.get('disposition', {}).get('hearing_impaired')) or any(k in title for k in ('sdh', 'hearing', 'cc'))

    subs = [s for s in streams if s['codec_type'] == 'subtitle' and lang_of(s) in WANT_LANGS
            and not s.get('disposition', {}).get('forced')]
    done = set()
    for s in sorted(subs, key=sdh):
        lang = lang_of(s)
        if lang in done:
            continue
        if s['codec_name'] not in TEXT_SUBS:
            print(f"  자막 #{s['index']} ({lang}, {s['codec_name']})은 이미지 자막이라 텍스트로 뽑을 수 없음")
            continue
        done.add(lang)
        out = dst.with_name(f'{dst.stem}.{lang}.srt')
        if not out.exists():
            if not dry:
                out.parent.mkdir(parents=True, exist_ok=True)
            run(FFMPEG + ['-i', src, '-map', f"0:{s['index']}", '-c:s', 'srt', out], dry)


def english_ratio(path):
    raw = path.read_bytes()
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = raw.decode('utf-16', errors='replace')
    else:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp949', errors='replace')
    text = re.sub(r'<[^>]*>', '', text)
    latin, hangul = len(re.findall(r'[A-Za-z]', text)), len(re.findall(r'[가-힣]', text))
    return latin / (latin + hangul) if latin + hangul else 0


def english_sub(subs):
    """짝지어진 자막 중 영어 자막 하나 (.en 이름 우선)"""
    tagged = [s for s in subs if re.search(r'\.(en|eng|english)$', s.stem, re.I)]
    return next((s for s in tagged + subs if s.suffix.lower() == '.srt' and english_ratio(s) >= 0.3), None)


def dtw_preset(model):
    """ggml-large-v3-turbo-q5_0.bin → large.v3.turbo (whisper-cli -dtw 이름)"""
    name = re.sub(r'-q\d_\d$', '', model.stem.replace('ggml-', ''))
    return name.replace('-', '.')


def whisper_words(audio, model, whisper):
    """whisper.cpp로 받아써서 [(발화 시작 초, 단어)].
    DTW 토큰 시각을 쓴다 — 기본 단어 시각(-ml 1)은 문장 앞 쉼까지 먹어서 문장 시작이 최대 1초쯤 일찍 잡힌다."""
    out = audio.with_name('out')
    base = [whisper, '-m', model, '-l', 'en', '-f', audio, '-ojf', '-of', out, '-np']
    if subprocess.run([str(c) for c in base + ['-dtw', dtw_preset(model)]], capture_output=True).returncode != 0:
        subprocess.run([str(c) for c in base], capture_output=True, check=True)
    words = []
    for seg in json.loads(out.with_suffix('.json').read_text(encoding='utf-8', errors='replace')).get('transcription', []):
        for tok in seg.get('tokens', []):
            text = tok.get('text', '')
            if text.startswith('[_') or text.startswith('<|'):
                continue
            t = tok['t_dtw'] / 100 if tok.get('t_dtw', -1) >= 0 else tok['offsets']['from'] / 1000
            if text.startswith(' ') or not words:
                words.append([t, text.strip()])
            else:
                words[-1][1] += text  # 같은 단어의 나머지 조각 (n't, 's, 문장 부호)
    return [(t, w) for t, w in words if w]


def transcribe(video, model, whisper, dry):
    dst = video.with_name(video.stem + '.en.srt')
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / 'audio.wav'
        run(FFMPEG + ['-i', video, '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', wav], dry)
        print(f'  $ {whisper} -m {model} -f {wav} -ojf -dtw {dtw_preset(model)}')
        if dry:
            return
        words = whisper_words(wav, model, whisper)
        if not words:
            print('  받아쓴 내용이 없음')
            return
        # 단어 끝 = 다음 단어 시작 (최대 1초) → 문장 사이의 긴 쉼이 자막에 붙지 않는다
        starts = [t for t, _ in words[1:]] + [words[-1][0] + 1.0]
        sents = sentences([(t, min(nxt, t + 1.0), w) for (t, w), nxt in zip(words, starts)])
        write_srt(dst, sents)
        print(f'  → {dst.name}: {len(sents)}문장 ({time.time() - t0:.0f}초)')


# ---------- 싱크 검사: 영상 몇 구간을 whisper로 받아쓰고, 자막 첫 단어들이 실제로 말해진 시각과 비교한다
def norm(w):
    return re.sub(r"[^a-z0-9']", '', w.lower().replace('’', "'")).strip("'")


def cue_words(text):
    text = re.sub(r'<[^>]*>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|♪', ' ', text)
    text = re.sub(r'(^|\s)-\s*', ' ', text)  # 대화 표시 "- "
    text = re.sub(r'^\s*[A-Z][A-Z .\'-]{1,20}:', ' ', text)  # 화자 표시 "ROSS:"
    return [w for w in map(norm, text.split()) if w]


def heard_words(audio, model, whisper):
    return [(t, norm(w)) for t, w in whisper_words(audio, model, whisper) if norm(w)]


def window_offsets(cues, words, t0, t1):
    """창 [t0, t1] 안의 자막마다 첫 2~3단어를 ±3초 안에서 찾아 (자막 시작 − 실제 발화 시작)"""
    offs = []
    for a, _, text in cues:
        ws = cue_words(text)[:3]
        if not (t0 + 3 <= a <= t1 - 3) or len(ws) < 2:
            continue
        need = len(ws) - (1 if len(ws) == 3 else 0)  # 3단어면 2개, 2단어면 둘 다 맞아야
        best = None
        for i, (t, w) in enumerate(words):
            if abs(t - a) <= 3 and w == ws[0] and sum(x == y for x, (_, y) in zip(ws, words[i:i + len(ws)])) >= need:
                d = a - t
                best = d if best is None or abs(d) < abs(best) else best
        if best is not None:
            offs.append(best)
    return offs


def check_sync(root, model, whisper, span=90):
    print('영상별로 앞·중간·뒤 세 구간을 받아써서 자막 시작 시각과 실제 발화 시각을 비교합니다.')
    print('차이(초) = 자막 시작 − 발화 시작. +면 자막이 늦게, −면 일찍 나옴.')
    print('자막은 보통 말보다 조금 일찍 띄우고, 이 측정은 정확히 맞아도 −0.1초쯤 나오므로')
    print('세 구간 모두 −0.5 ~ +0.3초이고 구간끼리 0.3초 넘게 벌어지지 않으면(점점 밀리지 않으면) 맞는 것으로 봅니다.\n')
    bad = 0
    for v, subs in episodes(root):
        sub = english_sub(subs)
        if not sub:
            print(f'{v.name}: 영어 자막 없음')
            continue
        cues = srt_cues(sub.read_text(encoding='utf-8-sig', errors='replace'))
        dur = float(probe(v)['format']['duration'])
        meds, n = [], 0
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / 'a.wav'
            for frac in (0.15, 0.5, 0.8):
                t0 = max(0.0, min(dur - span, dur * frac))
                subprocess.run(FFMPEG + ['-ss', f'{t0:.2f}', '-t', str(span), '-i', str(v), '-vn', '-ac', '1', '-ar', '16000',
                                         '-c:a', 'pcm_s16le', str(audio)], check=True)
                words = [(t + t0, w) for t, w in heard_words(audio, model, whisper)]
                offs = window_offsets(cues, words, t0, t0 + span)
                n += len(offs)
                meds.append(statistics.median(offs) if len(offs) >= 3 else None)
        got = [m for m in meds if m is not None]
        ok = len(got) >= 2 and all(-0.5 <= m <= 0.3 for m in got) and max(got) - min(got) <= 0.3
        bad += not ok
        shown = ' / '.join('  ?  ' if m is None else f'{m:+.2f}' for m in meds)
        print(f"{'맞음 ' if ok else '확인 '} {v.stem[:52]:52s}  {shown}   (비교한 자막 {n}줄)", flush=True)
    print(f'\n싱크를 확인해야 할 영상: {bad}개')


def main():
    ap = argparse.ArgumentParser(description='영상·자막을 브라우저용으로 정리')
    ap.add_argument('library', nargs='?', default='~/Movies/Friends')
    ap.add_argument('--out', metavar='LIBRARY', help='변환 결과를 둘 라이브러리 폴더 (원본 폴더 구조 유지)')
    ap.add_argument('--whisper', metavar='MODEL', help='whisper.cpp ggml 모델 경로 — 영어 자막이 없는 영상에 자막 생성')
    ap.add_argument('--check-sync', metavar='MODEL', help='whisper.cpp ggml 모델로 영어 자막 싱크 검사')
    ap.add_argument('--dry-run', action='store_true', help='실행할 명령만 출력')
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # ffmpeg 출력과 순서가 섞이지 않게

    root = Path(os.path.abspath(os.path.expanduser(args.library)))
    out = Path(os.path.abspath(os.path.expanduser(args.out))) if args.out else None
    if not root.is_dir():
        sys.exit(f'폴더가 없습니다: {root}')
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        sys.exit('ffmpeg가 필요합니다: brew install ffmpeg')
    whisper = shutil.which('whisper-cli') or shutil.which('whisper-cpp')
    for opt in (args.whisper, args.check_sync):
        if opt and not whisper:
            sys.exit('whisper.cpp가 필요합니다: brew install whisper-cpp')
        if opt and not Path(opt).expanduser().is_file():
            sys.exit(f'모델 파일이 없습니다: {opt}')

    if args.check_sync:
        return check_sync(out or root, Path(args.check_sync).expanduser(), whisper)

    videos, _ = scan(root, set(VIDEO_TYPES) | CONVERT_EXTS)
    for i, v in enumerate(videos, 1):
        print(f'[{i}/{len(videos)}] {v.relative_to(root)}')
        dst = (out / v.relative_to(root) if out else v).with_suffix('.mp4')
        streams = probe(v)['streams']
        extract_subs(v, dst, streams, args.dry_run)
        if out or v.suffix.lower() not in BROWSER_OK:
            to_mp4(v, dst, streams, args.dry_run)

    if args.whisper:
        lib = out or root
        todo = [v for v, subs in episodes(lib) if not any(english_ratio(s) >= 0.3 for s in subs)]
        print(f'\n영어 자막이 없는 영상 {len(todo)}개를 whisper로 받아씁니다')
        for i, v in enumerate(todo, 1):
            print(f'[{i}/{len(todo)}] {v.relative_to(lib)}')
            transcribe(v, Path(args.whisper).expanduser(), whisper, args.dry_run)


if __name__ == '__main__':
    main()
