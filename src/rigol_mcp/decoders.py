"""Hardware serial decoder configuration with bounded event-table reads."""
import csv
from datetime import datetime, timezone
import io as textio
import json
import math
import re
import time

from .operations import UART_FIELDS, I2C_FIELDS, SPI_FIELDS, BOOL, choice, number, validate
from .workflows import idle_for_control, differences

GROUPS = {"UART": UART_FIELDS, "IIC": I2C_FIELDS, "SPI": SPI_FIELDS}
COMMON = {"MODE": choice("PAR", "UART", "IIC", "SPI"), "DISP": BOOL,
          "FORM": choice("HEX", "ASC", "DEC", "BIN", "LINE"), "THRE:AUTO": BOOL,
          "THRE:CHAN1": number("threshold"), "THRE:CHAN2": number("threshold")}
TABLE = {"DISP": BOOL, "FORM": choice("HEX", "ASC", "DEC"), "VIEW": choice("PACK", "DET", "PAYL"),
         "SORT": choice("ASC", "DESC")}
EXTRA = {"SPI:MISO": choice("CHAN1", "CHAN2", "OFF"), "SPI:MODE": choice("CS", "TIM")}
FIELDS = {**COMMON, **{suffix: spec for group in GROUPS.values() for suffix, spec in group.values()}, **EXTRA}


def literal(value, spec):
    if spec["type"] == "boolean":
        if value not in ("0", "1"):
            raise ValueError("Invalid decoder boolean")
    elif spec["type"] in ("number", "integer"):
        v = float(value)
        if not math.isfinite(v) or spec["type"] == "integer" and v != int(v):
            raise ValueError("Invalid decoder numeric reply")
        if "minimum" in spec and v < spec["minimum"] or "maximum" in spec and v > spec["maximum"]:
            raise ValueError("Decoder setting outside supported range")
        if "enum" in spec and v not in spec["enum"]:
            raise ValueError("Invalid decoder numeric enum")
        return f"{v:.12g}"
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError("Invalid decoder enum")
    return value


def read_settings(io, decoder):
    fields = {suffix: literal(io.query(f":DEC{decoder}:{suffix}?"), spec) for suffix, spec in FIELDS.items()}
    table = {suffix: literal(io.query(f":ETAB{decoder}:{suffix}?"), spec) for suffix, spec in TABLE.items()}
    return {"decoder": decoder, "settings": fields, "event_table": table}


def compare(expected, actual):
    fields = dict(expected["settings"])
    if fields["THRE:AUTO"] == "1":
        fields.pop("THRE:CHAN1")
        fields.pop("THRE:CHAN2")
    return differences(fields, actual["settings"]) + differences(expected["event_table"], actual["event_table"])


def classify_differences(items):
    hard, quantized = [], []
    for item in items:
        step = {"SPI:TIM": 1.01e-9, "UART:BAUD": 1.01}.get(item["setting"])
        if step is not None and abs(float(item["actual"]) - float(item["expected"])) <= step:
            quantized.append(item)
        else:
            hard.append(item)
    return hard, quantized

def save_backup(store, state, identity, simulated):
    return store.put(json.dumps(state).encode(), {"ok": True, "instrument": identity, "simulated": simulated,
                     "artifact_type": "decoder.settings", "created_at_utc": datetime.now(timezone.utc).isoformat()})


def configure(scope, store, protocol, arguments):
    decoder = arguments["decoder"]
    mode = {"uart": "UART", "i2c": "IIC", "spi": "SPI"}[protocol]
    defaults = {"uart": {"data_bits": 8, "stop_bits": 1, "parity": "NONE", "polarity": "POS", "bit_order": "LSB"},
                "i2c": {"address_format": "NORM"},
                "spi": {"data_bits": 8, "polarity": "POS", "bit_order": "MSB"}}
    args = {**defaults[protocol], **arguments}
    sources = [args[k] for k in (("tx", "rx") if protocol == "uart" else ("clock", "data")) if args[k] != "OFF"]
    if not sources or len(sources) != len(set(sources)):
        raise ValueError("Choose distinct physical channels; UART TX and RX cannot both be OFF")
    if args.get("auto_threshold") is True and any(k in args for k in ("threshold_ch1", "threshold_ch2")):
        raise ValueError("Manual thresholds and auto_threshold=true are mutually exclusive")
    with scope.session(control=True) as (io, identity):
        idle_for_control(io)
        for source in sources:
            scope._enabled(io, int(source[-1]))
        for c in (1, 2):
            if f"threshold_ch{c}" in args:
                scale, offset = float(io.query(f":CHAN{c}:SCAL?")), float(io.query(f":CHAN{c}:OFFS?"))
                if not -4 * scale - offset <= args[f"threshold_ch{c}"] <= 4 * scale - offset:
                    raise ValueError(f"CH{c} threshold is outside ±4 divisions minus offset")
        scope._check_error(io, "before decoder configuration")
        before = read_settings(io, decoder)
        scope._check_error(io, "reading decoder setup")
        backup = save_backup(store, before, identity, scope.simulated)
        desired = {"MODE": mode, "FORM": "HEX"}
        desired.update({suffix: str(args[key]) for key, (suffix, _) in GROUPS[mode].items()})
        if mode == "SPI":
            desired.update({"SPI:MODE": "TIM", "SPI:MISO": "OFF"})
        manual = any(k in args for k in ("threshold_ch1", "threshold_ch2"))
        desired["THRE:AUTO"] = "0" if manual or args.get("auto_threshold") is False else "1"
        for c in (1, 2):
            if f"threshold_ch{c}" in args:
                desired[f"THRE:CHAN{c}"] = str(args[f"threshold_ch{c}"])
        desired["DISP"] = str(int(args.get("enabled", True)))
        table = {"FORM": "HEX", "VIEW": "PACK", "SORT": "ASC", "DISP": desired["DISP"]}
        return apply(scope, io, decoder, desired, table, backup["artifact_id"], before)


def apply(scope, io, decoder, desired, table, backup_id, before):
    completed, actual = [], None
    try:
        # Enable a requested UART source before disabling the other, never transient OFF/OFF.
        order = list(desired)
        for key in ("UART:TX", "UART:RX"):
            if key in order and desired[key] != "OFF":
                order.remove(key)
                order.insert(1, key)
        # SPI timeout is only applicable in TIM mode.
        if "SPI:MODE" in order:
            order.remove("SPI:MODE")
            order.insert(1, "SPI:MODE")
        for suffix in order:
            value = literal(desired[suffix], FIELDS[suffix])
            if not differences({suffix: value}, before["settings"]):
                continue
            io.write(f":DEC{decoder}:{suffix} {value}")
            time.sleep(.15)
            scope._check_error(io, "setting decoder " + suffix)
            completed.append(suffix)
        if desired.get("DISP") != "0":
            # Configure the visible package view first. PAYL/DET do not support sorting.
            ordered_table = [("DISP", "1"), ("VIEW", "PACK"),
                             ("FORM", table.get("FORM", before["event_table"]["FORM"])),
                             ("SORT", table.get("SORT", before["event_table"]["SORT"])),
                             ("VIEW", table.get("VIEW", before["event_table"]["VIEW"])),
                             ("DISP", table.get("DISP", before["event_table"]["DISP"]))]
            for suffix, value in ordered_table:
                if io.query(f":ETAB{decoder}:{suffix}?") == value:
                    continue
                io.write(f":ETAB{decoder}:{suffix} {literal(value, TABLE[suffix])}")
                time.sleep(.15)
                scope._check_error(io, "setting event table " + suffix)
        actual = read_settings(io, decoder)
        scope._check_error(io, "reading decoder back")
        expected = {"settings": {**before["settings"], **desired},
                    "event_table": before["event_table"] if desired.get("DISP") == "0" else {**before["event_table"], **table}}
        mismatch, quantized = classify_differences(compare(expected, actual))
        return {"ok": not mismatch, "simulated": scope.simulated, "decoder": decoder, "actual": actual,
                "rollback_artifact_id": backup_id, "readback_differences": mismatch, "quantized_settings": quantized,
                "note": "Actual readback is authoritative. One-step rounding (1 ns SPI timeout, 1 baud UART) is reported separately. This does not validate the signal or decoded content."}
    except Exception as exc:
        return {"ok": False, "simulated": scope.simulated, "problem": str(exc), "completed_fields": completed,
                "rollback_artifact_id": backup_id, "actual": actual,
                "next_action": "Inspect decoder state; use decode.restore with the backup ID. No automatic retry."}


def restore(scope, store, artifact_id):
    meta, raw = store.load(artifact_id)
    saved = json.loads(raw)
    decoder = saved["decoder"]
    if type(decoder) is not int or decoder not in (1, 2) or set(saved["settings"]) != set(FIELDS) or set(saved["event_table"]) != set(TABLE):
        raise ValueError("Invalid saved decoder configuration")
    for key, value in saved["settings"].items():
        literal(value, FIELDS[key])
    for key, value in saved["event_table"].items():
        literal(value, TABLE[key])
    with scope.session(control=True) as (io, identity):
        if identity != meta["instrument"] or scope.simulated != meta["simulated"]:
            raise ValueError("Decoder backup belongs to a different instrument/firmware")
        idle_for_control(io)
        scope._check_error(io, "before decoder restore")
        before = read_settings(io, decoder)
        backup = save_backup(store, before, identity, scope.simulated)
        fields = saved["settings"]
        # Restore only changed fields, with saved mode selected last. Inactive protocol registers are independent.
        desired = {k: v for k, v in fields.items() if before["settings"][k] != v and k not in ("MODE", "DISP", "THRE:CHAN1", "THRE:CHAN2")}
        if fields["THRE:AUTO"] == "0":
            desired = {"THRE:AUTO": "0", **desired, "THRE:CHAN1": fields["THRE:CHAN1"], "THRE:CHAN2": fields["THRE:CHAN2"]}
        desired.update({"MODE": fields["MODE"], "DISP": "1"})
        result = apply(scope, io, decoder, desired, saved["event_table"], backup["artifact_id"], before)
        if result["ok"]:
            io.write(f":DEC{decoder}:DISP {fields['DISP']}")
            time.sleep(.15)
            scope._check_error(io, "restoring decoder enable state")
            actual = read_settings(io, decoder)
            result["actual"] = actual
            hard, quantized = classify_differences(compare(saved, actual))
            result["readback_differences"], result["quantized_settings"] = hard, quantized
            result["ok"] = not hard
        return result


def parse_table(data):
    text = data.decode("ascii").strip("\x00\r\n ")
    if not text:
        return []
    # Preserve raw firmware cells. Delimiter selection is explicit, not semantic guessing.
    delimiter = "\t" if "\t" in text else ","
    return list(csv.reader(textio.StringIO(text), delimiter=delimiter))


def normalized_events(rows, fmt):
    """Normalize only observed/documented column labels; preserve unknown firmware cells."""
    header_index = next((i for i, row in enumerate(rows) if row and row[0].strip().lower() == "time"), None)
    if header_index is None:
        return [{"firmware_cells": row} for row in rows], []
    columns = [value.strip() for value in rows[header_index] if value.strip()]
    result = []
    for row in rows[header_index + 1:]:
        if not any(cell.strip() for cell in row):
            continue
        item = {}
        for index, column in enumerate(columns):
            value = row[index].strip() if index < len(row) else ""
            key = column.lower()
            if key == "time":
                match = re.fullmatch(r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\s*(ns|us|ms|s)", value)
                if match:
                    item["time_seconds"] = float(match[1]) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1}[match[2]]
                else:
                    item["time_text"] = value
            elif key in ("tx", "rx", "miso", "mosi", "data") and fmt == "HEX" and re.fullmatch(r"(?:0[xX])?[0-9A-Fa-f]+", value):
                item[key] = int(value, 16)
            elif value:
                item[key] = value
        result.append(item)
    return result, columns

def events(scope, decoder, start=0, count=20):
    with scope.session() as (io, _):
        if io.query(":TRIG:STAT?") != "STOP":
            raise ValueError("Stop acquisition before reading a coherent decoder event table")
        before = read_settings(io, decoder)
        if before["settings"]["DISP"] != "1" or before["event_table"]["DISP"] != "1":
            raise ValueError("Enable the decoder and its event table with protocol configure first")
        scope._check_error(io, "before event table")
        empty = io.query(f":ETAB{decoder}:ROW?") == "0"
        rows = [] if empty else parse_table(io.binary(f":ETAB{decoder}:DATA?", 1024 * 1024))
        if io.query(":TRIG:STAT?") != "STOP" or compare(before, read_settings(io, decoder)):
            raise RuntimeError("Acquisition or decoder settings changed during table read")
        scope._check_error(io, "reading event table")
        decoded, columns = normalized_events(rows, before["event_table"]["FORM"])
        end, used_bytes = start, 0
        for item in decoded[start:start + count]:
            size = len(json.dumps(item))
            if used_bytes + size > 16000:
                if end == start:
                    raise ValueError("One firmware event exceeds the compact response limit; inspect the event on a screenshot")
                break
            used_bytes += size
            end += 1
        return {"ok": True, "simulated": scope.simulated, "decoder": decoder, "protocol": before["settings"]["MODE"],
                "format": before["event_table"]["FORM"], "columns": columns, "start": start, "events": decoded[start:end],
                "total_events_in_transfer": len(decoded), "next_start": end if end < len(decoded) else None,
                "note": "Recognized Time cells converted to seconds; single HEX data cells converted to integers. Other firmware fields preserved, without inferred protocol semantics. Event-table times may be rounded. Empty table does not prove no traffic. Not full-memory host decoding."}
