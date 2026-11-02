import importlib.util
from pathlib import Path
import unittest
import tempfile,io,zipfile,hashlib
from unittest.mock import Mock

spec=importlib.util.spec_from_file_location("calibration",Path(__file__).parents[1]/"tools/probes/l2_archive_calibration.py")
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)

class CalibrationTests(unittest.TestCase):
    def test_oi_quantity_not_notional_and_log_symmetry(self):
        rows=[]
        for hour,q,ratio in ((0,"100","2"),(1,"120","0.5")):
            rows.append(dict(symbol="BTCUSDT",create_time=f"2026-09-01 {hour:02d}:00:00",sum_open_interest=q,count_long_short_ratio=ratio))
        result=c.observations(rows,[],"BTCUSDT")
        self.assertEqual(result["long_short"][0][1],result["long_short"][1][1])
        self.assertEqual(result["oi_1h"][0][1],.2)
        self.assertEqual(result["oi_4h"],[])
        with self.assertRaisesRegex(ValueError,"archive_conflict"):
            c.observations(rows+[{**rows[0],"sum_open_interest":"999"}],[],"BTCUSDT")

    def test_funding_native_hours_anchor_and_consecutive_delta(self):
        rows=[dict(calc_time=str(c.START+i*3600000),funding_interval_hours="1",last_funding_rate=rate)
              for i,rate in ((0,"0.0000125"),(1,"0.000025"),(3,"0.00002"))]
        result=c.observations([],rows,"BTCUSDT")
        self.assertEqual(result["funding"][0][1],0)
        self.assertEqual(result["funding"][1][1],1)
        self.assertEqual(len(result["funding_change"]),1)
        self.assertEqual(result["funding_change"][0][1],1)

    def test_degenerate_cuts_not_silently_repaired_and_ties_go_up(self):
        self.assertEqual(c.quantiles([0]*100),[0,0,0])
        out=c.occupancy([(1,0,1),(2,1,1),(3,2,1),(4,3,1)],[1,2,3])
        self.assertEqual(out["bands"],[1,1,1,1]);self.assertEqual(out["band_changes"],3)

    def test_missing_ls_does_not_discard_valid_oi(self):
        rows=[dict(symbol="COPPERUSDT",create_time=f"2026-09-01 {h:02d}:00:00",sum_open_interest=q,count_long_short_ratio="")
              for h,q in ((0,"100"),(1,"120"))]
        diagnostics={};result=c.observations(rows,[],"COPPERUSDT",diagnostics)
        self.assertEqual(result["long_short"],[])
        self.assertEqual(result["oi_1h"][0][1],.2)
        self.assertEqual(diagnostics["count_long_short_ratio_unavailable"],2)

    def test_offline_cache_hash_verification_refuses_tamper_without_network(self):
        name="BTCUSDT-fundingRate-2026-09.zip";buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,"w") as z:z.writestr("funding.csv","calc_time,funding_interval_hours,last_funding_rate\n")
        data=buffer.getvalue()
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/name;target.write_bytes(data)
            archive=c.Archive(None,directory,10,{name:hashlib.sha256(data).hexdigest()})
            archive.get=Mock(side_effect=AssertionError("no network"))
            self.assertEqual(archive.rows("BTCUSDT","fundingRate","2026-09"),[])
            target.write_bytes(data+b"tamper")
            with self.assertRaisesRegex(ValueError,"archive_checksum_mismatch"):archive.rows("BTCUSDT","fundingRate","2026-09")
            archive.get.assert_not_called()
