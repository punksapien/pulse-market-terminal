"""ADR-0078 cross-property checks; JSON Schema must validate shape first."""
from decimal import Decimal, localcontext, Context
import math
import sys

from snapshot_spec import CATEGORIES
from canonical import digest, generation
from pulse.wire.level_contract import validate_levels

WINDOWS={"1H":3600000,"4H":14400000,"24H":86400000}


def require(condition,reason):
    if not condition: raise ValueError(reason)


def price(value):
    exact=Decimal(value); binary=float(exact)
    require(exact>0 and math.isfinite(binary) and binary>=sys.float_info.min
            and Decimal(repr(binary))==exact,"price_precision_unsupported")


def fields(node):
    if isinstance(node,dict):
        if "unit" in node and "value" in node: yield node
        for value in node.values(): yield from fields(value)
    elif isinstance(node,list):
        for value in node: yield from fields(value)


def validate_snapshot(snapshot):
    def integers_only(node):
        if type(node) is float: raise ValueError("noninteger_json_number")
        if isinstance(node,dict):
            for value in node.values(): integers_only(value)
        elif isinstance(node,list):
            for value in node: integers_only(value)
    integers_only(snapshot)
    generated=snapshot["generated_ms"]
    require(generated<snapshot["expires_ms"],"envelope_expiry_order")
    universe,rows=snapshot["universe"],snapshot["rows"]
    require(snapshot["mapping_version"]==digest(universe),"dictionary_digest_mismatch")
    require(len(universe)==len(rows),"row_dictionary_length")
    require(len({u["pair_id"] for u in universe})==len(universe)
            and len({u["symbol"] for u in universe})==len(universe),"dictionary_identity_duplicate")
    for entry in snapshot["inputs"].values():
        require(entry["source_ms"]<=generated+5000 and entry["source_ms"]<entry["expires_ms"],"input_time_order")
        if entry["verified_at_ms"] is not None:
            require(entry["verified_at_ms"]<=generated+5000,"input_future_verification")
    for field in fields(rows):
        require(all(key in snapshot["inputs"] for key in field["input_refs"]),"unknown_field_input")
        if field["status"]!="available": continue
        require(field["asof_ms"]<=generated+5000 and field["asof_ms"]<field["expires_ms"]
                and generated<field["expires_ms"],"field_expired_at_generation")
        require(field["expires_ms"]<=min(snapshot["inputs"][key]["expires_ms"] for key in field["input_refs"]),
                "field_freshens_input")
        if field["unit"]=="price": price(field["value"])
    for index,(meta,row) in enumerate(zip(universe,rows)):
        validate_levels(row)
        require(meta["id"]==row["id"]==index,"row_dictionary_id_mismatch")
        require(meta["symbol"]==meta["pair_id"][2:-5]+"USDT","dictionary_symbol_mismatch")
        require(row["volatility"]["score"]==row["volatility"]["windows"][snapshot["horizon"]],"volatility_horizon_mismatch")
        for field in fields(row):
            if field["status"]!="available" or field["unit"] in ("state","text","boolean"): continue
            number=Decimal(field["value"])
            require(number.is_finite(),"nonfinite_field")
            require(number.as_tuple().exponent>=-60,"decimal_scale_unsupported")
            if field["unit"] in ("price","ratio"): require(number>0,"nonpositive_field")
            if field["unit"] in ("quantity","USDT"): require(number>=0,"negative_quantity")
        for name in ("global_long_share","top_long_share"):
            f=row["ls"][name]
            if f["status"]=="available": require(0<=Decimal(f["value"])<=1,"invalid_share")
        for name in ("confidence",):
            f=row["outlook"][name]
            if f["status"]=="available": require(0<=Decimal(f["value"])<=1,"invalid_confidence")
        f=row["context"]["rsi_14"]
        if f["status"]=="available": require(0<=Decimal(f["value"])<=100,"invalid_rsi")
        cycle=row["funding_change"]; funding=row["funding"]
        require(cycle["id"]==row["id"] and cycle["mapping_version"]==snapshot["mapping_version"],"funding_identity_mismatch")
        require(cycle["h"]==funding["h"]==meta["funding_hours"] and cycle["nft"]==funding["nft"],"funding_cycle_mismatch")
        if funding["native_estimate"]["status"]=="available":
            estimate=funding["native_estimate"]
            require(funding["h"] in (1,4,8) and funding["nft"] is not None,"estimate_metadata_missing")
            require(estimate["expires_ms"]<=min(estimate["asof_ms"]+120000,funding["nft"]),"estimate_source_expiry")
        if cycle["status"]=="available":
            due=cycle["settlement_ms"]+cycle["h"]*3600000
            require(abs(due-cycle["nft"])<=5000,"settlement_spacing_nft_mismatch")
            require(cycle["settlement_ms"]<=cycle["verified_at_ms"]+5000 and cycle["verified_at_ms"]<due
                    and cycle["verified_at_ms"]<=generated+5000 and generated<due,"settlement_verification_time")
            require(cycle["expires_ms"]==due+900000,"settlement_r6_expiry")
            last,basis=cycle["settled_last"],cycle["basis_bps"]
            require((last["status"]=="available")== (basis["status"]=="available"),"basis_operand_unavailable")
            if last["status"]=="available":
                with localcontext(Context(prec=60)):
                    expected=abs(Decimal(last["value"])*10000*8/cycle["h"])
                require(Decimal(basis["value"])==expected,"funding_basis_mismatch")
                if last["level_basis"] is not None:
                    require(last["level_basis_unit"]=="bps" and Decimal(last["level_basis"])==expected,"funding_level_basis_mismatch")
            for name in ("settled_last","settled_1d","settled_7d","basis_bps"):
                f=cycle[name]
                if f["status"]=="available": require(f["expires_ms"]<=min(due,cycle["nft"]),"settled_value_past_due")
        else:
            require(all(cycle[name]["status"]!="available" for name in
                ("settled_last","settled_1d","settled_7d","basis_bps")),"unverified_settlement_number")
        for h,window in WINDOWS.items():
            field=row["oi"]["change_"+h.lower()]
            if field["status"]!="available": continue
            current=row["oi"]["quantity"]
            require(current["status"]=="available" and current["asof_ms"]==field["asof_ms"],"oi_current_mismatch")
            require(field["mode"] in ("own","cold-start") and field["baseline_asof_ms"] is not None,"oi_provenance_missing")
            duration=field["asof_ms"]-field["baseline_asof_ms"]
            error=duration-window
            require(duration==field["actual_duration_ms"] and error==field["baseline_error_ms"]
                    and 0<=error<=(60000 if field["mode"]=="own" else 300000),"oi_baseline_mismatch")
        code,category=row["signal"]["code"],row["signal"]["category"]
        require((code["status"]=="available")== (category["status"]=="available"),"signal_category_unavailable")
        if code["status"]=="available": require(CATEGORIES[code["value"]]==category["value"],"signal_category_mismatch")
        since=row["signal_since"]
        if since["status"]=="available":
            require(code["status"]=="available" and since["state"]==code["value"],"signal_since_state_mismatch")
            require(since["at_ms"]<=since["asof_ms"]<=generated+5000 and generated<since["expires_ms"]
                    and since["expires_ms"]<=code["expires_ms"],"signal_since_time_order")
            require(all(key in snapshot["inputs"] for key in since["input_refs"]),"unknown_field_input")
            price(since["price"])
        for series in row["history"].values():
            points=series["points"]
            require(all(p["source_ms"]<=generated+5000 for p in points)
                    and all(a["source_ms"]<b["source_ms"] for a,b in zip(points,points[1:])),"history_time_order")
        references=row["references"]
        require(references["mapping_version"]==snapshot["mapping_version"]
                and references["generation_id"]==snapshot["generation"],"reference_version_mismatch")
        if references["status"]!="available":
            require(all(f["status"]!="available" for f in row["changes"].values()),"change_without_reference")
            continue
        ladder=references["ladder"]
        require(ladder["start_ms"]==ladder["built_at_ms"]//60000*60000
                and ladder["valid_until_ms"]==ladder["start_ms"]+300000
                and ladder["built_at_ms"]<=generated+5000 and generated<ladder["valid_until_ms"],"ladder_time_order")
        for slot,points in enumerate(ladder["slots"]):
            for point,window in zip(points,WINDOWS.values()):
                require(point["reference_minute_ms"]==ladder["start_ms"]+slot*60000-window,"reference_minute_mismatch")
                if point["status"]=="available": price(point["open_price"])
        fallback=references["fallback"]
        if fallback is None:
            require(all(f["status"]!="available" for f in row["changes"].values()),"change_without_fallback")
            continue
        p=row["price"]
        require(p["status"]=="available" and fallback["source_ms"]==p["asof_ms"]
                and Decimal(fallback["price"])==Decimal(p["value"])
                and fallback["expires_ms"]<=p["expires_ms"],"fallback_numerator_mismatch")
        for h,window in WINDOWS.items():
            point=fallback["references"][h]
            require(point["reference_minute_ms"]==(fallback["source_ms"]-window)//60000*60000,"fallback_reference_minute")
            change=row["changes"][h]
            if change["status"]!="available": continue
            require(point["status"]=="available" and change["asof_ms"]==fallback["source_ms"],"change_operand_unavailable")
            price(point["open_price"])
            with localcontext(Context(prec=34)):
                expected=(Decimal(fallback["price"])-Decimal(point["open_price"]))/Decimal(point["open_price"])
            require(Decimal(change["value"])==expected,"price_change_operand_mismatch")
    require(snapshot["generation"]==generation(snapshot),"generation_digest_mismatch")
