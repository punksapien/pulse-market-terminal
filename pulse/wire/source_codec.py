"""ADR-0136 bounded lossless worker-only source-witness compression."""
import gzip
import zlib
import brotli

MAGIC=b"PULSEW1GZ\x00"
BR_MAGIC=b"PULSEW1BR\x00"
MAX_BYTES=1114112


def gunzip(body,*,maximum=MAX_BYTES):
    if (type(body) is not bytes or not 1<=len(body)<=MAX_BYTES
            or type(maximum) is not int or not 1<=maximum<=MAX_BYTES):
        raise ValueError("source_codec_size")
    try:
        stream=zlib.decompressobj(31)
        result=stream.decompress(body,maximum+1)
        if len(result)>maximum or not stream.eof or stream.unused_data or stream.unconsumed_tail:
            raise ValueError("source_codec_invalid")
        return result
    except zlib.error:raise ValueError("source_codec_invalid") from None


def encode(body):
    if type(body) is not bytes or not 1<=len(body)<=MAX_BYTES:raise ValueError("source_codec_size")
    result=MAGIC+gzip.compress(body,compresslevel=6,mtime=0)
    alternative=BR_MAGIC+brotli.compress(body,quality=5)
    if len(alternative)<len(result):result=alternative
    if len(result)>MAX_BYTES:raise ValueError("source_codec_size")
    return result


def decode(body):
    if type(body) is not bytes or not 1<=len(body)<=MAX_BYTES:raise ValueError("source_codec_size")
    if body.startswith(BR_MAGIC):
        try:
            stream=brotli.Decompressor()
            result=stream.process(body[len(BR_MAGIC):],output_buffer_limit=MAX_BYTES+1)
            if len(result)>MAX_BYTES or not stream.is_finished():raise ValueError("source_codec_invalid")
            return result
        except brotli.error:raise ValueError("source_codec_invalid") from None
    return gunzip(body[len(MAGIC):]) if body.startswith(MAGIC) else body
