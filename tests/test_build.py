import copy
import importlib.util
import json
import re
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
            self.assertEqual(len(files), 13)
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
            self.assertEqual(total_cues, 3872)
            self.assertLess(sum(p.stat().st_size for p in out.rglob('*') if p.is_file()), 2_000_000)

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
        self.assertEqual({v['yt'] for v in result['videos'] if v.get('source') != 'feed'},
                         {v['yt'] for v in old['videos'] if v.get('source') != 'feed'})

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


if __name__ == '__main__':
    unittest.main()
