import importlib.util
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # serve.py imports captions.py
spec = importlib.util.spec_from_file_location('serve', ROOT / 'serve.py')
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)


class StateTests(unittest.TestCase):
    def test_state_carries_notes_sync_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            srv = serve.ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
            srv.root, srv.data, srv.lan, srv.allow_hosts = data, data, False, set()
            srv.state = serve.State(data / 'state.json')
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                def state():
                    with urllib.request.urlopen(f'http://localhost:{srv.server_port}/api/state') as response:
                        return json.load(response)
                self.assertIsNone(state()['noteSync'])
                good = 'me/sync github_pat_' + 'a' * 40
                (data / 'notes-sync').write_text(good + '\n')
                self.assertEqual(state()['noteSync'], good)
                (data / 'notes-sync').write_text('me/sync not-a-token"')
                self.assertIsNone(state()['noteSync'])
                self.assertEqual(state()['notes'], [])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == '__main__':
    unittest.main()
