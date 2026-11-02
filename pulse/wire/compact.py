"""ADR-0081 lossless columnar documents; no financial arithmetic."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

PARTS = ("core", "volatility", "ls", "funding", "funding_change", "oi", "references")
METRICS = PARTS[1:-1]
TIMES = ("asof_ms", "expires_ms", "input_refs")
SPECIAL = ("value", "status", "reason") + TIMES
FORBIDDEN = {"__proto__", "constructor", "prototype"}


def raw(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(raw(value)).hexdigest()


def vector(values):
    """Constant/dense/default+sparse; ties choose simpler representation."""
    counts = Counter(raw(v) for v in values)
    common = json.loads(counts.most_common(1)[0][0])
    if len(counts) == 1:
        return ["c", common]
    dense = ["d", values]
    sparse = ["s", common, [[i, v] for i, v in enumerate(values) if raw(v) != raw(common)]]
    return min((dense, sparse), key=lambda v: len(raw(v)))


def expand(encoded, n):
    if not isinstance(encoded, list) or not encoded:
        raise ValueError("invalid_vector")
    if encoded[0] == "c" and len(encoded) == 2:
        return [deepcopy(encoded[1]) for _ in range(n)]
    if encoded[0] == "d" and len(encoded) == 2 and isinstance(encoded[1], list) and len(encoded[1]) == n:
        return deepcopy(encoded[1])
    if encoded[0] == "s" and len(encoded) == 3 and isinstance(encoded[2], list):
        result = [deepcopy(encoded[1]) for _ in range(n)]
        previous = -1
        for item in encoded[2]:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("invalid_sparse_entry")
            i, value = item
            if type(i) is not int or not previous < i < n:
                raise ValueError("invalid_sparse_index")
            result[i] = deepcopy(value)
            previous = i
        return result
    raise ValueError("invalid_vector")


def flatten(value, path=()):
    if isinstance(value, dict) and value and not {"unit", "value", "status"} <= value.keys():
        for key in sorted(value):
            if key in FORBIDDEN:
                raise ValueError("unsafe_path")
            yield from flatten(value[key], path + (key,))
    else:
        yield path, value


def encode_rows(rows):
    flat = [dict(flatten(row)) for row in rows]
    paths = sorted(set().union(*(r.keys() for r in flat)))
    metadata, groups, mi, gi = [], [], {}, {}

    def intern(value, table, index):
        key = raw(value)
        if key not in index:
            index[key] = len(table)
            table.append(value)
        return index[key]

    columns = []
    for path in paths:
        present = [i for i, row in enumerate(flat) if path in row]
        values = [flat[i][path] for i in present]
        field = all(isinstance(v, dict) and {"unit", "value", "status"} <= v.keys() for v in values)
        presence = None if len(present) == len(rows) else present
        if field:
            metas, times, states = [], [], []
            for value in values:
                metas.append(intern({k: v for k, v in value.items() if k not in SPECIAL}, metadata, mi))
                times.append(intern({k: value[k] for k in TIMES if k in value}, groups, gi))
                states.append({k: value[k] for k in ("status", "reason") if k in value})
            columns.append([list(path), presence, "f", vector([v["value"] for v in values]),
                            vector(metas), vector(times), vector(states)])
        else:
            columns.append([list(path), presence, "j", vector(values)])
    return {"metadata": metadata, "groups": groups, "columns": columns}


def decode_rows(document):
    n = document["count"]
    if type(n) is not int or not 1 <= n <= 8192:
        raise ValueError("invalid_count")
    rows = [{} for _ in range(n)]
    seen = {}
    for column in document["columns"]:
        if not isinstance(column, list) or len(column) not in (4, 7):
            raise ValueError("invalid_column")
        path, presence, kind = column[:3]
        if not isinstance(path, list) or not path or any(not isinstance(k, str) or not k or k in FORBIDDEN for k in path):
            raise ValueError("unsafe_path")
        key = tuple(path)
        indices = list(range(n)) if presence is None else presence
        if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < n for i in indices) or indices != sorted(set(indices)):
            raise ValueError("invalid_presence")
        present=set(indices)
        if key in seen or any((key[:len(p)] == p or p[:len(key)] == key) and present.intersection(used)
                              for p,used in seen.items()):
            raise ValueError("overlapping_path")
        seen[key]=present
        m = len(indices)
        values = expand(column[3], m)
        if kind == "f" and len(column) == 7:
            mids, tids, states = [expand(c, m) for c in column[4:]]
            for i in range(m):
                mid, tid = mids[i], tids[i]
                if type(mid) is not int or not 0 <= mid < len(document["metadata"]) or type(tid) is not int or not 0 <= tid < len(document["groups"]):
                    raise ValueError("invalid_table_index")
                meta, group, state = document["metadata"][mid], document["groups"][tid], states[i]
                if not isinstance(meta, dict) or set(meta) & set(SPECIAL) or not isinstance(group, dict) or set(group) - set(TIMES) or not isinstance(state, dict) or set(state) - {"status", "reason"}:
                    raise ValueError("invalid_field_tables")
                values[i] = {**deepcopy(meta), **deepcopy(group), **deepcopy(state), "value": values[i]}
        elif kind != "j" or len(column) != 4:
            raise ValueError("invalid_column_kind")
        for i, value in zip(indices, values):
            node = rows[i]
            for segment in path[:-1]:
                node = node.setdefault(segment, {})
            node[path[-1]] = value
    return rows


def encode(snapshot):
    """Caller validates the logical snapshot before publishing."""
    if snapshot.get("schema") != "pulse.snapshot.v1" or not 1 <= len(snapshot["rows"]) <= 8192:
        raise ValueError("invalid_logical_snapshot")
    documents = {}
    def refs(node):
        if isinstance(node, dict):
            yield from node.get("input_refs", [])
            for value in node.values():
                yield from refs(value)
        elif isinstance(node, list):
            for value in node:
                yield from refs(value)
    all_refs = set(refs(snapshot["rows"]))
    for part in PARTS:
        rows = []
        for row in snapshot["rows"]:
            if part == "core":
                r = {k: v for k, v in row.items() if k not in METRICS + ("references", "history")}
                # Each core URL is already horizon-specific. Preserve the other
                # two logical changes in references, rather than downloading all
                # three full-precision returns on every horizon switch.
                r["changes"] = {snapshot["horizon"]: row["changes"][snapshot["horizon"]]}
                r["history"] = {k: v for k, v in row["history"].items() if k not in METRICS}
            elif part == "references":
                r = {"references": row[part], "changes": {h: v for h, v in row["changes"].items() if h != snapshot["horizon"]}}
            else:
                r = {part: row[part], "history": {part: row["history"][part]}}
            rows.append(r)
        doc = {"schema": "pulse.columns.v1", "part": part, "count": len(rows),
               **{k: snapshot[k] for k in ("mapping_version", "generation", "horizon", "generated_ms", "expires_ms")},
               **encode_rows(rows)}
        if part == "core":
            doc["header"] = {k: v for k, v in snapshot.items() if k not in ("rows", "inputs")}
        names = set(refs(rows))
        if part == "core":
            names |= set(snapshot["inputs"]) - all_refs
        doc["inputs"] = {k: snapshot["inputs"][k] for k in sorted(names)}
        documents[part] = doc
    return documents


def decode(documents):
    if set(documents) != set(PARTS):
        raise ValueError("incomplete_generation")
    core = documents["core"]
    header=core.get("header",{})
    if any(header.get(k)!=core.get(k) for k in ("mapping_version","generation","horizon","generated_ms","expires_ms")) or len(header.get("universe",[]))!=core.get("count"):
        raise ValueError("header_identity_mismatch")
    rows = decode_rows(core)

    def merge(target, source):
        for k, value in source.items():
            if k not in target:
                target[k] = value
            elif isinstance(target[k], dict) and isinstance(value, dict):
                merge(target[k], value)
            else:
                raise ValueError("duplicate_logical_path")

    inputs = {}
    for part in PARTS:
        doc = documents[part]
        if doc.get("schema") != "pulse.columns.v1" or doc.get("part") != part or any(doc[k] != core[k] for k in ("mapping_version", "generation", "horizon", "count", "generated_ms", "expires_ms")):
            raise ValueError("mixed_generation")
        if part != "core":
            for target, source in zip(rows, decode_rows(doc)):
                merge(target, source)
        for key, value in doc["inputs"].items():
            if key in inputs and inputs[key] != value:
                raise ValueError("input_version_mismatch")
            inputs[key] = deepcopy(value)
    return {**deepcopy(core["header"]), "inputs": inputs, "rows": rows}
