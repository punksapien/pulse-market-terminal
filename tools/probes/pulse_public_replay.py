"""Finite staging replay of PUBLIC published documents, never production Redis.

ADR-0197. Original timestamps, generation and field expiry are preserved.
No exchange calls, scans, deletions, collectors or production writes.
"""
import argparse, gzip, hashlib, json, ssl, sys, time
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from pathlib import Path
from urllib.request import Request, urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from preview_redis_access import connect
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.runtime_document import RuntimeDocument
from pulse.wire.composer import compose
from pulse.wire.compact import decode, raw
from pulse.wire.validation import snapshot as validate_snapshot, live as validate_live
from pulse.wire.publication import byte_digest
from pulse.wire.live_price import compose_live_price

RUN="29c01970000000000000000000000001"
ORIGIN="https://dcxpulse.vercel.app"
MAPPING="sha256:6a02d7759bfcc2c27159385e0e03119a2ea3d8112723c0a6ceeca91cadca4fc8"

def gap(exc,**fields):
    result=dict(status='replay_gap',error_type=type(exc).__name__,at_ms=time.time_ns()//1000000,**fields)
    if hasattr(exc,'code') and type(exc.code) is int:result['http']=exc.code
    if isinstance(exc,RuntimeError) and re.fullmatch(r'[a-z0-9_:]{1,100}',str(exc)):result['detail']=str(exc)
    print(json.dumps(result),flush=True)

def fetch(query, *, timeout=3):
    context=ssl.create_default_context()
    context.load_verify_locations('/private/tmp/pulse-probe-ca.pem')
    request=Request(ORIGIN+"/api/pulse?"+query,headers={"Accept-Encoding":"gzip"})
    with urlopen(request,context=context,timeout=timeout) as response:
        if response.url!=request.full_url:raise ValueError("public_replay_redirect")
        body=response.read(1114113)
        if len(body)>1114112:raise ValueError("public_replay_size")
        if response.headers.get('Content-Encoding')=='gzip':body=gzip.decompress(body)
    if len(body)>1114112:raise ValueError("public_replay_expanded_size")
    return json.loads(body)

class ReplayPublisher(ArtifactPublisher):
    def preflight(self,extra=0):
        info=self.client.info('memory');used=info.get('used_memory')
        if type(used) is not int or used+extra+1000000>=21000000 or info.get('maxmemory_policy')!='noeviction':
            raise RuntimeError('isolated_preview_21mb_gate')

def publish(publisher, value, name, witness):
    import brotli
    started=time.monotonic()
    now=time.time_ns()//1000000
    if value['mapping_version']!=MAPPING or now>=value['expires_ms']:raise ValueError('public_replay_expired_or_mapping')
    validate=(validate_snapshot if value['schema']=='pulse.snapshot.v1' else lambda doc:validate_live(doc,witness['universe'],MAPPING))
    package=compose(value,validate=validate,brotli_compress=lambda body,q:brotli.compress(body,quality=q),
        input_revisions={name:1},now_ms=now,include_identity=False)
    # ADR-0197 amendment: compress before acquiring the bounded writer lease.
    publisher.acquire()
    try:
        if time.time_ns()//1000000>=package.expires_ms:raise ValueError('public_replay_expired_before_stage')
        revision,_=publisher.register_source(name,raw(witness),ttl_ms=60000)
        package=replace(package,input_revisions=((name,revision),))
        current=RuntimeDocument(publisher.client,RUN,package.channel,MAPPING).read(now_ms=time.time_ns()//1000000)
        result=publisher.publish([package],expected_revisions={package.channel:current.get('revision',0)})
    finally:publisher.release()
    timings=publisher.script_timings[-(len(package.blobs)+len(package.descriptors)+2):]
    print(json.dumps({'status':'replay_publication','channel':package.channel,'payload_bytes':package.allocation_bytes,
        'at_ms':time.time_ns()//1000000,'source_generated_ms':package.generated_ms,'source_expires_ms':package.expires_ms,
        'elapsed_ms':round((time.monotonic()-started)*1000),
        'max_lua_us':max((elapsed for _,elapsed in timings),default=None),
        'sample_blob_ttl_ms':publisher.client.pttl(publisher.artifacts.key('blob',package.blobs[0][0]))}),flush=True)
    return result

def price_document(value,universe):
    stamp=value['inputs']['price:batch']['source_ms'];prices={}
    if len(value['rows'])!=len(universe):raise ValueError('public_replay_membership')
    for row,meta in zip(value['rows'],universe):
        if row['id']!=meta['id']:raise ValueError('public_replay_row_id')
        volume=row.get('volume',{})
        if volume.get('status')=='available' and volume['source_ms']!=stamp:raise ValueError('public_replay_volume_clock')
        prices[meta['pair_id']]={'mkt':meta['symbol'],'ls':row.get('price'),'btST':row.get('source_ms'),
            'v':volume.get('value') if volume.get('status')=='available' else None}
    payload={'ts':stamp,'prices':prices}
    return compose_live_price(universe,payload,mapping_version=MAPPING,batch_version=byte_digest(raw(payload)),
        sequence=value['sequence'],now_ms=value['generated_ms'],price_max_age_ms=60000,
        validate=lambda doc:validate_live(doc,universe,MAPPING))

def snapshot_cycle(publisher,horizon,seen):
    manifest=fetch('horizon='+horizon,timeout=6)
    if manifest.get('mapping_version')!=MAPPING:raise ValueError('public_replay_mapping')
    if seen.get(horizon)==manifest['generation']:return
    parts={}
    for name,descriptor in manifest['parts'].items():
        body=fetch('artifact='+descriptor['logical_sha256'],timeout=6)
        if byte_digest(raw(body))!=descriptor['logical_sha256']:raise ValueError('public_replay_digest')
        parts[name]=body
    value=decode(parts)
    if len(value['universe'])!=350:raise ValueError('public_replay_membership')
    publish(publisher,value,'public.'+horizon,manifest)
    seen[horizon]=manifest['generation']

def live_cycle(publisher,universe,name):
    value=fetch('live=price' if name=='price' else 'live=1')
    if name=='price':value=price_document(value,universe)
    publish(publisher,value,'public.'+name,{'universe':universe,'logical_sha256':byte_digest(raw(value))})

def run_lanes(publishers,universe,seconds,*,snapshot_work=snapshot_cycle,live_work=live_cycle):
    """Two independent clients and leases: slow snapshots cannot block live updates."""
    until=time.monotonic()+seconds;stop=Event()
    stats={'snapshot':{'completed_cycles':0,'failed_cycles':0},'price':{'publications':0,'failed_cycles':0,'max_gap_ms':0},
           'funding':{'publications':0,'failed_cycles':0,'max_gap_ms':0}}
    def snapshots():
        seen={};index=0
        while not stop.is_set() and time.monotonic()<until:
            h=('24H','1H','4H')[index%3];index+=1
            try:
                snapshot_work(publishers['snapshot'],h,seen);stats['snapshot']['completed_cycles']+=1
            except Exception as exc:
                stats['snapshot']['failed_cycles']+=1;gap(exc,lane='snapshot',horizon=h)
            stop.wait(max(0,min(10,until-time.monotonic())))
    def live():
        due={'price':0,'funding':0};last={}
        while not stop.is_set() and time.monotonic()<until:
            for name,period in (('price',2),('funding',5)):
                now=time.monotonic()
                if now>=until:break
                if now<due[name]:continue
                try:
                    live_work(publishers['live'],universe,name);at=time.monotonic()
                    stats[name]['publications']+=1
                    if name in last:stats[name]['max_gap_ms']=max(stats[name]['max_gap_ms'],round((at-last[name])*1000))
                    last[name]=at
                except Exception as exc:
                    stats[name]['failed_cycles']+=1;gap(exc,lane='live',channel=name)
                due[name]=max(now+period,time.monotonic()+.05)
            stop.wait(max(.05,min(*due.values(),until)-time.monotonic()))
    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='pulse-replay') as pool:
        futures=[pool.submit(snapshots),pool.submit(live)]
        try:
            for future in futures:future.result()
        finally:stop.set()
    return stats

def run(cli,seconds):
    fixture=json.loads((Path(__file__).resolve().parents[2]/'docs/fixtures/release_top350_2026-10-28.json').read_text())
    universe=fixture['universe']
    if fixture['mapping_version']!=MAPPING or byte_digest(raw(universe))!=MAPPING or len(universe)!=350:raise ValueError('public_replay_frozen_membership')
    clients={}
    try:
        for lane in ('snapshot','live'):clients[lane]=connect(cli)
        publishers={lane:ReplayPublisher(client,RUN,MAPPING,lane=lane) for lane,client in clients.items()}
        stats=run_lanes(publishers,universe,seconds)
        receipt={'status':'replay_observed','seconds':seconds,'stats':stats,
            'scope':'isolated_30mb_preview','at_ms':time.time_ns()//1000000}
        try:receipt['used_memory']=clients['live'].info('memory').get('used_memory')
        except Exception as exc:receipt['final_memory_error_type']=type(exc).__name__
        print(json.dumps(receipt),flush=True)
    finally:
        for client in clients.values():client.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--vercel-cli',required=True);parser.add_argument('--seconds',type=int,default=600)
    args=parser.parse_args()
    if not 1<=args.seconds<=3600:raise ValueError('replay_duration')
    try:run(args.vercel_cli,args.seconds)
    except Exception as exc:print(json.dumps({'status':'replay_stopped','error_type':type(exc).__name__}));raise SystemExit(1) from None
