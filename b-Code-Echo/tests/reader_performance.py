"""YT-014: explicit live reader timing; temporary user/context data, one existing model."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
from collections import Counter


async def run(rounds):
    from echo_app import config, reader, predict
    from echo_app.dictionary import Dictionary
    from echo_app.engine import Engine
    with tempfile.TemporaryDirectory(prefix='echo-reader-perf-') as directory:
        config.USER_DB = Path(directory) / 'user.db'
        dictionary = Dictionary()
        engine = Engine()
        models = engine.models()
        model = models[0]['name']
        template = predict.template_of(models[0]['family'])
        text = 'She went to the bank for a loan and read a book about the river.'
        history = ['The student reads a different book about science and nature every morning. '
                   'Learning English through stories helps people understand the world. ' + str(i)
                   for i in range(30)]
        original_op = engine.shared_kv.op
        operations = []
        async def traced(name, **body):
            started = time.perf_counter()
            try:
                result = await original_op(name, **body)
                tokens = result.get('prefix_tokens')
                suffix = sum(r.get('suffix_tokens', 0) for r in result.get('results', []))
                operations.append({'op':name,'ms':round((time.perf_counter()-started)*1000,2),
                                   'prefix_tokens':tokens,'suffix_tokens':suffix})
                return result
            except BaseException:
                operations.append({'op':name,'ms':round((time.perf_counter()-started)*1000,2),'failed':True})
                raise
        engine.shared_kv.op = traced
        report = {'model':model,'rounds':rounds,'trials':[]}
        try:
            runtime = await original_op('runtime')
            if runtime['active_sequences'] or runtime['active_branches']:
                raise RuntimeError('Leave active A/B work idle before benchmarking')
            for turn in range(rounds):
                for context_size in ([0,30] if turn%2==0 else [30,0]):
                    contexts = reader.Contexts(Path(directory)/f'context-{turn}-{context_size}')
                    contexts.save('probe', history[:context_size])
                    operations.clear()
                    started = time.perf_counter()
                    first_dictionary = first_selected = None
                    final = {}
                    states = {}
                    async for event in reader.events(text,dictionary,engine,model,template,
                            session='probe',contexts=contexts):
                        elapsed = round((time.perf_counter()-started)*1000,2)
                        if event.get('phase') == 'dictionary': first_dictionary = elapsed
                        elif event.get('items') and first_selected is None: first_selected = elapsed
                        for item in event.get('items',[]): states[item['start']] = dict(item)
                        if event.get('done'): final=event
                    row = {'round':turn+1,'earlier_cues':context_size,'dictionary_ms':first_dictionary,
                           'first_selected_ms':first_selected,'total_ms':elapsed,
                           'input_tokens':final.get('input_tokens'),'decisions':final.get('decisions'),
                           'states':dict(Counter(item['state'] for item in states.values())),
                           'operations':list(operations)}
                    report['trials'].append(row)
                    print(json.dumps(row),flush=True)
            report['runtime_after'] = await original_op('runtime')
            assert report['runtime_after']['active_sequences']==report['runtime_after']['active_branches']==0
            return report
        finally:
            await engine.aclose()
            dictionary._user.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--out',type=Path)
    args=parser.parse_args()
    if not args.live: parser.error('--live required')
    result=asyncio.run(run(args.rounds))
    if args.out: args.out.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
