import json
import unittest
from unittest.mock import Mock
from pulse.http.worker_trigger import response


class WorkerTriggerTests(unittest.TestCase):
    def call(self,**patch):
        self.runner=Mock(return_value={"status":"incomplete"})
        self.args=dict(method="POST",authorization="Bearer "+"s"*40,content_length="0",query="",
            environment="preview",config={"PULSE_WORKER_SECRET":"s"*40,"PULSE_WORKER_ENABLED":"1",
            "PULSE_RUNTIME_RUN_ID":"a"*32,"PULSE_MAPPING_VERSION":"sha256:"+"b"*64},runner=self.runner)
        self.args.update(patch)
        return response(**self.args)

    def test_auth_and_scope_before_any_worker_access(self):
        for patch,status in (({"environment":"production"},404),({"method":"GET"},404),
            ({"authorization":None},401),({"authorization":"Bearer wrong"},401),
            ({"content_length":"1"},400),({"query":"symbols=BTC"},400),({"config":{}},401)):
            self.assertEqual(self.call(**patch).status,status);self.runner.assert_not_called()

    def test_bounded_run_and_disabled_scope(self):
        self.assertEqual(self.call().status,200)
        self.runner.assert_called_once_with(run_id="a"*32,mapping_version="sha256:"+"b"*64,duration_ms=55000)
        for key,value in (("PULSE_WORKER_ENABLED","0"),("PULSE_RUNTIME_RUN_ID","production"),
                          ("PULSE_MAPPING_VERSION","unknown")):
            self.call();config=dict(self.args["config"]);config[key]=value
            self.assertEqual(self.call(config=config).status,503);self.runner.assert_not_called()

    def test_production_cron_uses_its_own_secret_and_explicit_activation(self):
        self.call()
        config=dict(self.args["config"], VERCEL_ENV="production", PULSE_PRODUCTION_ENABLED="1", PULSE_UNIVERSE_MODE="all", CRON_SECRET="c"*40)
        self.assertEqual(self.call(method="GET", environment="production", config=config,
            authorization="Bearer "+"c"*40).status,200)
        self.runner.assert_called_once()
        self.assertEqual(self.call(method="GET", environment="production", config=config,
            authorization="Bearer "+"s"*40).status,401)
        self.runner.assert_not_called()
        self.assertEqual(self.call(method="GET", config=config, authorization="Bearer "+"c"*40).status,404)

    def test_provider_errors_are_not_disclosed(self):
        self.call();self.runner.side_effect=RuntimeError("redis://private-credential")
        value=response(**self.args)
        self.assertEqual(value.status,503)
        self.assertNotIn(b"private",value.body)
        self.assertEqual(json.loads(value.body)["reason"],"worker_run_failed")
        self.assertNotIn("detail",json.loads(value.body),"free text is withheld")

    def test_fixed_codes_are_reported_for_diagnosis(self):
        self.call();self.runner.side_effect=RuntimeError("publication_promotion_refused:input_revision_conflict")
        body=json.loads(response(**self.args).body)
        self.assertEqual((body["error_type"],body["detail"]),("RuntimeError","publication_promotion_refused:input_revision_conflict"))
        self.runner.side_effect=KeyError("secret path /etc/x")
        body=json.loads(response(**self.args).body)
        self.assertEqual(body["error_type"],"KeyError");self.assertNotIn("detail",body)

    def test_safe_stage_is_returned_without_arbitrary_attributes(self):
        self.call()
        error = RuntimeError("publication_promotion_refused:invalid_time")
        error.worker_stage = "promote"
        self.runner.side_effect = error
        self.assertEqual(json.loads(response(**self.args).body)["stage"], "promote")
        error.worker_stage = "redis://private"
        self.assertNotIn("stage", json.loads(response(**self.args).body))


if __name__=="__main__": unittest.main()
