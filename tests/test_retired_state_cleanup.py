import hashlib
from unittest import TestCase
from unittest.mock import Mock,patch

from pulse.collectors import retired_state_cleanup as cleanup


class RetiredCleanupTests(TestCase):
    def entry(self,key=None):
        return dict(key=key or cleanup.KEYS[0],dump_sha1=hashlib.sha1(b'fixed-dump').hexdigest(),dump_bytes=10,hash_fields=275)

    def test_only_exact_keys_and_identical_dump_may_be_unlinked(self):
        client=Mock();client.dump.return_value=b'fixed-dump';client.eval.return_value=[1,23]
        entry=self.entry()
        self.assertEqual(cleanup.checked_unlink(client,entry,b'retired-owner'),dict(outcome='unlinked',unlink_us=23))
        client.eval.assert_called_once_with(cleanup.UNLINK,2,cleanup.OLD_PREFIX+'lease',entry['key'],b'retired-owner',275)
        client.reset_mock();client.dump.return_value=b'changed'
        with self.assertRaisesRegex(RuntimeError,'retired_cleanup_dump_changed'):
            cleanup.checked_unlink(client,entry,b'owner')
        client.eval.assert_not_called()
        with self.assertRaisesRegex(ValueError,'retired_cleanup_key_refused'):
            cleanup.checked_unlink(client,self.entry('pulse:prod:foreign'),b'owner')

    def test_missing_is_idempotent_but_fence_loss_is_not_success(self):
        client=Mock();client.dump.return_value=None
        self.assertEqual(cleanup.checked_unlink(client,self.entry(),b'owner'),dict(outcome='already_absent',unlink_us=None))
        client.eval.assert_not_called()
        client.dump.return_value=b'fixed-dump';client.eval.return_value=-1
        with self.assertRaisesRegex(RuntimeError,'retired_cleanup_fence_or_shape_changed'):
            cleanup.checked_unlink(client,self.entry(),b'owner')

    def test_scope_and_approval_fail_before_connection(self):
        base=dict(VERCEL_ENV='preview',PULSE_RELEASE_TOP350='1',PULSE_CAPSULE_RUN_ID=cleanup.OLD_RUN,
            PULSE_RETIRED_STATE_CLEANUP_APPROVED='1')
        for change in (dict(VERCEL_ENV='production'),dict(PULSE_RETIRED_STATE_CLEANUP_APPROVED='0'),
                       dict(PULSE_CAPSULE_RUN_ID='b'*32),dict(PULSE_RELEASE_TOP350='0')):
            with patch.object(cleanup,'connect') as connect:
                with self.assertRaisesRegex(ValueError,'retired_cleanup_not_approved'):
                    cleanup.run(run_id=cleanup.NEW_RUN,mapping_version='sha256:'+'b'*64,duration_ms=55000,env={**base,**change})
                connect.assert_not_called()

    def test_lua_only_has_two_exact_targets_and_no_dump_hashing_or_scan(self):
        for key in cleanup.KEYS:self.assertIn(key,cleanup.UNLINK)
        for forbidden in ('DUMP','SCAN','KEYS pattern','FLUSH','DEL('):self.assertNotIn(forbidden,cleanup.UNLINK)
        self.assertIn("redis.call('PTTL'",cleanup.UNLINK)
        self.assertIn("redis.call('HLEN'",cleanup.UNLINK)
