import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('cloud', Path(__file__).resolve().parents[1] / 'cloud_monitor.py')
cloud = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cloud)

class Response:
    def __init__(self, status=200, payload=None, content=b'', headers=None):
        self.status_code, self.payload, self.content, self.headers = status, payload, content, headers or {}
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError('HTTP error')
    def json(self): return self.payload

class CloudStateTests(unittest.TestCase):
    def test_new_history_is_explicit(self):
        with patch.dict(os.environ, GITHUB_TOKEN='fixture', GITHUB_REPOSITORY='owner/repo'), patch.object(cloud.requests, 'get', return_value=Response(payload={'artifacts':[]})):
            self.assertEqual(cloud.restore(), 'new_history_no_previous_artifact')

    def test_restore_only_database_and_no_credentials_to_redirect(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('market.sqlite', b'fixture state')
            z.writestr('../unwanted.txt', b'do not extract')
        responses = [Response(payload={'artifacts':[dict(id=7, expired=False, created_at='2026-10-14', archive_download_url='https://api.github.com/artifact')]}),
                     Response(status=302, headers={'Location':'https://storage.example/artifact'}), Response(content=stream.getvalue())]
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp, patch.object(cloud, 'OUT', Path(tmp)), patch.dict(os.environ, GITHUB_TOKEN='fixture', GITHUB_REPOSITORY='owner/repo'), patch.object(cloud.requests, 'get', side_effect=responses) as get:
            self.assertEqual(cloud.restore(), 'restored_artifact_7')
            self.assertEqual((Path(tmp)/'market.sqlite').read_bytes(), b'fixture state')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['market.sqlite'])
            self.assertNotIn('headers', get.call_args_list[-1].kwargs)

    def test_missing_database_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z: z.writestr('wrong.txt', 'x')
        responses = [Response(payload={'artifacts':[dict(id=7, expired=False, created_at='2026-10-14', archive_download_url='https://api.github.com/artifact')]}), Response(content=stream.getvalue())]
        with patch.dict(os.environ, GITHUB_TOKEN='fixture', GITHUB_REPOSITORY='owner/repo'), patch.object(cloud.requests, 'get', side_effect=responses):
            with self.assertRaisesRegex(ValueError, 'missing'): cloud.restore()

