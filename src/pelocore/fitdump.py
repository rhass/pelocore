"""Raw FIT record walker for debugging nonstandard files.

fit_tool's strict profile projection rejects real-world files such as
COROS's own activity exports (nonstandard field sizes and record
headers). This module decodes the raw record stream directly and makes
no profile assumptions beyond base types, so vendor quirks surface as
hex fallbacks instead of exceptions.

CLI: ``pelocore fitdump <file> [--json] [--records N] [--trace]``
"""

from __future__ import annotations

import argparse
import json
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
FIT_EPOCH_OFFSET_S = 631_065_600

#: base type id -> (name, size in bytes)
BASE_TYPES: dict[int, tuple[str, int]] = {
    0: ("enum", 1),
    1: ("sint8", 1),
    2: ("uint8", 1),
    3: ("sint16", 2),
    4: ("uint16", 2),
    5: ("sint32", 4),
    6: ("uint32", 4),
    7: ("string", 1),
    8: ("float32", 4),
    9: ("float64", 8),
    10: ("uint8z", 1),
    11: ("uint16z", 2),
    12: ("uint32z", 4),
    13: ("byte", 1),
}

MESSAGE_NAMES: dict[int, str] = {
    0: "file_id",
    1: "capabilities",
    2: "device_settings",
    3: "user_profile",
    6: "hrv",
    7: "hr_zone",
    18: "session",
    19: "lap",
    20: "record",
    21: "event",
    22: "device_settings",
    23: "device_info",
    26: "workout",
    27: "workout_step",
    28: "video",
    # fit_tool's profile (21.212) and COROS both write the activity message
    # as gnum 34; the same profile numbers exercise_title 264.
    34: "activity",
    49: "hr",
    72: "timestamp_correlation",
    78: "hrv",
    79: "workout_step",
    101: "length",
    104: "segment_lap?",
    125: "speed_zone",
    131: "video_frame?",
    140: "activity",
    145: "software",
    147: "sensor?",
    160: "gps_metadata",
    164: "weather?",
    166: "cd?",
    206: "developer_data_id",
    207: "field_description",
    225: "set",
    229: "exercise_title",
    264: "exercise_title",
}

FILE_TYPES: dict[int, str] = {
    1: "device", 2: "settings", 3: "sport_settings", 4: "activity",
    5: "workout", 6: "course", 7: "schedules", 9: "weight",
}

SPORTS: dict[int, str] = {
    0: "generic", 1: "running", 2: "cycling", 3: "transition",
    4: "fitness_equipment", 5: "swimming", 10: "training", 11: "walking",
    12: "cross_country_skiing", 13: "alpine_skiing", 15: "rowing",
    17: "hiking", 20: "elliptical?", 21: "e_biking", 25: "hunting?",
    36: "yoga?", 62: "hiit?", 67: "meditation",
}

SUB_SPORTS: dict[int, str] = {
    0: "generic", 1: "treadmill", 5: "rower?", 6: "indoor_cycling",
    11: "pool?", 14: "indoor_rowing", 15: "elliptical", 19: "flexibility_training",
    20: "strength_training", 25: "indoor_skiing?", 26: "cardio_training",
    27: "indoor_walking", 43: "yoga", 44: "pilates", 45: "indoor_running",
    62: "breathing?", 70: "hiit",
}

ENUM_NAMES: dict[str, dict[int, str]] = {
    "file_type": FILE_TYPES,
    "sport": SPORTS,
    "sub_sport": SUB_SPORTS,
}


@dataclass
class FieldDef:
    number: int
    size: int
    base_type: int


@dataclass
class Definition:
    local_id: int
    global_id: int
    fields: list[FieldDef]
    dev_fields: list[tuple[int, int]]
    big_endian: bool = False


@dataclass
class DecodedMessage:
    global_id: int
    name: str
    local_id: int
    values: dict[str, Any]
    raw: bytes


@dataclass
class FitDump:
    path: Path | None = None
    header: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    messages: list[DecodedMessage] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)

    def message_count(self, global_id: int) -> int:
        return self.counts.get(MESSAGE_NAMES.get(global_id, str(global_id)), 0)


def _header(data: bytes) -> dict[str, Any]:
    length = data[0]
    protocol_byte = data[1]
    protocol = f"{protocol_byte >> 4}.{protocol_byte & 0x0F}"
    profile = struct.unpack("<H", data[2:4])[0]
    data_size = struct.unpack("<I", data[4:8])[0]
    magic_ok = data[8:12] == b".FIT"
    return {
        "header_length": length,
        "protocol_version": protocol,
        "profile_version": profile / 100.0,
        "magic_ok": magic_ok,
        "data_size": data_size,
    }


def _decode_value(
    chunk: bytes, base_type: int, big_endian: bool
) -> Any:
    name, _size = BASE_TYPES.get(base_type & 0x1F, ("byte", len(chunk)))
    if chunk and all(byte == 0xFF for byte in chunk):
        return None
    endian = ">" if big_endian else "<"
    if name == "string":
        return chunk.split(b"\x00")[0].decode("utf-8", "replace")
    if name == "byte":
        return chunk.hex()
    if name in ("enum", "uint8", "uint8z"):
        return chunk[0]
    if name == "sint8":
        return struct.unpack("b", chunk[:1])[0]
    if name in ("uint16", "uint16z"):
        return struct.unpack(endian + "H", chunk[:2])[0]
    if name == "sint16":
        return struct.unpack(endian + "h", chunk[:2])[0]
    if name in ("uint32", "uint32z"):
        return struct.unpack(endian + "I", chunk[:4])[0]
    if name == "sint32":
        return struct.unpack(endian + "i", chunk[:4])[0]
    if name == "float32":
        return struct.unpack(endian + "f", chunk[:4])[0]
    if name == "float64":
        return struct.unpack(endian + "d", chunk[:8])[0]
    return chunk.hex()


def _is_timestamp_field(global_id: int, number: int) -> bool:
    if number == 253:
        return True
    if global_id == 0 and number == 4:
        return True
    return global_id in (18, 19, 20, 21, 140, 225) and number == 2


def _format_timestamp(raw_s: int) -> str:
    dt = FIT_EPOCH.replace(tzinfo=None) + timedelta(seconds=raw_s)
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class FitWalker:
    """Walk raw FIT records; tolerant of nonstandard vendor encodings."""

    def __init__(self, *, verbose_trace: bool = False):
        self.definitions: dict[int, Definition] = {}
        self.dump = FitDump()
        self._verbose_trace = verbose_trace
        self._last_timestamp_s: int | None = None
        self._records_seen = 0

    def walk(self, data: bytes, *, records_limit: int = 0) -> FitDump:
        self.dump.header = _header(data)
        pos = data[0]
        crc_end = len(data) - 2
        record_count = 0
        while pos < crc_end:
            header = data[pos]
            if header & 0x80:  # compressed timestamp record
                local_id = (header >> 5) & 0x03
                offset_s = header & 0x1F
                definition = self.definitions.get(local_id)
                if definition is None:
                    raise ValueError(
                        f"compressed timestamp record references undefined local id {local_id}"
                    )
                payload_size = self._definition_size(definition)
                values = self._decode(
                    definition, data[pos + 1 : pos + 1 + payload_size],
                    timestamp_offset_s=offset_s,
                )
                payload = data[pos + 1 : pos + 1 + payload_size]
                self._record(definition, values, payload, records_limit)
                self._trace_line(f"cts local={local_id} +{offset_s}s")
                pos += 1 + payload_size
                record_count += 1
            elif header & 0x40:  # definition message
                definition, pos = self._parse_definition(data, pos + 1, header)
                self._trace_line(
                    f"def local={definition.local_id} gnum={definition.global_id} "
                    f"fields={len(definition.fields)} dev={len(definition.dev_fields)}"
                )
            else:  # data message
                local_id = header & 0x0F
                definition = self.definitions.get(local_id)
                if definition is None:
                    raise ValueError(
                        f"data record references undefined local id {local_id} at byte {pos}"
                    )
                payload_size = self._definition_size(definition)
                payload = data[pos + 1 : pos + 1 + payload_size]
                values = self._decode(definition, payload)
                self._record(definition, values, payload, records_limit)
                self._trace_line(
                    f"data local={local_id} gnum={definition.global_id} bytes={payload_size}"
                )
                pos += 1 + payload_size
                record_count += 1
        self.dump.header["records"] = record_count
        return self.dump

    def _definition_size(self, definition: Definition) -> int:
        native = sum(f.size for f in definition.fields)
        dev = sum(size for _index, size in definition.dev_fields)
        return native + dev

    def _parse_definition(self, data: bytes, pos: int, header: int) -> tuple[Definition, int]:
        big_endian = bool(header & 0x20)
        developer_flag = bool(header & 0x10)
        local_id = header & 0x0F
        _reserved = data[pos]
        _arch = data[pos + 1]
        global_id = struct.unpack(">H" if big_endian else "<H", data[pos + 2 : pos + 4])[0]
        count = data[pos + 4]
        pos += 5
        fields = [
            FieldDef(
                number=data[pos + i * 3],
                size=data[pos + i * 3 + 1],
                base_type=data[pos + i * 3 + 2],
            )
            for i in range(count)
        ]
        pos += 3 * count
        dev_fields: list[tuple[int, int]] = []
        if developer_flag:
            dev_count = data[pos]
            pos += 1
            dev_fields = [(data[pos + i * 2], data[pos + i * 2 + 1]) for i in range(dev_count)]
            pos += 2 * dev_count
        definition = Definition(
            local_id=local_id, global_id=global_id, fields=fields,
            dev_fields=dev_fields, big_endian=big_endian,
        )
        self.definitions[local_id] = definition
        return definition, pos

    def _decode(
        self, definition: Definition, payload: bytes, *, timestamp_offset_s: int | None = None
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        pos = 0
        for field_def in definition.fields:
            chunk = payload[pos : pos + field_def.size]
            pos += field_def.size
            value = _decode_value(chunk, field_def.base_type, definition.big_endian)
            key = f"field_{field_def.number}"
            if value is None:
                values[key] = None
                continue
            is_ts = _is_timestamp_field(definition.global_id, field_def.number)
            if is_ts and isinstance(value, int):
                self._last_timestamp_s = value
                values[key] = _format_timestamp(value)
            else:
                values[key] = self._annotate(definition.global_id, field_def.number, value)
        for index, (dev_index, dev_size) in enumerate(definition.dev_fields):
            chunk = payload[pos : pos + dev_size]
            pos += dev_size
            values[f"dev{dev_index}_{index}"] = chunk.hex()
        if timestamp_offset_s is not None and self._last_timestamp_s is not None:
            values["compressed_ts"] = _format_timestamp(
                self._last_timestamp_s + timestamp_offset_s
            )
        return values

    def _annotate(self, global_id: int, number: int, value: Any) -> Any:
        if not isinstance(value, int):
            return value
        if number == 0 and global_id == 0 and value in FILE_TYPES:
            return f"{value} ({FILE_TYPES[value]})"
        if number == 5 and global_id == 18 and value in SPORTS:
            return f"{value} ({SPORTS[value]})"
        if number == 6 and global_id == 18 and value in SUB_SPORTS:
            return f"{value} ({SUB_SPORTS[value]})"
        if number == 6 and global_id == 19 and value in SPORTS:
            return f"{value} ({SPORTS[value]})"
        if number == 7 and global_id == 19 and value in SUB_SPORTS:
            return f"{value} ({SUB_SPORTS[value]})"
        return value

    def _record(
        self,
        definition: Definition,
        values: dict[str, Any],
        raw: bytes,
        records_limit: int,
    ) -> None:
        name = MESSAGE_NAMES.get(definition.global_id, f"gnum_{definition.global_id}")
        self.dump.counts[name] = self.dump.counts.get(name, 0) + 1
        if name == "record":
            self._records_seen += 1
            if not (records_limit and self._records_seen <= records_limit):
                return  # records are voluminous; summarized by count
        self.dump.messages.append(
            DecodedMessage(
                global_id=definition.global_id, name=name,
                local_id=definition.local_id, values=values, raw=raw,
            )
        )

    def _trace_line(self, line: str) -> None:
        if self._verbose_trace:
            self.dump.trace.append(line)


def dump_bytes(
    data: bytes,
    *,
    as_json: bool = False,
    records_limit: int = 0,
    trace: bool = False,
) -> str:
    walker = FitWalker(verbose_trace=trace)
    dump = walker.walk(data, records_limit=records_limit)
    payload = {
        "header": dump.header,
        "counts": dump.counts,
        "messages": [
            {"name": m.name, "global_id": m.global_id, "values": m.values}
            for m in dump.messages
        ],
    }
    if trace:
        payload["trace"] = dump.trace
    if as_json:
        return json.dumps(payload, indent=2, default=str)
    return _render_text(dump, "<bytes>")


def dump_file(
    path: str | Path,
    *,
    as_json: bool = False,
    records_limit: int = 0,
    trace: bool = False,
) -> str:
    data = Path(path).read_bytes()
    walker = FitWalker(verbose_trace=trace)
    dump = walker.walk(data, records_limit=records_limit)
    payload = {
        "file": str(path),
        "size": len(data),
        "header": dump.header,
        "counts": dump.counts,
        "messages": [
            {"name": m.name, "global_id": m.global_id, "values": m.values}
            for m in dump.messages
        ],
    }
    if trace:
        payload["trace"] = dump.trace
    if as_json:
        return json.dumps(payload, indent=2, default=str)
    return _render_text(dump, path)


def _render_text(dump: FitDump, path: str | Path) -> str:
    lines = [f"file: {path} | header: {dump.header}"]
    lines.append("messages: " + ", ".join(f"{k}={v}" for k, v in sorted(dump.counts.items())))
    for message in dump.messages:
        lines.append(f"-- {message.name} (gnum {message.global_id}, local {message.local_id})")
        for key, value in sorted(message.values.items()):
            lines.append(f"   {key} = {value!r}")
        if len(message.raw) > 32:
            lines.append(f"   raw: {message.raw[:32].hex()}... ({len(message.raw)} bytes)")
        else:
            lines.append(f"   raw: {message.raw.hex()}")
    for line in dump.trace:
        lines.append(f"[trace] {line}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pelocore-fitdump", description="Raw FIT file dumper")
    parser.add_argument("file", help="FIT file to inspect")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument(
        "--records", type=int, default=0, metavar="N", help="dump first N record messages"
    )
    parser.add_argument("--trace", action="store_true", help="per-record header walk")
    args = parser.parse_args(argv)
    print(dump_file(args.file, as_json=args.json, records_limit=args.records, trace=args.trace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
