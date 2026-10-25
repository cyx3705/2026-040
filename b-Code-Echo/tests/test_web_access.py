"""NET-001: remote access, streaming and isolation without a GPU or user DB."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from echo_app import config, web_access


class SharedAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = FastAPI()
        self.app.add_middleware(web_access.ExtensionAccessMiddleware)
        self.key = 'fixture-access-' + 'x' * 32
        self.app.state.share_key = self.key
        self.known = set()

        @self.app.get('/api/status')
        async def status():
            return {'model': 'shared-fixture'}

        @self.app.get('/api/reader/learned')
        async def learned():
            return sorted(self.known)

        @self.app.post('/api/learned')
        async def learn(body: dict):
            if body['learned']:
                self.known.add(body['lemma'])
            else:
                self.known.discard(body['lemma'])
            return {'ok': True}

        @self.app.post('/api/reader/gloss')
        async def gloss():
            async def stream():
                yield 'data: {"phase":"dictionary"}\n\n'
                yield 'data: {"done":true}\n\n'
            return StreamingResponse(stream(), media_type='text/event-stream')

        @self.app.get('/api/conversations')
        async def conversations():
            return ['private']

    def client(self, host='192.168.1.2', base='http://192.168.1.10:8040', key=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(host, 1234)),
            base_url=base, headers={} if key is None else {'Authorization': f'Bearer {key}'})

    async def test_remote_requires_correct_key_even_with_loopback_host_or_forwarded_headers(self):
        for base in ('http://192.168.1.10:8040', 'http://127.0.0.1:8040'):
            for key in (None, 'wrong'):
                async with self.client(base=base, key=key) as client:
                    result = await client.get('/api/status', headers={'X-Forwarded-For': '127.0.0.1'})
                    self.assertEqual(result.status_code, 401)
        self.app.state.share_key = None
        async with self.client(key=self.key) as client:
            self.assertEqual((await client.get('/api/status')).status_code, 401)

    async def test_two_remote_clients_share_learned_state_and_stream(self):
        async with self.client(key=self.key) as reader, self.client(host='192.168.1.3', key=self.key) as youtube:
            self.assertEqual((await reader.get('/api/status')).json()['model'], 'shared-fixture')
            self.assertEqual((await reader.post('/api/learned', json={'lemma': 'bank', 'learned': True})).status_code, 200)
            self.assertEqual((await youtube.get('/api/reader/learned')).json(), ['bank'])
            await youtube.post('/api/learned', json={'lemma': 'bank', 'learned': False})
            self.assertEqual((await reader.get('/api/reader/learned')).json(), [])
            result = await youtube.post('/api/reader/gloss', json={'text': 'bank'})
            self.assertEqual(result.text.count('data: '), 2)
            self.assertIn('text/event-stream', result.headers['content-type'])

    async def test_remote_key_cannot_open_desktop_admin_or_input_endpoints(self):
        async with self.client(key=self.key) as client:
            for path in ('/', '/api/conversations', '/api/input-mode', '/api/models', '/docs', '/openapi.json'):
                self.assertEqual((await client.get(path)).status_code, 403, path)
            self.assertEqual((await client.post('/api/status')).status_code, 403)

    async def test_local_ui_works_but_foreign_origin_and_rebound_host_do_not_bypass(self):
        async with self.client(host='127.0.0.1', base='http://127.0.0.1:8040') as client:
            self.assertEqual((await client.get('/api/conversations')).json(), ['private'])
            self.assertEqual((await client.get('/api/status', headers={'Origin': 'http://foreign.test'})).status_code, 401)
        async with self.client(host='127.0.0.1', base='http://foreign.test') as client:
            self.assertEqual((await client.get('/api/conversations')).status_code, 401)

    def test_key_is_persistent_and_invalid_or_short_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'key.txt'
            key = web_access.load_key(path)
            self.assertGreaterEqual(len(key), 32)
            self.assertEqual(web_access.load_key(path), key)
            path.write_text('short')
            with self.assertRaises(ValueError):
                web_access.load_key(path)
            for key in ('', 'x' * 31, 'x' * 32 + '\n', '汉' * 32):
                with self.assertRaises(ValueError):
                    web_access.validate_key(key)
            with patch.object(config, 'WEB_HOST', '127.0.0.1'):
                web_access.configure(self.app)
                self.assertIsNone(self.app.state.share_key)


if __name__ == '__main__':
    unittest.main()
