from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from pulse.store.artifact_publisher import ArtifactPublisher, STAGE, REGISTER, PROMOTE
from pulse.wire.composer import Publication
from pulse.wire.publication import prepare


def package():
    doc={"schema":"pulse.live_price.v1","mapping_version":"sha256:"+"a"*64,"sequence":1}
    descriptor,blobs=prepare(doc,brotli_compress=lambda body,quality:b"compressed")
    return Publication("live_price_v1",doc["mapping_version"],1000,16000,(("ticker",1),),
        json.dumps(descriptor).encode(),((descriptor["logical_sha256"],json.dumps(descriptor).encode()),),tuple(blobs.items()))


class ArtifactPublisherTests(unittest.TestCase):
    def test_snapshot_lane_is_separately_fenced_but_keeps_shared_immutable_namespace(self):
        client=Mock()
        main=ArtifactPublisher(client,"1"*32,"sha256:"+"a"*64)
        slow=ArtifactPublisher(client,"1"*32,"sha256:"+"a"*64,lane="snapshot")
        self.assertNotEqual(main.lease,slow.lease)
        self.assertNotEqual(main.fence,slow.fence)
        self.assertEqual(main.inputs,slow.inputs)
        self.assertEqual(main.prefix,slow.prefix)
        client.eval.assert_not_called()
        with self.assertRaises(ValueError):ArtifactPublisher(client,"1"*32,"sha256:"+"a"*64,lane="arbitrary")

    def test_history_uses_snapshot_retention_and_stages_before_promoting(self):
        publisher=self.publisher();p=replace(package(),channel="history:0",expires_ms=181000)
        publisher.publish([p],expected_revisions={p.channel:0})
        calls=publisher.client.eval.call_args_list
        self.assertTrue(all(c.args[-1]==180000 for c in calls if c.args[0]==STAGE))
        promotion=next(c for c in calls if c.args[0]==PROMOTE)
        self.assertEqual(json.loads(promotion.args[-3])[0]["grace"],120000)
        for channel in ("history:35","history:-1","history:foo"):
            with self.assertRaises(ValueError):publisher.publish([replace(p,channel=channel)],expected_revisions={channel:0})

    def test_full_release_history_twelve_minute_limit_is_not_available_to_live(self):
        publisher=self.publisher();p=replace(package(),channel="history:34",expires_ms=721000)
        publisher.publish([p],expected_revisions={p.channel:0})
        plan=json.loads(publisher.client.eval.call_args_list[-1].args[-3])[0]
        self.assertTrue(plan["history"])
        publisher=self.publisher()
        with self.assertRaisesRegex(ValueError,"expiry_refused"):
            publisher.publish([replace(p,channel="live_price_v1")],expected_revisions={"live_price_v1":0})
        publisher.client.eval.assert_not_called()

    def test_live_only_artifacts_and_witnesses_have_bounded_stage_retention(self):
        publisher=self.publisher();p=package()
        publisher.register_source("price:live",b"{}",ttl_ms=60000)
        publisher.publish([p],expected_revisions={p.channel:0})
        calls=publisher.client.eval.call_args_list
        stages=[c for c in calls if c.args[0]==STAGE]
        self.assertEqual(stages[0].args[-1],60000,"source registration keeps its own retention")
        self.assertTrue(all(c.args[-1]==15000 for c in stages[1:]))
        promotion=next(c for c in calls if c.args[0]==PROMOTE)
        self.assertEqual(json.loads(promotion.args[-3])[0]["grace"],15000)
        self.assertEqual(json.loads(promotion.args[-3])[0]["source_only"],{})
        self.assertIn("p.grace==15000 and p.expiry>p.source+15000",PROMOTE)

    def test_snapshot_stage_and_promotion_grace_remain_longer(self):
        from pathlib import Path
        from pulse.wire.composer import compose
        value=json.loads((Path(__file__).parents[1]/"docs/contracts/fixtures/snapshot.valid.1H.json").read_text())
        publisher=self.publisher();publisher.mapping_version=value["mapping_version"]
        p=compose(value,validate=lambda v:None,brotli_compress=lambda body,q:b"compressed",
            input_revisions={"ticker":1},now_ms=value["generated_ms"])
        publisher.publish([p],expected_revisions={p.channel:0})
        calls=publisher.client.eval.call_args_list
        self.assertTrue(all(c.args[-1]==180000 for c in calls if c.args[0]==STAGE))
        promotion=next(c for c in calls if c.args[0]==PROMOTE)
        self.assertEqual(json.loads(promotion.args[-3])[0]["grace"],120000)
        plan=json.loads(promotion.args[-3]);private=plan[0]['source_only']
        self.assertTrue(private)
        keys=list(promotion.args[2:2+promotion.args[1]])
        for index,flag in private.items():
            self.assertTrue(flag)
            self.assertEqual(keys[2+len(plan)+int(index)-1],publisher.artifacts.key('blob','sha256:'+'b'*64))
        self.assertIn("source_only[tostring(i-2-#plan)] and source_keep or keep",PROMOTE)

    def test_source_blob_shared_with_public_artifact_keeps_public_grace(self):
        publisher=self.publisher();p=package()
        publisher.client.hget.return_value=p.blobs[0][0].encode()
        publisher.publish([p],expected_revisions={p.channel:0})
        plan=json.loads(publisher.client.eval.call_args_list[-1].args[-3])
        self.assertEqual(plan[0]['source_only'],{})

    def publisher(self):
        client=Mock()
        client.info.return_value={"used_memory":1000000,"maxmemory_policy":"noeviction"}
        client.eval.side_effect=lambda script,*args: 1 if script not in (STAGE,REGISTER,PROMOTE) else (
            [b"staged"] if script==STAGE else [b"registered",b"1"] if script==REGISTER else [b"published"])
        client.hget.return_value=b"sha256:"+b"b"*64;client.strlen.return_value=10
        value=ArtifactPublisher(client,"1"*32,"sha256:"+"a"*64);value.owner=b"owner"
        return value

    def test_prepared_set_staged_before_atomic_promotion(self):
        publisher=self.publisher();p=package()
        self.assertEqual(publisher.publish([p],expected_revisions={p.channel:0})["status"],"published")
        calls=publisher.client.eval.call_args_list
        self.assertEqual(calls[-1].args[0],PROMOTE)
        self.assertTrue(any(call.args[0]==STAGE for call in calls[:-1]))
        for call in calls:
            if call.args[0] in (STAGE,PROMOTE):
                for key in call.args[2:2+call.args[1]]: self.assertTrue(key.startswith("pulse:stg:"))
        publisher.client.delete.assert_not_called();publisher.client.scan.assert_not_called()

    def test_invalid_mapping_blob_or_pointer_refuses_before_writes(self):
        p=package()
        bad=(replace(p,mapping_version="sha256:"+"c"*64),
             replace(p,blobs=((p.blobs[0][0],b"tampered"),)),
             replace(p,pointer=b"{}"),replace(p,pointer=b"x"*131073),
             replace(p,channel="legacy"),replace(p,input_revisions=(("ticker",0),)))
        for candidate in bad:
            publisher=self.publisher()
            with self.subTest(candidate=candidate.channel):
                with self.assertRaises((ValueError,KeyError)):
                    publisher.publish([candidate],expected_revisions={candidate.channel:0})
                publisher.client.eval.assert_not_called()

    def test_stage_lease_loss_never_promotes(self):
        p=package();publisher=self.publisher()
        publisher.client.eval.return_value=[b"fenced_out"]
        publisher.client.eval.side_effect=lambda script,*args: [b"fenced_out"] if script==STAGE else 1
        with self.assertRaisesRegex(RuntimeError,"fenced_out"):
            publisher.publish([p],expected_revisions={p.channel:0})
        self.assertFalse(any(c.args[0]==PROMOTE for c in publisher.client.eval.call_args_list))

    def test_source_register_and_scope_rejection(self):
        publisher=self.publisher()
        rev,identity=publisher.register_source("ticker",b'{"time":1000}')
        self.assertEqual(rev,1);self.assertTrue(identity.startswith("sha256:"))
        publisher.client.eval.reset_mock()
        for key in ("pulse:prod:forbidden","pulse:stg:{other}:publication:blob:x"):
            with self.assertRaises(ValueError): publisher.stage(key,b"x")
        publisher.client.eval.assert_not_called()


if __name__=="__main__": unittest.main()
