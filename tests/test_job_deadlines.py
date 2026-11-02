from unittest import TestCase
from pulse.collectors.job_deadlines import priority,providers

NOW=1800000000000


class DeadlineTests(TestCase):
    def test_all_deadlines_missing_clocks_and_units(self):
        for kind,key,margin in [('oi','last_hot_ms',90000),('instrument','instrument_ms',120000),
                ('legacy','legacy_source_ms',900000),('funding','nft',900000),
                ('references','references_prefetch_until_ms',-60000)]:
            for value,end in [(NOW,NOW+margin),(None,NOW-1),(True,NOW-1),('1',NOW-1)]:
                r={key:value};before=dict(r)
                self.assertEqual(priority((NOW-1,'A',kind),r),(end,NOW-1,'A',kind))
                self.assertEqual(r,before)

    def test_period_boundary_does_not_starve_earlier_global_deadline(self):
        period=(NOW,'P','period_4H');global_=(NOW+420000,'G','global')
        p={'ls_periods':{'4H':{'expires_ms':NOW+900000}}}
        g={'global_source_ms':NOW-300000,'global_tip_refreshed_ms':NOW+120000}
        self.assertLess(priority(global_,g),priority(period,p))
        self.assertEqual(priority(period,p)[0],NOW+900000)
        self.assertEqual(priority(global_,g)[0],NOW+480000)
        self.assertEqual(priority(period,{'ls_periods':{'4H':{'expires_ms':True}}})[0],NOW)

    def test_top_deadline_and_baseline_hole_are_distinct(self):
        item=(NOW,'A','detail');r={'top_source_ms':NOW,'top_refreshed_ms':NOW+10000}
        self.assertEqual(priority(item,r)[0],NOW+970000)
        r['derived']={'changes':{'24H':{'status':'unavailable'}}}
        self.assertEqual(priority(item,r)[0],NOW)
        for kind,want in [('oi',('binance',)),('period_1H',('binance',)),
                ('instrument',('coindcx',)),('references',('coindcx',)),
                ('legacy',('binance','coindcx')),('funding',('binance','coindcx'))]:
            self.assertEqual(providers(kind),want)
