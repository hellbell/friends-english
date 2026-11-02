import copy
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_pages', ROOT / 'build_pages.py')
build_pages = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_pages)


class PublicBuildTests(unittest.TestCase):
    def setUp(self):
        self.catalog = json.loads((ROOT / 'catalog.json').read_text())

    def test_public_package_and_subtitle_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'site'
            build_pages.build(out)
            files = {p.relative_to(out).as_posix() for p in out.rglob('*') if p.is_file()}
            subtitle_paths = {sub for item in self.catalog['videos'] for sub in item.get('subs', [])}
            self.assertEqual(files, {'index.html', 'icon.png', 'catalog.json', '.nojekyll', '.friends-english-build'} | subtitle_paths)
            self.assertTrue({'index.html', 'icon.png', 'catalog.json', '.nojekyll', '.friends-english-build'} <= files)
            self.assertFalse(any(p.endswith(('.mp4', '.py', '.log')) or 'state' in p or 'downloads' in p for p in files))
            self.assertIn('name="fe-storage" content="browser"', (out / 'index.html').read_text())
            total_cues = 0
            for item in self.catalog['videos']:
                subs = item.get('subs', [])
                if not subs:
                    continue
                en, ko = [(out / p).read_text() for p in subs]
                times = lambda text: re.findall(r'^\d\d:\d\d:\d\d,\d{3} --> .+$', text, re.M)
                self.assertEqual(times(en), times(ko))
                self.assertGreater(len(times(en)), 0)
                total_cues += len(times(en))
            self.assertGreater(total_cues, 0)
            self.assertLess(sum(p.stat().st_size for p in out.rglob('*') if p.is_file()), 8_000_000)

    def test_failed_feed_preserves_previous_channel(self):
        old = copy.deepcopy(self.catalog)
        channel = old['channels'][0]
        prior = {'yt': 'abcdefghijk', 'title': 'Saved', 'source': 'feed', 'channelId': channel['id']}
        old['videos'].append(prior)
        with patch.object(build_pages, 'channel_videos', side_effect=OSError('offline')):
            result = build_pages.refresh_catalog(old)
        self.assertIn(prior, result['videos'])
        self.assertEqual(result['updatedAt'], old['updatedAt'])
        self.assertEqual(result['feedStatus']['updated'], 0)
        self.assertEqual([v for v in result['videos'] if v.get('source') != 'feed'],
                         [v for v in old['videos'] if v.get('source') != 'feed'])

    def test_refresh_deduplicates_curated_video(self):
        selected = next(v for v in self.catalog['videos'] if v.get('source') == 'curated')
        with patch.object(build_pages, 'channel_videos', return_value=[{**selected, 'source': 'feed'}]):
            result = build_pages.refresh_catalog(self.catalog)
        matches = [v for v in result['videos'] if v['yt'] == selected['yt']]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]['source'], 'curated')

    def test_refuses_to_clean_unrelated_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            keep = out / 'personal.txt'
            keep.write_text('keep')
            with self.assertRaises(ValueError):
                build_pages.build(out)
            self.assertEqual(keep.read_text(), 'keep')


class LockedBuildTests(unittest.TestCase):
    password = 'correct horse battery'

    def test_key_format(self):
        key = build_pages.make_key(self.password, iterations=1000)
        lock = build_pages.parse_key(key + '\n')
        self.assertEqual((lock['iterations'], len(lock['salt']), len(lock['key'])), (1000, 16, 32))
        self.assertNotEqual(build_pages.make_key(self.password, iterations=1000), key)  # fresh salt
        tag, iterations, salt, secret = key.split('.')
        for bad in ('', 'fe1.1000.abc', f'fe2.{iterations}.{salt}.{secret}', f'fe1.999.{salt}.{secret}',
                    f'fe1.x.{salt}.{secret}', f'fe1.{iterations}.{salt}.{secret[:-8]}', key + '.extra'):
            with self.assertRaises(ValueError, msg=bad):
                build_pages.parse_key(bad)

    def test_missing_key_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {build_pages.KEY_ENV: ''}), \
                patch.object(build_pages, 'KEY_FILE', Path(tmp) / 'missing.key'):
            with self.assertRaises(SystemExit):
                build_pages.load_key()

    @unittest.skipUnless(shutil.which('deno'), 'Deno seals and opens locked builds')
    def test_locked_build_opens_only_with_password(self):
        lock = build_pages.parse_key(build_pages.make_key(self.password, iterations=1000))
        with tempfile.TemporaryDirectory() as tmp:
            plain, locked, opened = (Path(tmp) / name for name in ('plain', 'locked', 'opened'))
            build_pages.build(plain)
            build_pages.build(locked, lock=lock)
            files = lambda d: {p.relative_to(d).as_posix() for p in d.rglob('*') if p.is_file()}
            self.assertEqual(files(locked), files(plain))
            sealed = sorted(files(plain) - {'icon.png', '.nojekyll', '.friends-english-build'})
            self.assertNotIn('STATIC_MODE', (locked / 'index.html').read_text())
            for name in sealed:
                if name != 'index.html':
                    data, original = (locked / name).read_bytes(), (plain / name).read_bytes()
                    self.assertEqual(len(data), len(original) + 28, name)  # IV + tag only
                    self.assertNotIn(original[:64], data, name)
            result = subprocess.run(['deno', 'run', '--quiet', '--no-prompt', '--no-config', f'--allow-read={locked}',
                                     f'--allow-write={opened}', str(ROOT / 'tests' / 'lock_check.mjs'),
                                     str(locked), str(opened), self.password], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in sealed:
                self.assertEqual((opened / name).read_bytes(), (plain / name).read_bytes(), name)


if __name__ == '__main__':
    unittest.main()
