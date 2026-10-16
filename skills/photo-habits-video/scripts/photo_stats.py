#!/usr/bin/env python3
"""Read-only EXIF inventory, conservative capture grouping, and photo statistics.

The data layer uses only Python's standard library. ExifTool is an external,
user-supplied executable; this script neither downloads it nor writes to photos.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path, PureWindowsPath

SCHEMA_VERSION = 1
RAW = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw", ".raw", ".3fr", ".iiq"}
JPEG = {".jpg", ".jpeg"}
EXTENSIONS = RAW | JPEG | {".heic", ".heif", ".tif", ".tiff", ".png", ".webp", ".avif"}
TAGS = ["Make", "Model", "LensModel", "Lens", "LensID", "ISO", "RecommendedExposureIndex",
        "StandardOutputSensitivity", "ISOSpeed", "SensitivityType", "FNumber", "FocalLength",
        "FocalLengthIn35mmFormat", "ExposureTime", "DateTimeOriginal", "SubSecTimeOriginal",
        "SubSecDateTimeOriginal", "OffsetTimeOriginal", "SerialNumber", "BodySerialNumber",
        "ImageUniqueID", "FileType", "ImageWidth", "ImageHeight", "Warning", "Error"]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        temporary = stream.name
    os.replace(temporary, path)


def digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def path_key(value):
    value = str(value)
    if re.match(r"^[A-Za-z]:[\\/]", value) or "\\" in value:
        return str(PureWindowsPath(value)).casefold()
    return os.path.normcase(os.path.normpath(value))


def photo_path(value):
    return PureWindowsPath(value) if re.match(r"^[A-Za-z]:[\\/]", str(value)) or "\\" in str(value) else Path(value)


def number(value):
    try:
        if isinstance(value, str) and "/" in value:
            left, right = value.split("/", 1)
            result = float(left) / float(right)
        else:
            result = float(value)
        return round(result, 10) if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def clean(value):
    return str(value or "").strip().casefold()


def iso_value(row):
    direct = number(row.get("ISO"))
    if direct != 65535:
        return direct, None
    fields = {1: ("StandardOutputSensitivity",), 2: ("RecommendedExposureIndex",), 3: ("ISOSpeed",),
              4: ("StandardOutputSensitivity", "RecommendedExposureIndex"),
              5: ("StandardOutputSensitivity", "ISOSpeed"), 6: ("RecommendedExposureIndex", "ISOSpeed"),
              7: ("StandardOutputSensitivity", "RecommendedExposureIndex", "ISOSpeed")}
    kind = number(row.get("SensitivityType"))
    if kind is not None and kind not in fields:
        return None, "unsupported_sensitivity_type"
    tags = fields.get(kind, ("RecommendedExposureIndex", "StandardOutputSensitivity", "ISOSpeed"))
    evidence = {number(row.get(tag)) for tag in tags}
    evidence.discard(None)
    evidence.discard(65535)
    if len(evidence) == 1:
        return evidence.pop(), "recovered_from_extended_iso"
    return None, "unresolved_iso_sentinel" if not evidence else "conflicting_extended_iso"


def capture_datetime(row):
    raw = row.get("DateTimeOriginal") or row.get("SubSecDateTimeOriginal")
    if not raw:
        return None
    value = str(raw).strip()
    # Preserve camera-local wall time. Do not convert an unknown travel timezone.
    value = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", value)
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def subseconds(row):
    raw = row.get("SubSecTimeOriginal", row.get("SubsecTimeOriginal"))
    if raw is None:
        match = re.search(r"\d{2}:\d{2}:\d{2}\.(\d+)", str(row.get("SubSecDateTimeOriginal", "")))
        raw = match.group(1) if match else ""
    value = str(raw).strip()
    if value.startswith("0."):
        value = value[2:]
    return (value.rstrip("0") or "0") if value.isdigit() else ""


def normalized(row):
    iso, note = iso_value(row)
    return {"camera": str(row.get("Model") or "").strip(),
            "lens": str(row.get("LensModel") or row.get("Lens") or "").strip(),
            "focal_length": number(row.get("FocalLength")), "aperture": number(row.get("FNumber")),
            "iso": iso, "shutter": number(row.get("ExposureTime")),
            "datetime": capture_datetime(row), "subseconds": subseconds(row), "iso_note": note}


def compatible(a, b):
    for tag in ("Make", "LensModel", "SerialNumber", "BodySerialNumber", "OffsetTimeOriginal"):
        if clean(a.get(tag)) and clean(b.get(tag)) and clean(a[tag]) != clean(b[tag]):
            return False
    # A serial may move between ExifTool's serial aliases across file formats.
    sa = clean(a.get("BodySerialNumber") or a.get("SerialNumber"))
    sb = clean(b.get("BodySerialNumber") or b.get("SerialNumber"))
    la = clean(a.get("LensModel") or a.get("Lens"))
    lb = clean(b.get("LensModel") or b.get("Lens"))
    return not ((sa and sb and sa != sb) or (la and lb and la != lb))


def settings_key(row, include_subseconds=True):
    item = normalized(row)
    required = [item[key] for key in ("focal_length", "aperture", "iso", "shutter")]
    if not item["camera"] or not item["datetime"] or any(value is None for value in required):
        return None
    return (clean(item["camera"]), item["datetime"].isoformat(),
            *((item["subseconds"],) if include_subseconds else ()), *required)


def volume_info(root):
    info = {"root": str(root), "device_id": str(root.stat().st_dev), "volume_label": None, "volume_serial": None, "volume_guid": None}
    if os.name == "nt":
        import ctypes
        drive = root.anchor
        label = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong()
        fs = ctypes.create_unicode_buffer(261)
        maximum = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, label, 261, ctypes.byref(serial), ctypes.byref(maximum), ctypes.byref(flags), fs, 261):
            info.update(volume_label=label.value, volume_serial=f"{serial.value:08X}", filesystem=fs.value)
        guid = ctypes.create_unicode_buffer(261)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(drive, guid, 261):
            info["volume_guid"] = guid.value
    return info


def content_hash(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def scan(args):
    roots = [Path(value).expanduser().resolve() for value in args.root]
    if not args.batch_id.strip():
        raise ValueError("A nonempty --batch-id is required; use a new ID for each physical-disk import.")
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Photo root does not exist: {root}")
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "scan_state.json"
    prior = read_json(state_path) if state_path.exists() else {}
    volumes = [volume_info(root) for root in roots]
    provenance = {"batch_id": args.batch_id, "roots": [str(root) for root in roots], "volumes": volumes}
    if prior and prior.get("provenance") != provenance:
        raise ValueError("This output folder belongs to a different batch/root/volume. Choose a new --output folder and batch ID.")
    inventory, seen = [], set()
    for root, volume in zip(roots, volumes):
        for folder, directories, names in os.walk(root):
            directories.sort()
            for name in sorted(names):
                path = Path(folder) / name
                if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                    continue
                key = path_key(path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    stat = path.stat()
                    row = {"SourceFile": str(path), "SourceRoot": str(root), "SourceBatch": args.batch_id,
                           "FileBytes": stat.st_size, "FileMTimeNs": stat.st_mtime_ns,
                           "VolumeProvenance": volume}
                    row["SourceRecordID"] = digest(args.batch_id, key, stat.st_size, stat.st_mtime_ns)
                    inventory.append(row)
                except OSError as error:
                    print(f"Inventory read failed: {error}", file=sys.stderr)
    write_json(out / "inventory.json", inventory)
    metadata_path = out / "metadata.json"
    old_rows = read_json(metadata_path) if metadata_path.exists() else []
    cache = {row["SourceRecordID"]: row for row in old_rows}
    rows = []
    for item in inventory:
        previous = cache.get(item["SourceRecordID"], {})
        rows.append({**item, **previous})
    pending = [index for index, row in enumerate(rows) if not row.get("MetadataReadComplete")]
    executable = ([args.perl] if args.perl else []) + [args.exiftool]
    errors = []
    for offset in range(0, len(pending), args.chunk_size):
        indexes = pending[offset:offset + args.chunk_size]
        file_rows = [rows[index] for index in indexes]
        # A UTF-8 argument file avoids Windows' command-line length limit.
        argument_file = out / "exiftool_paths.txt"
        if any("\n" in row["SourceFile"] or "\r" in row["SourceFile"] for row in file_rows):
            raise ValueError("Newlines in photo filenames are unsupported by ExifTool argument files.")
        argument_file.write_text("\n".join(row["SourceFile"] for row in file_rows) + "\n", encoding="utf-8")
        command = executable + ["-json", "-n", "-fast2", "-charset", "filename=UTF8"] + ["-" + tag for tag in TAGS] + ["-@", str(argument_file)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace", check=False)
        try:
            extracted = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as error:
            raise RuntimeError("ExifTool did not return JSON. Check --exiftool and optional --perl.") from error
        by_path = {path_key(row.get("SourceFile", "")): row for row in extracted}
        for index in indexes:
            item = rows[index]
            exif = by_path.get(path_key(item["SourceFile"]))
            if exif is None:
                item["MetadataReadError"] = "No ExifTool result"
            else:
                item.update({key: value for key, value in exif.items() if key != "SourceFile"})
                item["MetadataReadComplete"] = not bool(exif.get("Error"))
                if exif.get("Error"):
                    item["MetadataReadError"] = str(exif["Error"])
        if result.stderr.strip():
            errors.append(result.stderr[-4000:])
        write_json(metadata_path, rows)
        write_json(state_path, {"schema_version": SCHEMA_VERSION, "provenance": provenance, "phase": "metadata", "completed": sum(bool(row.get("MetadataReadComplete")) for row in rows), "total": len(rows), "errors": errors})
        print(f"Metadata checkpoint: {min(offset + len(indexes), len(pending))}/{len(pending)} pending files", flush=True)
    sizes = Counter(row["FileBytes"] for row in rows)
    for index, row in enumerate(rows):
        selected = args.hash_mode == "full" or (args.hash_mode == "candidates" and sizes[row["FileBytes"]] > 1)
        if selected and not row.get("SHA256"):
            try:
                stat = Path(row["SourceFile"]).stat()
                if stat.st_size != row["FileBytes"] or stat.st_mtime_ns != row["FileMTimeNs"]:
                    row["HashError"] = "File changed after inventory; rescan before trusting it."
                else:
                    candidate_hash = content_hash(row["SourceFile"])
                    after = Path(row["SourceFile"]).stat()
                    if after.st_size != stat.st_size or after.st_mtime_ns != stat.st_mtime_ns:
                        row["HashError"] = "File changed during hashing; rescan before trusting it."
                    else:
                        row["SHA256"] = candidate_hash
            except OSError as error:
                row["HashError"] = str(error)
        if (index + 1) % args.chunk_size == 0:
            write_json(metadata_path, rows)
            write_json(state_path, {"schema_version": SCHEMA_VERSION, "provenance": provenance, "phase": "hashing", "hash_checkpoint": index + 1, "total": len(rows)})
    write_json(metadata_path, rows)
    write_json(state_path, {"schema_version": SCHEMA_VERSION, "provenance": provenance, "phase": "complete", "total": len(rows), "metadata_read_errors": sum(not row.get("MetadataReadComplete", False) for row in rows), "hash_mode": args.hash_mode, "hashed_files": sum(bool(row.get("SHA256")) for row in rows), "hash_scope_note": "candidates hashes only equal-size files within this scan; use full when importing against a previous batch."})
    print(f"Saved {len(rows)} source records to {metadata_path}")
    return rows


class DisjointSet:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left, right):
        left, right = self.find(left), self.find(right)
        if left == right:
            return False
        self.parent[max(left, right)] = min(left, right)
        return True


def distribution(counter, denominator=None):
    total = sum(counter.values()) if denominator is None else denominator
    return [{"value": key, "count": value, "percent": round(100 * value / total, 2) if total else 0.0}
            for key, value in sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))]


def load_rows(path):
    document = read_json(path)
    if isinstance(document, dict):
        if "records" not in document and "metadata" not in document:
            raise ValueError(f"Expected source metadata, not a summary or another JSON object: {path}")
        document = document.get("records", document.get("metadata"))
    if not isinstance(document, list):
        raise ValueError(f"Expected a JSON array of source records: {path}")
    result = []
    for row in document:
        item = dict(row)
        if not item.get("SourceFile"):
            raise ValueError("Every source row needs SourceFile.")
        item.setdefault("SourceBatch", "import_" + digest(str(Path(path).resolve()))[:12])
        item.setdefault("SourceRecordID", digest(item["SourceBatch"], path_key(item["SourceFile"]), item.get("FileBytes"), item.get("FileMTimeNs")))
        result.append(item)
    return result


def deduplicate(rows, baseline_records=(), baseline_decisions=None, max_candidates=20000):
    groups = DisjointSet(len(rows))
    hashes, local = {}, defaultdict(list)
    hash_merges = paired_merges = 0
    for index, row in enumerate(rows):
        sha = str(row.get("SHA256", "")).lower()
        if re.fullmatch(r"[0-9a-f]{64}", sha):
            if sha in hashes:
                hash_merges += int(groups.union(index, hashes[sha]))
            else:
                hashes[sha] = index
        key = settings_key(row)
        path = photo_path(row["SourceFile"])
        if key:
            local[(row["SourceBatch"], path_key(path.parent), path.stem.casefold(), *key)].append(index)
    pairing_ambiguous = 0
    for indexes in local.values():
        # Multiple RAWs or JPEGs can be bursts without subsecond metadata.
        # Pair only an unambiguous one-RAW/one-JPEG group.
        raw = [index for index in indexes if photo_path(rows[index]["SourceFile"]).suffix.lower() in RAW]
        jpeg = [index for index in indexes if photo_path(rows[index]["SourceFile"]).suffix.lower() in JPEG]
        if len(indexes) == 2 and len(raw) == len(jpeg) == 1 and compatible(rows[raw[0]], rows[jpeg[0]]):
            paired_merges += int(groups.union(raw[0], jpeg[0]))
        elif raw and jpeg:
            pairing_ambiguous += 1
    source_to_old = defaultdict(set)
    for capture in baseline_records:
        for source_id in capture.get("SourceRecordIDs", []):
            source_to_old[source_id].add(capture["CaptureID"])
    members = defaultdict(list)
    for index, row in enumerate(rows):
        members[groups.find(index)].append(row)
    captures = []
    for group in members.values():
        ids = sorted(row["SourceRecordID"] for row in group)
        old_ids = sorted({old for source in ids for old in source_to_old[source]})
        # Pick the richest row so an EXIF-poor exact copy doesn't hide data.
        representative = max(group, key=lambda row: sum(value is not None and value != "" for value in normalized(row).values()))
        capture = dict(representative)
        capture.update(CaptureID=old_ids[0] if old_ids else digest("capture", ids), SourceRecordIDs=ids,
                       SourceBatches=sorted({row["SourceBatch"] for row in group}), SourceFiles=[row["SourceFile"] for row in group],
                       BaselineCaptureIDs=old_ids, SourceFileCount=len(group))
        captures.append(capture)
    captures.sort(key=lambda row: row["CaptureID"])
    possible = defaultdict(list)
    for row in captures:
        key = settings_key(row, include_subseconds=False)
        if key:
            possible[key].append(row)
    previous = {}
    for item in (baseline_decisions or {}).get("decisions", []):
        previous[item["candidate_id"]] = item
    candidates, decisions, truncated = [], [], False
    for group in possible.values():
        for first, second in combinations(group, 2):
            a, b = normalized(first), normalized(second)
            if a["subseconds"] and b["subseconds"] and a["subseconds"] != b["subseconds"]:
                continue
            if not compatible(first, second):
                continue
            if len(candidates) >= max_candidates:
                truncated = True
                break
            capture_ids = sorted([first["CaptureID"], second["CaptureID"]])
            candidate_id = digest("candidate", capture_ids)
            paths = [photo_path(first["SourceFile"]), photo_path(second["SourceFile"])]
            renamed = paths[0].stem.casefold() != paths[1].stem.casefold()
            resized = any(first.get(tag) and second.get(tag) and first[tag] != second[tag] for tag in ("ImageWidth", "ImageHeight"))
            item = {"candidate_id": candidate_id, "capture_ids": capture_ids,
                    "source_record_ids": sorted(first["SourceRecordIDs"] + second["SourceRecordIDs"]),
                    "reason": "renamed_or_resized_matching_parameters" if renamed or resized else "matching_metadata_without_content_proof",
                    "renamed": renamed, "resized": resized, "missing_subseconds": not (a["subseconds"] and b["subseconds"]),
                    "action_taken": "kept_separately"}
            candidates.append(item)
            decision = dict(previous.get(candidate_id) or {"candidate_id": candidate_id, "capture_ids": capture_ids,
                                                          "decision": "keep_separate", "source": "conservative_default"})
            if candidate_id in previous:
                decision["inherited"] = True
            decisions.append(decision)
        if truncated:
            break
    current = {item["candidate_id"] for item in decisions}
    decisions.extend({**item, "inherited": True, "currently_candidate": False} for key, item in previous.items() if key not in current)
    return captures, candidates, {"schema_version": SCHEMA_VERSION, "default": "keep_separate", "decisions": decisions}, {
        "full_sha256_capture_merges": hash_merges, "raw_jpeg_capture_merges": paired_merges,
        "ambiguous_raw_jpeg_groups_preserved": pairing_ambiguous, "suspected_duplicate_pairs": len(candidates),
        "candidate_list_truncated": truncated, "candidate_limit": max_candidates}


def shutter_label(seconds):
    return f"1/{round(1 / seconds):,} s" if seconds < 1 else f"{seconds:g} s"


def summarize(captures, exclusions=(), source_count=None):
    counters = {key: Counter() for key in ("focal_length", "aperture", "iso", "camera", "lens", "shutter")}
    dates, warnings, warnings_zh = [], [], []
    invalid_time = excluded_time = 0
    valid = []
    sentinel = Counter()
    for row in captures:
        item = normalized(row)
        if not item["camera"] and not any(item[key] is not None for key in ("focal_length", "aperture", "iso", "shutter")):
            continue
        valid.append(row)
        for key, counter in counters.items():
            if item[key] is not None and item[key] != "":
                counter[item[key]] += 1
        if item["iso_note"]:
            sentinel[item["iso_note"]] += 1
        date = item["datetime"]
        if date is None:
            invalid_time += 1
        elif date.date().isoformat() in exclusions:
            excluded_time += 1
        else:
            dates.append(date)
    if invalid_time:
        warnings.append(f"{invalid_time} usable captures have no parsable DateTimeOriginal; excluded from time charts only.")
        warnings_zh.append(f"{invalid_time} 条有效记录缺少可解析拍摄时间，仅从时间统计中排除。")
    if excluded_time:
        warnings.append(f"{excluded_time} captures excluded from time charts by explicit date exclusions; exposure statistics retain them.")
        warnings_zh.append(f"按已有日期排除决定，{excluded_time} 条记录仅从时间统计中排除；曝光参数仍保留。")
    suspicious = sorted({date.date().isoformat() for date in dates if date.year < 1990 or date > datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
                         or (date.month == date.day == 1 and date.hour == date.minute == date.second == 0)})
    if suspicious:
        warnings.append("Review camera-clock dates (not automatically excluded): " + ", ".join(suspicious[:20]))
        warnings_zh.append("请核对可能异常的相机日期（未自动剔除）：" + "、".join(suspicious[:20]))
    if any(row.get("Synthetic") for row in captures):
        warnings.append("SYNTHETIC DEMO: contains fictional records; not valid as pure personal photography statistics.")
        warnings_zh.append("虚构演示数据，不能当作真实个人摄影统计。")
    if sentinel:
        warnings.append("ISO sentinel audit: " + ", ".join(f"{key}={value}" for key, value in sorted(sentinel.items())))
        warnings_zh.append(f"ISO 占位值核查：可靠恢复 {sentinel['recovered_from_extended_iso']} 条；其余疑点按缺失值处理。")
    warnings.append("Statistics describe supplied files only; zero months do not prove that no photos were taken. Camera-local times are not timezone-normalized.")
    warnings_zh.append("统计仅覆盖已提供素材；月份为零不代表没有拍摄；时间按相机当地记录，未统一时区。")
    if source_count and not all(row.get("SHA256") for row in captures):
        warnings.append("Full-library content identity was not proven for every file; metadata-only duplicate candidates remain separate.")
        warnings_zh.append("未对每个文件完成内容一致性证明；仅元数据相同的疑似重复分别保留。")
    coverage = {key: sum(counter.values()) for key, counter in counters.items()}
    coverage["capture_time"] = len(dates)
    hours = Counter(date.hour for date in dates)
    dayparts = Counter()
    for date in dates:
        label = "00–05" if date.hour < 6 else f"{date.hour // 3 * 3:02d}–{date.hour // 3 * 3 + 2:02d}"
        dayparts[label] += 1
    iso_bins = [("≤100", lambda value: value <= 100), ("101–400", lambda value: 100 < value <= 400),
                ("401–800", lambda value: 400 < value <= 800), ("801–1600", lambda value: 800 < value <= 1600),
                ("1601–3200", lambda value: 1600 < value <= 3200), ("3201–6400", lambda value: 3200 < value <= 6400),
                ("6401–12800", lambda value: 6400 < value <= 12800), (">12800", lambda value: value > 12800)]
    shutter_bins = [("1/1000 s and faster", lambda value: value <= .001),
                    ("slower than 1/1000 through 1/250 s", lambda value: .001 < value <= .004),
                    ("slower than 1/250 through 1/60 s", lambda value: .004 < value <= number(1/60)),
                    ("slower than 1/60, under 1 s", lambda value: number(1/60) < value < 1),
                    ("1 s and longer", lambda value: value >= 1)]
    def bins(counter, definitions):
        total = sum(counter.values())
        return [{"value": label, "label": label, "count": sum(count for value, count in counter.items() if predicate(value)),
                 "percent": round(100 * sum(count for value, count in counter.items() if predicate(value)) / total, 2) if total else 0.0}
                for label, predicate in definitions]
    annual_monthly = []
    annual_monthly_records = []
    for year in sorted({date.year for date in dates}):
        selected = [date for date in dates if date.year == year]
        months = [sum(date.month == month for date in selected) for month in range(1, 13)]
        annual_monthly.append({"year": year, "count": len(selected), "shooting_days": len({date.date() for date in selected}), "months": months})
        annual_monthly_records.extend({"year": year, "month": month, "count": months[month - 1],
                                       "shooting_days": len({date.date() for date in selected if date.month == month})} for month in range(1, 13))
    month_photos = Counter(date.month for date in dates)
    month_days = Counter(date.month for date in {date.date() for date in dates})
    result = {"schema_version": SCHEMA_VERSION, "dataset_version": "synthetic_demo" if any(row.get("Synthetic") for row in captures) else "local_analysis",
              "synthetic": any(row.get("Synthetic") for row in captures), "scanned_photo_files": source_count if source_count is not None else len(captures),
              "photo_records_after_deduplication": len(captures), "valid_capture_records": len(valid), "valid_camera_records": len(valid),
              "unusable_records": len(captures) - len(valid), "valid_record_definition": "A capture with a camera model or at least one positive focal length, f-number, resolved ISO, or shutter duration. Each chart uses its own nonmissing-field denominator.",
              "metric_coverage": coverage, "date_range": [min(dates).isoformat(), max(dates).isoformat()] if dates else [],
              "date_range_detail": {"start": min(dates).isoformat() if dates else None, "end": max(dates).isoformat() if dates else None},
              "shooting_days": len({date.date() for date in dates}), "coverage_notes": warnings, "coverage_notes_zh": warnings_zh,
              "time_exclusion_dates": sorted(exclusions), "iso_corrections": dict(sentinel),
              "hours_chronological": [{"value": hour, "count": hours[hour], "percent": round(100 * hours[hour] / len(dates), 2) if dates else 0.0} for hour in range(24)],
              "hours_ranked": distribution(hours), "dayparts": distribution(dayparts),
              "iso_bins": bins(counters["iso"], iso_bins), "shutter_bins": bins(counters["shutter"], shutter_bins),
              "annual_monthly": annual_monthly, "annual_monthly_records": annual_monthly_records,
              "months_by_photos": distribution(month_photos), "months_by_shooting_days": distribution(month_days)}
    for item, label in zip(result["shutter_bins"], ["1/1000 秒及更快", "慢于 1/1000 至 1/250 秒", "慢于 1/250 至 1/60 秒", "慢于 1/60 秒且不足 1 秒", "1 秒及更慢"]):
        item["display_name"] = label
    for key, counter in counters.items():
        output_key = "shutter_top" if key == "shutter" else key
        result[output_key] = distribution(counter)
        if key in ("camera", "lens"):
            for item in result[output_key]:
                item["display_name"] = item["value"]
        if key == "shutter":
            for item in result[output_key]:
                item["label"] = shutter_label(item["value"])
    return result


def markdown_report(summary, dedup):
    lines = ["# Photography data report", "", "**SYNTHETIC DEMO — fictional records.**" if summary["synthetic"] else "Statistics cover only the files supplied to this run.", "",
             f"- Source files: {summary['scanned_photo_files']:,}", f"- Capture groups: {summary['photo_records_after_deduplication']:,}",
             f"- Usable capture groups: {summary['valid_capture_records']:,}", f"- Shooting days with usable dates: {summary['shooting_days']:,}",
             f"- Exact SHA-256 merges: {dedup['full_sha256_capture_merges']:,}", f"- Unambiguous local RAW/JPEG pairs: {dedup['raw_jpeg_capture_merges']:,}",
             f"- Suspected duplicate pairs retained separately: {dedup['suspected_duplicate_pairs']:,}", "", summary["valid_record_definition"], "",
             "## Metric coverage", "", "| Metric | Captures in denominator |", "|---|---:|"]
    lines += [f"| {key} | {value:,} |" for key, value in summary["metric_coverage"].items()]
    for metric in ("focal_length", "aperture", "iso", "shutter_top", "camera", "lens"):
        lines += ["", "## " + metric.replace("_", " ").title(), "", "| Value | Captures | Share of available field |", "|---|---:|---:|"]
        lines += [f"| {str(item.get('label', item['value'])).replace('|', '/')} | {item['count']:,} | {item['percent']:.2f}% |" for item in summary[metric][:10]]
    lines += ["", "## Photos per year and month", "", "| Year | " + " | ".join(str(month) for month in range(1, 13)) + " | Total | Shooting days |",
              "|---|" + "---:|" * 14]
    lines += [f"| {item['year']} | " + " | ".join(str(value) for value in item["months"]) + f" | {item['count']} | {item['shooting_days']} |" for item in summary["annual_monthly"]]
    lines += ["", "## Coverage and decisions", ""] + ["- " + note for note in summary["coverage_notes"]]
    if dedup["candidate_list_truncated"]:
        lines += [f"- Candidate reporting stopped at {dedup['candidate_limit']:,} pairs; no candidate was automatically merged."]
    lines += ["", "Source photographs were not modified. Metadata, paths, serials, and hashes in analysis output are private working data; do not publish them with the reusable skill.", ""]
    return "\n".join(lines)


def analyze(args):
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows_by_id = {}
    baseline_records, baseline_decisions, baseline_exclusions = [], {}, set()
    inputs = list(args.input)
    if args.baseline:
        baseline = Path(args.baseline)
        if not (baseline / "metadata_all.json").exists():
            raise ValueError("Baseline must be a previous analyze output containing metadata_all.json.")
        inputs.insert(0, str(baseline / "metadata_all.json"))
        baseline_records = read_json(baseline / "records.json")
        if (baseline / "duplicate_decisions.json").exists():
            baseline_decisions = read_json(baseline / "duplicate_decisions.json")
        if (baseline / "summary.json").exists():
            baseline_exclusions = set(read_json(baseline / "summary.json").get("time_exclusion_dates", []))
    for source in inputs:
        for row in load_rows(source):
            source_id = row["SourceRecordID"]
            if source_id in rows_by_id:
                previous = rows_by_id[source_id]
                for key in set(row) & set(previous):
                    if row[key] is not None and previous[key] is not None and row[key] != previous[key]:
                        raise ValueError(f"Conflicting data for one SourceRecordID ({source_id[:12]}), field {key}. Use a new batch ID for a new physical-disk import.")
                previous.update({key: value for key, value in row.items() if value is not None})
            else:
                rows_by_id[source_id] = row
    rows = list(rows_by_id.values())
    captures, candidates, decisions, dedup = deduplicate(rows, baseline_records, baseline_decisions)
    exclusions = baseline_exclusions | set(args.exclude_date or [])
    for value in exclusions:
        datetime.strptime(value, "%Y-%m-%d")
    summary = summarize(captures, exclusions, len(rows))
    summary.update(dedup)
    summary["source_batches"] = sorted({row["SourceBatch"] for row in rows})
    for name, data in (("metadata_all.json", rows), ("records.json", captures), ("summary.json", summary),
                       ("duplicate_candidates.json", candidates), ("duplicate_decisions.json", decisions)):
        write_json(out / name, data)
    (out / "report.md").write_text(markdown_report(summary, dedup), encoding="utf-8")
    print(f"Saved {len(captures)} capture groups ({summary['valid_capture_records']} usable) to {out}")
    return summary


def demo(args):
    source = Path(__file__).resolve().parent.parent / "assets" / "demo_metadata.json"
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    data = read_json(source)
    write_json(out / "demo_metadata.json", data)
    return analyze(argparse.Namespace(input=[str(out / "demo_metadata.json")], output=str(out), baseline=None, exclude_date=[]))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="Read local photo metadata with ExifTool, checkpointing each chunk.")
    scan_parser.add_argument("--root", action="append", required=True)
    scan_parser.add_argument("--batch-id", required=True)
    scan_parser.add_argument("--output", required=True)
    scan_parser.add_argument("--exiftool", required=True, help="ExifTool executable or Perl script path.")
    scan_parser.add_argument("--perl", help="Perl executable when --exiftool points to the Perl script.")
    scan_parser.add_argument("--hash-mode", choices=("none", "candidates", "full"), default="candidates")
    scan_parser.add_argument("--chunk-size", type=int, default=200)
    scan_parser.set_defaults(handler=scan)
    analyze_parser = commands.add_parser("analyze", help="Merge scan JSON files and produce conservative capture statistics.")
    analyze_parser.add_argument("--input", action="append", required=True)
    analyze_parser.add_argument("--output", required=True)
    analyze_parser.add_argument("--baseline")
    analyze_parser.add_argument("--exclude-date", action="append", default=[])
    analyze_parser.set_defaults(handler=analyze)
    demo_parser = commands.add_parser("demo", help="Analyze the bundled fictional metadata without ExifTool.")
    demo_parser.add_argument("--output", required=True)
    demo_parser.set_defaults(handler=demo)
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if getattr(arguments, "chunk_size", 1) < 1:
        raise SystemExit("--chunk-size must be positive")
    try:
        arguments.handler(arguments)
    except (OSError, ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
