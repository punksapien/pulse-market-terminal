import json
from pathlib import Path
import unittest


class ClassifiedFixtureTests(unittest.TestCase):
    def test_frozen_levels_and_stable_match_executable_v1(self):
        from pulse.legacy.signal_engine import classify_direction,classify_regime
        s=json.loads((Path(__file__).resolve().parents[1]/"docs/contracts/fixtures/snapshot.classified_stable.json").read_text())
        row=s["rows"][0]
        levels={"Calm":1,"Low":1,"Active":2,"Hot":3,"Extreme":4}
        for horizon,seconds in (("1H",3600),("4H",14400),("24H",86400)):
            f=row["volatility"]["windows"][horizon]
            self.assertEqual(f["level"],levels[classify_regime(float(f["value"]),seconds)])
        self.assertEqual(row["volatility"]["direction"]["value"],classify_direction(.01,.01,.01,.01,.5))


if __name__=="__main__": unittest.main()
