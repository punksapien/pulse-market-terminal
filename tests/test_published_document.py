import unittest
from unittest.mock import Mock

from api.pulse import response


class PublishedDocumentTests(unittest.TestCase):
    def test_explicit_production_activation_reads_only_published_bytes(self):
        store=Mock()
        store.read.return_value={"status":"available","expires_ms":4500,"digest":"a"*64,"payload":b'{}'}
        factory=Mock(return_value=store)
        result=response({"horizon":["1H"]},environment="production",production_active=True,store_factory=factory,now_ms=1000)
        self.assertEqual(result.status,200)
        store.acquire.assert_not_called();store.publish.assert_not_called()

    def test_production_refuses_before_store_access(self):
        factory=Mock()
        result=response({"horizon":["1H"]},environment="production",store_factory=factory,now_ms=1000)
        self.assertEqual(result.status,404); factory.assert_not_called()

    def test_fixed_urls_unknowns_and_duplicate_parameters_refuse(self):
        for query in ({},{"symbol":["BTCUSDT"]},{"horizon":["1H","4H"]},
                      {"horizon":["1H"],"utm_source":["x"]},{"live":["0"]}):
            factory=Mock()
            result=response(query,environment="preview",store_factory=factory,now_ms=1000)
            self.assertEqual(result.status,400); factory.assert_not_called()

    def test_200_304_and_expired_never_extend_lifetime(self):
        store=Mock()
        store.read.return_value={"status":"available","expires_ms":4500,"digest":"a"*64,"payload":b'{"schema":"fixture"}'}
        factory=Mock(return_value=store)
        args=dict(query={"horizon":["1H"]},environment="preview",store_factory=factory,now_ms=1000)
        result=response(**args)
        self.assertEqual(result.status,200)
        self.assertIn("s-maxage=3",result.headers["Cache-Control"])
        factory.assert_called_once_with("snapshot:1h")
        cached=response(**args,if_none_match='"'+'a'*64+'"')
        self.assertEqual(cached.status,304); self.assertEqual(cached.body,b"")
        store.read.return_value={"status":"unavailable","reason":"document_expired"}
        expired=response(**args,if_none_match='"'+'a'*64+'"')
        self.assertEqual(expired.status,503)
        self.assertEqual(expired.headers["Cache-Control"],"no-store")
        store.acquire.assert_not_called(); store.publish.assert_not_called()
        store.read.side_effect=TimeoutError()
        self.assertEqual(response(**args).status,503)


if __name__=="__main__": unittest.main()
