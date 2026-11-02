"""ADR-0167 exact, bounded successful checks; semantic checks are never cached."""
from collections import OrderedDict
import json
from threading import Lock


def json_value(value,depth=0):
    if depth>8:return False
    if type(value) in (str,int,float,bool,type(None)):return True
    if type(value) is list:return len(value)<=128 and all(json_value(v,depth+1) for v in value)
    if type(value) is dict:return len(value)<=64 and all(type(k) is str and json_value(v,depth+1) for k,v in value.items())
    return False


class SuccessfulReferences:
    def __init__(self,original,*,entries=16384,byte_limit=8*1024*1024):
        self.original=original;self.entries=entries;self.byte_limit=byte_limit
        self.cache=OrderedDict();self.bytes=0;self.lock=Lock()

    def __call__(self,validator,reference,instance,schema):
        key=None
        if (reference.startswith("urn:pulse:schema:v1:common#/$defs/")
                and type(instance) is dict and json_value(instance)):
            try:body=json.dumps(instance,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
            except (ValueError,TypeError,OverflowError):body=b""
            if body and len(body)<=4096:key=(id(schema),reference,body)
        if key is not None:
            with self.lock:
                if key in self.cache:
                    self.cache.move_to_end(key)
                    return
        errors=list(self.original(validator,reference,instance,schema))
        if errors:
            yield from errors
            return
        if key is not None:
            size=len(key[1])+len(key[2])+128
            with self.lock:
                if key not in self.cache:
                    self.cache[key]=size;self.bytes+=size
                self.cache.move_to_end(key)
                while len(self.cache)>self.entries or self.bytes>self.byte_limit:
                    _,old=self.cache.popitem(last=False);self.bytes-=old
