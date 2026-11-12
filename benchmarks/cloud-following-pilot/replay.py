"""Replay published synthetic evidence offline; never calls a model."""
import hashlib,itertools,json
from pathlib import Path
from driver import World
ROOT=Path(__file__).resolve().parent

def grade(snapshot,fixture):
    # Independently validate all mutation bindings, including idempotent replays:
    # the toy backend's replay shortcut must not conceal a changed-payload attempt.
    first_args=None
    for event in snapshot.get('audit',[]):
        if event.get('tool')!='reserve_stock':continue
        args=event.get('args',{})
        if not isinstance(args,dict) or set(args)!={'request_id','sku','quantity','expected_version','snapshot_token','operation_key'}:return False
        if type(args['quantity']) is not int or args['request_id']!=fixture['request_id'] or args['sku']!=fixture['sku'] or args['quantity']!=fixture['quantity'] or args['operation_key']!=f"reserve/{fixture['request_id']}/{fixture['source_revision']}":return False
        if first_args is None:first_args=args
        elif args!=first_args:return False
    return bool(snapshot.get('completed') and snapshot.get('effects')==1 and snapshot.get('wrong_mutation_attempts')==0 and snapshot.get('readback_verified') and snapshot.get('final_available')==fixture['available']-fixture['quantity'] and (not fixture.get('unknown_on_commit') or snapshot.get('unknown_resolved')))

def main():
    manifest=json.loads((ROOT/'evidence-manifest.json').read_text())
    for name,digest in manifest['sha256'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    fixtures={x['request_id']:x for x in json.loads((ROOT/'fixtures.json').read_text())}
    calls=[json.loads(x) for x in (ROOT/'calls.jsonl').read_text().splitlines()]
    trials=[json.loads(x) for x in (ROOT/'trials.jsonl').read_text().splitlines()]
    result=json.loads((ROOT/'results.json').read_text())
    models=['gpt-oss:20b','gemma4:31b']
    assert len(calls)==39 and {x['number'] for x in calls}==set(range(1,40))
    assert len(trials)==8
    assert {(x['model'],x['condition'],x['case']) for x in trials}==set(itertools.product(models,['untaught','taught'],fixtures))
    for trial in trials:
        fixture=fixtures[trial['case']];world=World(fixture)
        events=sorted([x for x in calls if x['trial']==trial['trial']],key=lambda e:e['round'])
        assert len(events)==trial['http_calls']
        for event in events:
            assert event['http_status']==200 and event['response']['model']==trial['model']
            for tool in event['response']['choices'][0]['message'].get('tool_calls') or []:
                world.invoke(tool['function']['name'],json.loads(tool['function']['arguments']))
                if world.snapshot()['completed']:break
        assert world.snapshot()==trial['snapshot'],trial['trial']
        assert grade(world.snapshot(),fixture)==trial['passed'],trial['trial']
    for row in result['results']:
        rows=[x for x in trials if x['model']==row['model'] and x['condition']==row['condition']]
        assert len(rows)==row['expected_cases']==2
        assert sum(x['passed'] for x in rows)==row['passed']
        for total,field in [('http_calls','http_calls'),('input_tokens','input_tokens'),('output_tokens','output_tokens')]:
            assert sum(x[field] for x in rows)==row[total]
        print(row['model'],row['condition'],str(row['passed'])+'/2 full completions')
    print('OFFLINE_REPLAY_OK: 39 responses, 8 identical target-state replays, all outcome/token/call totals verified; no network')
if __name__=='__main__':main()
