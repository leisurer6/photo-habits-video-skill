"""Run with: python -m unittest discover -s scripts -p 'test_photo_stats.py'."""
import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_stats as stats


def row(name="IMG_0001.JPG", batch="disk-a", **changes):
    result = {"SourceFile": "/fictional/photos/" + name, "SourceBatch": batch,
              "Make": "Example Camera Co", "Model": "Example A", "LensModel": "Example 35mm",
              "DateTimeOriginal": "2023:07:16 16:12:34", "SubSecTimeOriginal": "012",
              "FocalLength": 35, "FNumber": 2.8, "ISO": 100, "ExposureTime": .004,
              "ImageWidth": 6000, "ImageHeight": 4000}
    result.update(changes)
    result["SourceRecordID"] = stats.digest(result["SourceBatch"], result["SourceFile"], changes.get("FileBytes"), changes.get("FileMTimeNs"))
    return result


class DeduplicationTests(unittest.TestCase):
    def dedup(self, values, **options):
        return stats.deduplicate(values, **options)

    def test_same_name_different_date_or_parameters_is_retained(self):
        source = row()
        for changed in ({"DateTimeOriginal": "2023:07:17 16:12:34"}, {"ISO": 400}, {"FNumber": 4}, {"FocalLength": 56}, {"ExposureTime": .01}, {"Model": "Example B"}):
            with self.subTest(changed=changed):
                other = row(batch="disk-b", **changed)
                captures, candidates, _, _ = self.dedup([source, other])
                self.assertEqual(len(captures), 2)
                self.assertEqual(candidates, [])

    def test_local_raw_jpeg_pair_is_one_capture(self):
        captures, _, _, evidence = self.dedup([row("IMG_0001.ARW"), row("IMG_0001.JPG")])
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0]["SourceFileCount"], 2)
        self.assertEqual(evidence["raw_jpeg_capture_merges"], 1)

    def test_distinct_subseconds_and_missing_core_do_not_pair(self):
        for changes in ({"SubSecTimeOriginal": "013"}, {"ISO": None}, {"ExposureTime": None}, {"FNumber": 0}, {"FocalLength": None}, {"Model": ""}):
            with self.subTest(changes=changes):
                captures, _, _, _ = self.dedup([row("IMG_0001.ARW"), row("IMG_0001.JPG", **changes)])
                self.assertEqual(len(captures), 2)

    def test_actual_zero_subsecond_is_distinct_from_missing(self):
        self.assertNotEqual(stats.subseconds({"SubSecTimeOriginal": "000"}), stats.subseconds({}))

    def test_optional_identity_conflicts_prevent_raw_jpeg_pair(self):
        for tag in ("Make", "LensModel", "SerialNumber", "BodySerialNumber", "OffsetTimeOriginal"):
            with self.subTest(tag=tag):
                first = row("IMG_0001.ARW", **{tag: "one"})
                second = row("IMG_0001.JPG", **{tag: "two"})
                self.assertEqual(len(self.dedup([first, second])[0]), 2)

    def test_ambiguous_raw_burst_is_not_paired(self):
        # Same-stem files with case-sensitive paths on a Unix source volume.
        values = [row("IMG_0001.ARW"), row("img_0001.arw"), row("IMG_0001.JPG")]
        captures, _, _, evidence = self.dedup(values)
        self.assertEqual(len(captures), 3)
        self.assertEqual(evidence["ambiguous_raw_jpeg_groups_preserved"], 1)

    def test_same_full_sha256_merges_across_names_and_batches(self):
        values = [row(SHA256="a" * 64), row("renamed.JPG", batch="disk-b", SHA256="a" * 64)]
        captures, _, _, evidence = self.dedup(values)
        self.assertEqual(len(captures), 1)
        self.assertEqual(evidence["full_sha256_capture_merges"], 1)
        self.assertEqual(len(captures[0]["SourceRecordIDs"]), 2)

    def test_identical_metadata_across_folders_stays_separate(self):
        values = [row(), row("../other/renamed.JPG", ImageWidth=1600, ImageHeight=1067)]
        captures, candidates, decisions, _ = self.dedup(values)
        self.assertEqual(len(captures), 2)
        self.assertTrue(candidates[0]["renamed"])
        self.assertTrue(candidates[0]["resized"])
        self.assertEqual(decisions["decisions"][0]["decision"], "keep_separate")

    def test_nine_keep_decisions_and_capture_ids_inherit(self):
        values = []
        for index in range(9):
            changes = {"DateTimeOriginal": f"2023:07:{index+1:02d} 16:12:34"}
            values += [row(f"day{index}/A.JPG", **changes), row(f"copy{index}/B.JPG", ImageWidth=1600, **changes)]
        captures, candidates, decisions, _ = self.dedup(values)
        self.assertEqual(len(candidates), 9)
        for item in decisions["decisions"]:
            item.update(source="user", note="Unsure; keep both.")
        current, _, inherited, _ = self.dedup(values, baseline_records=captures, baseline_decisions=decisions)
        self.assertEqual([item["CaptureID"] for item in current], [item["CaptureID"] for item in captures])
        self.assertEqual(len(inherited["decisions"]), 9)
        self.assertTrue(all(item["inherited"] and item["source"] == "user" and item["decision"] == "keep_separate" for item in inherited["decisions"]))

    def test_new_disk_same_drive_letter_and_path_keeps_batch_identity(self):
        first = row(SourceFile=r"F:\photos\IMG_0001.JPG", batch="old-disk")
        second = row(SourceFile=r"F:\photos\IMG_0001.JPG", batch="new-disk", DateTimeOriginal="2024:08:01 16:12:34")
        self.assertNotEqual(first["SourceRecordID"], second["SourceRecordID"])
        captures, _, _, _ = self.dedup([first, second])
        self.assertEqual(len(captures), 2)


class StatisticsTests(unittest.TestCase):
    def test_summary_input_is_rejected_instead_of_silent_empty_analysis(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.json"
            stats.write_json(path, {"valid_capture_records": 100})
            with self.assertRaisesRegex(ValueError, "Expected source metadata"):
                stats.load_rows(path)

    def test_iso_sentinel_recovery_requires_unambiguous_evidence(self):
        self.assertEqual(stats.iso_value({"ISO": 65535, "RecommendedExposureIndex": 102400})[0], 102400)
        self.assertIsNone(stats.iso_value({"ISO": 65535})[0])
        self.assertIsNone(stats.iso_value({"ISO": 65535, "RecommendedExposureIndex": 102400, "ISOSpeed": 51200})[0])
        self.assertIsNone(stats.iso_value({"ISO": 65535, "SensitivityType": 1, "RecommendedExposureIndex": 102400})[0])
        self.assertEqual(stats.iso_value({"ISO": 65535, "SensitivityType": 1, "StandardOutputSensitivity": 102400, "RecommendedExposureIndex": 51200})[0], 102400)

    def test_shutter_bin_boundaries_include_exact_one_sixtieth(self):
        values = [row("A.JPG", ExposureTime=1/60), row("B.JPG", ExposureTime=1/250), row("C.JPG", ExposureTime=1/1000)]
        summary = stats.summarize(values)
        self.assertEqual([item["count"] for item in summary["shutter_bins"]], [1, 1, 1, 0, 0])

    def test_independent_denominators_month_day_and_bins_conserve_totals(self):
        values = [row("A.JPG"), row("B.JPG", ISO=None, FNumber=None, DateTimeOriginal="2023:07:16 18:00:00"),
                  row("C.JPG", FocalLength=None, DateTimeOriginal="2024:08:01 12:00:00"),
                  row("D.JPG", DateTimeOriginal="bad date", ISO=65535), row("E.JPG", DateTimeOriginal="2000:01:01 00:00:00")]
        summary = stats.summarize(values)
        self.assertEqual(summary["valid_capture_records"], 5)
        self.assertEqual(summary["metric_coverage"]["iso"], 3)
        self.assertEqual(summary["metric_coverage"]["aperture"], 4)
        self.assertEqual(summary["metric_coverage"]["focal_length"], 4)
        self.assertEqual(sum(item["count"] for item in summary["iso_bins"]), 3)
        self.assertEqual(sum(item["count"] for item in summary["shutter_bins"]), 5)
        self.assertEqual(sum(sum(item["months"]) for item in summary["annual_monthly"]), 4)
        self.assertEqual(sum(item["count"] for item in summary["months_by_photos"]), 4)
        self.assertEqual(sum(item["count"] for item in summary["months_by_shooting_days"]), 3)
        self.assertEqual(sum(item["count"] for item in summary["hours_chronological"]), 4)
        # No project-specific camera-clock date is silently discarded.
        excluded = stats.summarize(values, {"2000-01-01"})
        self.assertEqual(excluded["metric_coverage"]["capture_time"], 3)
        self.assertEqual(excluded["metric_coverage"]["iso"], 3)

    def test_analyze_baseline_adds_new_rows_without_double_counting(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "first.json"
            stats.write_json(source, [row()])
            stats.analyze(argparse.Namespace(input=[str(source)], output=str(folder / "baseline"), baseline=None, exclude_date=[]))
            second = folder / "second.json"
            stats.write_json(second, [row(batch="disk-b", DateTimeOriginal="2024:08:01 16:12:34")])
            summary = stats.analyze(argparse.Namespace(input=[str(second)], output=str(folder / "merged"), baseline=str(folder / "baseline"), exclude_date=[]))
            self.assertEqual(summary["valid_capture_records"], 2)
            records = stats.read_json(folder / "merged" / "records.json")
            old_id = stats.read_json(folder / "baseline" / "records.json")[0]["CaptureID"]
            self.assertIn(old_id, [item["CaptureID"] for item in records])

    def test_baseline_time_exclusions_are_inherited(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "source.json"
            stats.write_json(source, [row(), row("reset.JPG", DateTimeOriginal="2000:01:01 00:00:00")])
            stats.analyze(argparse.Namespace(input=[str(source)], output=str(folder / "baseline"), baseline=None, exclude_date=["2000-01-01"]))
            summary = stats.analyze(argparse.Namespace(input=[str(source)], output=str(folder / "merged"), baseline=str(folder / "baseline"), exclude_date=[]))
            self.assertEqual(summary["metric_coverage"]["capture_time"], 1)
            self.assertEqual(summary["time_exclusion_dates"], ["2000-01-01"])

    def test_empty_dataset_and_no_time_are_supported(self):
        self.assertEqual(stats.summarize([])["date_range"], [])
        summary = stats.summarize([row(DateTimeOriginal=None)])
        self.assertEqual(summary["valid_capture_records"], 1)
        self.assertEqual(summary["metric_coverage"]["capture_time"], 0)


class ScanTests(unittest.TestCase):
    def test_checkpoint_resume_and_hashes_leave_source_files_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = folder / "photos"
            source.mkdir()
            paths = [source / f"DEMO_{index}.JPG" for index in range(5)]
            for path in paths:
                path.write_bytes(b"fictional image content")
            before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
            output = folder / "scan"
            args = argparse.Namespace(root=[str(source)], batch_id="test-disk", output=str(output), exiftool="unused", perl=None, hash_mode="full", chunk_size=2)
            calls = []
            def fake_tool(command, **unused):
                calls.append(command)
                filenames = Path(command[-1]).read_text(encoding="utf-8").splitlines()
                result = [{"SourceFile": path, "Model": "Demo camera", "ISO": 100} for path in filenames]
                return argparse.Namespace(stdout=json.dumps(result), stderr="", returncode=0)
            counter = 0
            def interrupted_tool(command, **kwargs):
                nonlocal counter
                counter += 1
                if counter == 2:
                    raise KeyboardInterrupt()
                return fake_tool(command, **kwargs)
            with patch.object(stats.subprocess, "run", side_effect=interrupted_tool):
                with self.assertRaises(KeyboardInterrupt):
                    stats.scan(args)
            checkpoint = stats.read_json(output / "scan_state.json")
            self.assertEqual(checkpoint["completed"], 2)
            calls.clear()
            with patch.object(stats.subprocess, "run", side_effect=fake_tool):
                metadata = stats.scan(args)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(metadata), 5)
            self.assertEqual(len({item["SHA256"] for item in metadata}), 1)
            self.assertEqual(stats.read_json(output / "scan_state.json")["phase"], "complete")
            self.assertEqual(before, {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})


if __name__ == "__main__":
    unittest.main()
