"""ADR-0082 selection of already stored bytes; no compression or exchange calls."""
import re
from .published_document import Response

Q=re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")


def negotiate(header, variants):
    if header is None or not header.strip():
        return "identity" if "identity" in variants else None
    if len(header)>1024: return None
    weights={}
    for item in header.lower().split(","):
        pieces=[p.strip() for p in item.split(";")]
        name=pieces[0]
        if name not in ("br","gzip","identity","*"): continue
        weight=1.0
        if len(pieces)>1:
            if len(pieces)!=2 or not pieces[1].startswith("q=") or not Q.fullmatch(pieces[1][2:]):
                weight=0.0
            else: weight=float(pieces[1][2:])
        weights[name]=max(weights.get(name,0),weight)
    choices=[]
    for rank,name in enumerate(("br11","br5","gzip","identity")):
        if name not in variants: continue
        coding="br" if name.startswith("br") else name
        default=1.0 if coding=="identity" and weights.get("*")!=0 else weights.get("*",0)
        weight=weights.get(coding,default)
        if weight>0: choices.append((weight,-rank,name))
    return max(choices)[2] if choices else None


def artifact_response(descriptor, read_blob, *, accept_encoding=None, if_none_match=None, cache_control):
    name=negotiate(accept_encoding,descriptor["variants"])
    if name is None:
        return Response(406,{"Cache-Control":"no-store","Vary":"Accept-Encoding"},b"")
    variant=descriptor["variants"][name]
    etag='"'+variant["sha256"]+'"'
    headers={"Content-Type":"application/json","Vary":"Accept-Encoding","ETag":etag,
        "Cache-Control":cache_control,"X-Content-Type-Options":"nosniff"}
    if variant["encoding"]!="identity": headers["Content-Encoding"]=variant["encoding"]
    # Read and verify before even a 304: don't claim an absent/corrupt blob exists.
    body=read_blob(variant["sha256"])
    import hashlib
    if not isinstance(body,bytes) or len(body)!=variant["bytes"] or "sha256:"+hashlib.sha256(body).hexdigest()!=variant["sha256"]:
        raise ValueError("artifact_integrity_mismatch")
    if if_none_match==etag: return Response(304,headers,b"")
    headers["Content-Length"]=str(len(body))
    return Response(200,headers,body)
