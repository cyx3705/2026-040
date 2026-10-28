"""ECHO-SVC-001: explicitly invoked live acceptance, using a temporary Echo database."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

import httpx


def events(response):
    response.raise_for_status()
    return [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith('data:')]


async def verify(require_shared=False):
    from echo_app import config
    if not config.DICT_DB.is_file():
        raise RuntimeError('Set ECHO_DATA_DIR to the existing dictionary/data directory before running this check.')
    report = {'checks': [], 'isolated_user_database': True}
    with tempfile.TemporaryDirectory(prefix='echo-external-live-') as directory:
        root = Path(directory)
        config.DATA_DIR = root
        config.USER_DB = root / 'user.db'
        config.READER_CONTEXT_DIR = root / 'reader-context'
        from echo_app import server
        try:
            async with server.lifespan(server.app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                        base_url='http://127.0.0.1:8040', timeout=180) as client:
                    async def check(name, path, payload=None):
                        started = time.monotonic()
                        response = await (client.get(path) if payload is None else client.post(path, json=payload))
                        response.raise_for_status()
                        report['checks'].append({'name':name,'elapsed_ms':round((time.monotonic()-started)*1000)})
                        print(json.dumps(report['checks'][-1]),flush=True)
                        return response
                    status = (await check('status','/api/status')).json()
                    assert status['state'] == 'ready', status['detail']
                    report['model'] = status['model']
                    if require_shared:
                        assert status['kv']['supported'] and status['kv']['fork_at'], status['kv']
                        report['shared_runtime_before'] = await server.engine.shared_kv.op('runtime')
                    for text in ['I want to learn ', 'I want to learn ', 'I want to learn Eng']:
                        result = (await check('prediction','/api/suggest',{'messages':[],'text':text,'level':'A2'})).json()
                        assert result and any(item['kind'] == 'word' for item in result), 'No model word candidates'
                    result = (await check('input_translation','/api/input-translation',{
                        'messages':[],'before':'I eat an ','chinese':'苹果','after':' every day.'})).json()
                    assert result['word'] and 'apple' in result['word'].lower(), result
                    stream = events(await check('reader_selection','/api/reader/gloss',{
                        'text':'She deposited money at the bank.', 'session':'echo-live-finance'}))
                    assert stream[-1].get('done'), 'Reader stream incomplete'
                    items = {item['start']:item for event in stream for item in event.get('items',[])}
                    bank = next(item for item in items.values() if item['word'] == 'bank')
                    assert bank['state'] == 'normal' and '银行' in bank['gloss'], bank
                    if require_shared:
                        assert bank.get('decision_steps') == 1 and bank.get('shared_prefix_tokens',0) > 0, bank
                    stream = events(await check('chat','/api/chat',{
                        'messages':[{'role':'user','content':'I like reading. Please ask me about books.'}],
                        'level':'A1','length':1}))
                    assert not any('error' in event for event in stream), stream
                    done = next(event for event in stream if event.get('done'))
                    assert any(event.get('delta') for event in stream)
                    conversation = (await client.get(f"/api/conversations/{done['conversation_id']}")).json()
                    assert conversation['messages'][-1]['glosses'] == done['glosses']
                    title = (await check('title','/api/conversations/'+str(done['conversation_id'])+'/title',{})).json()
                    assert title.get('title'), 'Empty title'
                    if require_shared:
                        runtime = await server.engine.shared_kv.op('runtime')
                        assert runtime['active_sequences'] == runtime['active_branches'] == 0, runtime
                        assert runtime['forks'] == runtime['releases'], runtime
                        assert runtime['forks'] > report['shared_runtime_before']['forks'], runtime
                        report['shared_runtime_after'] = runtime
        finally:
            server.store._con.close()
            server.dictionary._user.close()
    report['passed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Authorize requests to the configured running service')
    parser.add_argument('--out',type=Path)
    parser.add_argument('--require-shared-kv',action='store_true',help='Require actual shared sequence selection and zero leaked leases')
    args=parser.parse_args()
    if not args.live:
        parser.error('--live is required; this script sends real inference requests')
    report=asyncio.run(verify(args.require_shared_kv))
    if args.out:
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
