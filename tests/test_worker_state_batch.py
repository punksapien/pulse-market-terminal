import json
from types import SimpleNamespace
import unittest

from pulse.store.worker_state import WorkerState,READ


class Client:
    def __init__(self,records): self.records=records;self.batches=[]
    def pipeline(self,transaction):
        assert transaction is False
        client=self
        class Pipe:
            def __init__(self): self.names=[]
            def eval(self,script,count,key,name):
                assert script==READ and count==1 and key.startswith("pulse:stg:")
                self.names.append(name)
            def execute(self):
                client.batches.append(self.names)
                return [client.records.get(n,[b"missing"]) for n in self.names]
        return Pipe()


class StateBatchTests(unittest.TestCase):
    def state(self,records):
        self.client=Client(records)
        return WorkerState(SimpleNamespace(prefix="pulse:stg:{"+"a"*32+"}:publication:",client=self.client))

    def test_fifty_records_need_four_bounded_roundtrips(self):
        names=[f"P{i}USDT" for i in range(50)]
        state=self.state({n:[b"available",json.dumps({"pair":n}).encode()] for n in names})
        result=state.read_many(names)
        self.assertEqual(len(result),50)
        self.assertEqual([len(b) for b in self.client.batches],[16,16,16,2])
        self.assertEqual(state.io_stats()['record_read_bytes'],sum(len(json.dumps({'pair':n}).encode()) for n in names))
        stats=state.io_stats();stats['record_read_bytes']=0
        self.assertGreater(state.io_stats()['record_read_bytes'],0)

    def test_missing_is_not_zero_and_bad_size_or_shape_refuses(self):
        state=self.state({})
        self.assertIsNone(state.read("BTCUSDT"))
        for value in ([b"size_refused"],[b"available",b"[]"],[b"available",b"x"*131073]):
            state=self.state({"BTCUSDT":value})
            with self.assertRaises(ValueError):state.read("BTCUSDT")
        for names in ([None],[{}],["BTCUSDT"]*2,[str(i) for i in range(1025)]):
            with self.assertRaises(ValueError):state.read_many(names)

    def test_all_pair_reads_keep_sixteen_command_bound(self):
        names=[f"P{i}USDT" for i in range(505)]
        state=self.state({})
        self.assertEqual(len(state.read_many(names)),505)
        self.assertEqual(len(self.client.batches),32)
        self.assertTrue(all(len(batch)<=16 for batch in self.client.batches))
