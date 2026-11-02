"""ADR-0092 bounded disposable publication proof; Preview identity gate, no deletes."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pulse_staging_gate import connect_staging
from pulse.store.artifact_publisher import ArtifactPublisher
from pulse.store.runtime_document import RuntimeDocument
from pulse.store.published_artifacts import PublishedArtifacts
from pulse.wire.live_price import compose_live_price
from pulse.wire.identity import digest
from pulse.wire.validation import live
from pulse.wire.composer import compose
from pulse.http.wire_reader import response


def run(cli):
    import brotli
    client,identity=connect_staging(cli,approved_plaintext=True)
    run_id=uuid.uuid4().hex
    universe=[dict(id=0,pair_id="B-BTC_USDT",symbol="BTCUSDT",funding_hours=8)]
    mapping=digest(universe);publisher=ArtifactPublisher(client,run_id,mapping)
    def clock():
        s,us=client.time();return s*1000+us//1000
    checks=[]
    def check(name,condition):
        if not condition: raise RuntimeError("spike_failed:"+name)
        checks.append(name)
    try:
        before=client.info("memory")["used_memory"]
        publisher.acquire()
        now=clock();source=dict(ts=now,prices={"B-BTC_USDT":dict(mkt="BTCUSDT",ls="100.25",btST=now)})
        revision,witness=publisher.register_source("synthetic",json.dumps(source).encode())
        gate=lambda v:live(v,universe,mapping)
        value=compose_live_price(universe,source,mapping_version=mapping,batch_version=witness,
            sequence=now,now_ms=now,price_max_age_ms=60000,validate=gate)
        package=compose(value,validate=gate,brotli_compress=lambda body,q:brotli.compress(body,quality=q),
                        input_revisions={"synthetic":revision},now_ms=now)
        outcome=publisher.publish([package],expected_revisions={package.channel:0})
        check("publish",outcome["status"]=="published")
        args=dict(environment="preview",now_ms=clock(),
            store_factory=lambda channel:RuntimeDocument(client,run_id,channel,mapping),
            artifacts_factory=lambda:PublishedArtifacts(client,run_id,mapping))
        read=response({"live":["price"]},accept_encoding="br",**args)
        check("real_reader_brotli",read.status==200 and json.loads(brotli.decompress(read.body))==value)
        data=RuntimeDocument(client,run_id,package.channel,mapping).read(now_ms=clock())
        check("revision_one",data["revision"]==1)
        old=publisher.owner;publisher.owner=b"0:stale"
        try: publisher.publish([package],expected_revisions={package.channel:1})
        except RuntimeError: check("old_writer_refused",True)
        else: check("old_writer_refused",False)
        publisher.owner=old
        publisher.register_source("synthetic",b'{"updated":true}')
        try: publisher.publish([package],expected_revisions={package.channel:1})
        except RuntimeError as exc: check("input_change_refused","input_revision_conflict" in str(exc))
        else: check("input_change_refused",False)
        check("failed_publish_preserves_pointer",RuntimeDocument(client,run_id,package.channel,mapping).read(now_ms=clock())["payload"]==data["payload"])
        check("all_blobs_have_bounded_ttl",all(0<client.pttl(publisher.artifacts.key("blob",key))<=300000 for key,_ in package.blobs))
        return dict(status="passed",run_id=run_id,checks=checks,script_timings_us=publisher.script_timings,
            memory_before=before,memory_after=client.info("memory")["used_memory"],
            production_access=False,deletions=0,exchange_calls=0,synthetic_only=True,
            keys=[publisher.lease,publisher.fence,publisher.inputs,
                  RuntimeDocument(client,run_id,package.channel,mapping).data]+
                 [publisher.artifacts.key("blob",key) for key,_ in package.blobs]+
                 [publisher.artifacts.key("artifact",key) for key,_ in package.descriptors],
            note="Artifacts expire automatically; no reader is configured to this synthetic run.")
    finally: client.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--vercel-cli",required=True)
    args=parser.parse_args()
    try: print(json.dumps(run(args.vercel_cli)))
    except Exception as exc:
        print(json.dumps({"status":"failed","error_type":type(exc).__name__,
                          "reason":str(exc) if isinstance(exc,(ValueError,RuntimeError)) else "transport_failure"}))
        raise SystemExit(1) from None
