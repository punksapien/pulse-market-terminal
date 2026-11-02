"""ADR-0030: existing Redis client/state operations, preserved without tuning."""
import json
import math
import os
import threading
import time

from pulse.config import SIGNAL_STATE_TTL, TRACKED_TRADE_SIGNALS


class VercelStorage:
    _client = None
    _initialized = False
    _client_lock = threading.Lock()
    _retry_after = 0.0

    @classmethod
    def get_client(cls):
        if cls._initialized:
            return cls._client
        if cls._retry_after > time.time():
            return None
        with cls._client_lock:
            if cls._initialized:
                return cls._client
            if cls._retry_after > time.time():
                return None

            # Resolve custom Redis URLs from environment variables
            url = (
                os.environ.get("DCXVOL_REDIS_URL")
                or os.environ.get("DCXVOL_URL")
                or os.environ.get("REDIS_URL")
                or os.environ.get("STORAGE_REDIS_URL")
                or os.environ.get("KV_URL")
            )
            if not url:
                cls._initialized = True
                cls._client = None
                return None

            try:
                import redis
                cls._client = redis.Redis.from_url(
                    url,
                    socket_timeout=3,
                    socket_connect_timeout=3,
                    decode_responses=False
                )
                cls._client.ping()
                print("[REDIS] Successfully connected to Redis database.")
            except Exception as e:
                print(f"[REDIS] Failed to initialize client: {e}")
                cls._client = None
                cls._retry_after = time.time() + 5.0
                cls._initialized = False
                return None

            cls._retry_after = 0.0
            cls._initialized = True
            return cls._client

    @classmethod
    def _connection_failed(cls):
        with cls._client_lock:
            cls._client = None
            cls._initialized = False
            cls._retry_after = time.time() + 1.0

    @classmethod
    def is_available(cls):
        return cls.get_client() is not None

    @staticmethod
    def _encode(value):
        if not isinstance(value, str):
            value = json.dumps(value, separators=(",", ":"))
        value_bytes = value.encode("utf-8")
        if len(value_bytes) > 1024:
            import zlib
            value_bytes = zlib.compress(value_bytes, level=6)
        return value_bytes

    @classmethod
    def get(cls, key):
        client = cls.get_client()
        if not client:
            return None
        try:
            val = client.get(key)
            if val is None:
                return None
            import zlib
            # Check for zlib compression signature (always starts with \x78)
            if val.startswith(b'\x78'):
                try:
                    return zlib.decompress(val).decode('utf-8')
                except Exception:
                    pass
            return val.decode('utf-8')
        except Exception as e:
            print(f"[REDIS] GET {key} failed: {e}")
            cls._connection_failed()
            return None

    @classmethod
    def set(cls, key, value, ex=60):
        client = cls.get_client()
        if not client:
            return False
        try:
            client.set(key, cls._encode(value), ex=ex)
            return True
        except Exception as e:
            print(f"[REDIS] SET {key} failed: {e}")
            cls._connection_failed()
            return False

    @classmethod
    def setnx(cls, key, value, ex=120):
        client = cls.get_client()
        if not client:
            return False
        try:
            result = client.set(key, cls._encode(value), nx=True, ex=ex)
            return bool(result)
        except Exception as e:
            print(f"[REDIS] SETNX {key} failed: {e}")
            cls._connection_failed()
            return False

    @classmethod
    def set_many_atomic(cls, entries):
        """Atomically set {key: (value, ttl_seconds|None)} in one transaction."""
        client = cls.get_client()
        if not client:
            return False
        try:
            pipe = client.pipeline(transaction=True)
            for key, (value, ex) in entries.items():
                if ex is None:
                    pipe.set(key, cls._encode(value))
                else:
                    pipe.set(key, cls._encode(value), ex=ex)
            pipe.execute()
            return True
        except Exception as e:
            print(f"[REDIS] Atomic publish failed: {e}")
            cls._connection_failed()
            return False

    @classmethod
    def delete_if_value(cls, key, expected_value):
        client = cls.get_client()
        if not client:
            return 0
        try:
            expected = cls._encode(expected_value)
            script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end"
            )
            return int(client.eval(script, 1, key, expected))
        except Exception as e:
            print(f"[REDIS] Conditional DEL {key} failed: {e}")
            cls._connection_failed()
            return 0

    @classmethod
    def delete(cls, key):
        client = cls.get_client()
        if not client:
            return 0
        try:
            return client.delete(key)
        except Exception as e:
            print(f"[REDIS] DEL {key} failed: {e}")
            cls._connection_failed()
            return 0

    @classmethod
    def get_signal_states(cls, key, instruments):
        """Read transition states for a complete dashboard in one Redis call."""
        client = cls.get_client()
        clean_instruments = list(dict.fromkeys(
            str(instrument)
            for instrument in instruments
            if str(instrument)
        ))
        if not client or not clean_instruments:
            return {}
        try:
            values = client.hmget(key, clean_instruments)
            states = {}
            for instrument, raw in zip(clean_instruments, values):
                if not raw:
                    continue
                try:
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    state = json.loads(raw)
                except (UnicodeDecodeError, TypeError, ValueError):
                    continue
                if isinstance(state, dict):
                    states[instrument] = state
            return states
        except Exception as e:
            print(f"[REDIS] HMGET {key} failed: {e}")
            # Transition metadata is auxiliary. Do not poison the shared
            # generation-store client; redis-py will reconnect on the next
            # attempt, while primary reads retain their original health path.
            return {}

    @classmethod
    def reconcile_signal_states(
        cls,
        key,
        observations,
        source_at,
        generation_id="",
        prune=False,
        ex=SIGNAL_STATE_TTL,
    ):
        """
        Atomically reconcile a complete live signal snapshot.

        The request start time is an ordered snapshot watermark: a delayed
        response can never roll Redis back after a newer snapshot was accepted.
        Every classifier output is tracked so a transition into Uptrend,
        Downtrend, Breakout, or Range can retain its first valid live price.
        The first snapshot establishes only a baseline and never invents a
        historical transition.
        """
        client = cls.get_client()
        if not client:
            return {"accepted": False, "states": {}, "reason": "redis_unavailable"}

        cleaned = []
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            instrument = str(observation.get("instrument") or "")
            signal = str(observation.get("signal") or "")
            try:
                price = float(observation.get("price"))
            except (TypeError, ValueError):
                continue
            if (
                not instrument
                or signal not in TRACKED_TRADE_SIGNALS
                or not math.isfinite(price)
                or price <= 0
            ):
                continue
            cleaned.append((instrument, signal, price))

        try:
            source_at = float(source_at)
        except (TypeError, ValueError):
            return {"accepted": False, "states": {}, "reason": "invalid_snapshot"}
        if not cleaned or not math.isfinite(source_at) or source_at <= 0:
            return {"accepted": False, "states": {}, "reason": "invalid_snapshot"}

        # One hash and one Lua transaction keep the state durable across Vercel
        # instances without a read/modify/write race. State JSON remains small;
        # a 30-day sliding TTL prevents abandoned timeframe keys accumulating.
        script = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
local source_at = tonumber(ARGV[2])
local generation_id = ARGV[3]
local prune = ARGV[4] == '1'
local current_source = tonumber(redis.call('HGET', key, '__source_at') or '0')
local accepted = source_at > current_source
local result = {accepted and '1' or '0'}
local keep = {}

for i = 5, #ARGV, 3 do
  local instrument = ARGV[i]
  local signal = ARGV[i + 1]
  local price = tonumber(ARGV[i + 2])
  keep[instrument] = true
  local raw = redis.call('HGET', key, instrument)
  local state = nil
  if raw then
    local ok, decoded = pcall(cjson.decode, raw)
    if ok and type(decoded) == 'table' then
      state = decoded
    end
  end

  if accepted then
    if not state then
      state = {
        signal = signal,
        observed_price = price,
        observed_at = source_at,
        generation_id = generation_id
      }
      raw = cjson.encode(state)
      redis.call('HSET', key, instrument, raw)
    elseif state['signal'] ~= signal then
      state = {
        signal = signal,
        observed_price = price,
        observed_at = source_at,
        generation_id = generation_id
      }
      if signal == 'MomentumLong' or signal == 'MomentumShort'
          or signal == 'BreakoutWatch' or signal == 'Range' then
        state['since_price'] = price
        state['since_at'] = source_at
      end
      raw = cjson.encode(state)
      redis.call('HSET', key, instrument, raw)
    end
  end

  if raw then
    table.insert(result, instrument)
    table.insert(result, raw)
  end
end

if accepted then
  if prune then
    local fields = redis.call('HKEYS', key)
    for _, field in ipairs(fields) do
      if field ~= '__source_at' and not keep[field] then
        redis.call('HDEL', key, field)
      end
    end
  end
  redis.call('HSET', key, '__source_at', tostring(source_at))
  if ttl and ttl > 0 then
    redis.call('EXPIRE', key, ttl)
  end
end

return result
"""
        args = [
            str(int(ex)),
            repr(source_at),
            str(generation_id or ""),
            "1" if prune else "0",
        ]
        for instrument, signal, price in cleaned:
            args.extend((instrument, signal, repr(price)))

        try:
            raw_result = client.eval(script, 1, key, *args)
            if not isinstance(raw_result, (list, tuple)) or not raw_result:
                return {"accepted": False, "states": {}, "reason": "invalid_result"}
            accepted_raw = raw_result[0]
            if isinstance(accepted_raw, bytes):
                accepted_raw = accepted_raw.decode("utf-8")
            accepted = accepted_raw == "1"
            states = {}
            for index in range(1, len(raw_result) - 1, 2):
                instrument = raw_result[index]
                raw = raw_result[index + 1]
                if isinstance(instrument, bytes):
                    instrument = instrument.decode("utf-8")
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    state = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(state, dict):
                    states[str(instrument)] = state
            return {
                "accepted": accepted,
                "states": states,
                "reason": "accepted" if accepted else "superseded",
            }
        except Exception as e:
            print(f"[REDIS] Signal reconciliation {key} failed: {e}")
            # This optional state must not alter primary generation/auto-heal
            # connection health. A later live refresh retries it independently.
            return {"accepted": False, "states": {}, "reason": "redis_error"}

    @classmethod
    def rpush(cls, key, value):
        client = cls.get_client()
        if not client:
            return 0
        try:
            if not isinstance(value, str):
                value = json.dumps(value)
            value_bytes = value.encode('utf-8')
            # Compress if list items are large
            if len(value_bytes) > 1024:
                import zlib
                value_bytes = zlib.compress(value_bytes, level=6)
            return client.rpush(key, value_bytes)
        except Exception as e:
            print(f"[REDIS] RPUSH {key} failed: {e}")
            cls._connection_failed()
            return 0

    @classmethod
    def ltrim(cls, key, start, stop):
        client = cls.get_client()
        if not client:
            return None
        try:
            return client.ltrim(key, start, stop)
        except Exception as e:
            print(f"[REDIS] LTRIM {key} failed: {e}")
            cls._connection_failed()
            return None
