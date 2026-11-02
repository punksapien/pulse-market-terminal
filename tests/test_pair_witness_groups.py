"""ADR-0132 bounded source witnesses for all-pair publication."""
from unittest import TestCase

from pulse.collectors.preview_compose import pair_witness_groups


class PairWitnessGroupsTests(TestCase):
    def test_boundaries_and_lossless_membership(self):
        for count in (1,16,17,50,504,505,1024):
            with self.subTest(count=count):
                universe=[dict(symbol=str(i)) for i in range(count)]
                records=[dict(last_collected_ms=i) for i in range(count)]
                groups=list(pair_witness_groups(universe,records))
                self.assertEqual([item for _,g in groups for item in g],list(zip(universe,records)))
                self.assertTrue(all(1<=len(g)<=16 for _,g in groups))
                self.assertEqual(len({name for name,_ in groups}),len(groups))
                # Three global sources plus 3 horizons x 7 parts x 4 blobs.
                self.assertLessEqual(len(groups)+3+84,160)
                self.assertEqual(groups,list(pair_witness_groups(universe,records)))

    def test_invalid_sizes_fail_closed(self):
        for universe,records in (([],[]),([{}],[]),([{}]*1025,[{}]*1025)):
            with self.assertRaisesRegex(ValueError,"pair_witness_universe_refused"):
                list(pair_witness_groups(universe,records))
