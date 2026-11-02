from copy import deepcopy
import unittest
from jsonschema import Draft202012Validator
from jsonschema.validators import extend
from referencing import Registry,Resource
from pulse.wire.schema_cache import SuccessfulReferences


class SchemaCacheTests(unittest.TestCase):
    def rig(self,**limits):
        target={"$schema":"https://json-schema.org/draft/2020-12/schema",
            "$id":"urn:pulse:schema:v1:common","$defs":{"test":{"type":"object",
            "required":["v"],"additionalProperties":False,"properties":{"v":{"type":"array","items":{"type":"integer"}}}}}}
        schema={"$ref":"urn:pulse:schema:v1:common#/$defs/test"}
        cache=SuccessfulReferences(Draft202012Validator.VALIDATORS["$ref"],**limits)
        cls=extend(Draft202012Validator,{"$ref":cache})
        registry=Registry().with_resource(target["$id"],Resource.from_contents(target))
        return cls(schema,registry=registry),Draft202012Validator(schema,registry=registry),cache

    def test_mutations_types_and_errors_match_original(self):
        cached,plain,cache=self.rig()
        good={"v":[1,2]};cached.validate(good);cached.validate(deepcopy(good))
        self.assertEqual(len(cache.cache),1)
        for bad in ({"v":[True]}, {"v":(1,2)}, {"v":["1"]}, {"v":[1],"extra":0}, {}, {"v":[None]}):
            self.assertEqual(cached.is_valid(bad),plain.is_valid(bad))
            self.assertFalse(cached.is_valid(bad))
        good["v"][0]=True
        self.assertFalse(cached.is_valid(good))
        self.assertEqual(len(cache.cache),1,"invalid results must never be cached")

    def test_both_lru_bounds_and_evicted_values_are_checked(self):
        cached,plain,cache=self.rig(entries=2,byte_limit=500)
        for n in range(100):cached.validate({"v":[n]})
        self.assertLessEqual(len(cache.cache),2);self.assertLessEqual(cache.bytes,500)
        self.assertEqual(cached.is_valid({"v":[0]}),plain.is_valid({"v":[0]}))
        self.assertFalse(cached.is_valid({"v":[False]}))
