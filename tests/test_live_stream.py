"""Unit tests for live stream rolling-window clipping, transcript/score gates,
metadata merging, and Cut.Pro live CLI subcommands."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from clip_selection import (
    clip_count_targets,
    dedupe_against_stream_spans,
    filter_by_min_score,
    max_window_score,
    shift_transcript,
)
from cloud.scopes import allows
from live_stream import (
    merge_chunk_into_session_metadata,
    normalize_live_candidate_url,
    probe_live_stream,
)


class TestLiveClipSelectionHelpers(unittest.TestCase):
    def test_clip_count_targets_live_mode_allows_zero_min(self):
        with mock.patch.dict(os.environ, {"LIVE_MODE": "1", "CLIP_TARGET_MAX": "2"}, clear=False):
            low, high = clip_count_targets(4)
            self.assertEqual(low, 0)
            self.assertEqual(high, 2)

    def test_max_window_score(self):
        self.assertEqual(max_window_score([]), 0)
        self.assertEqual(max_window_score([{"score": 42}, {"score": 78}, {"score": 61}]), 78)

    def test_filter_by_min_score(self):
        shorts = [
            {"start": 10, "end": 40, "predicted_score": 68},
            {"start": 50, "end": 90, "predicted_score": 75},
            {"start": 100, "end": 140, "predicted_score": 88},
        ]
        kept = filter_by_min_score(shorts, 75)
        self.assertEqual(len(kept), 2)
        self.assertEqual([c["predicted_score"] for c in kept], [75, 88])

    def test_shift_transcript_offsets_and_filters_overlap(self):
        transcript = {
            "language": "pt",
            "segments": [
                {
                    "start": 5.0,
                    "end": 25.0,
                    "text": "overlap part",
                    "words": [{"word": "overlap", "start": 10.0, "end": 11.0}],
                },
                {
                    "start": 32.0,
                    "end": 45.0,
                    "text": "new part",
                    "words": [{"word": "new", "start": 32.5, "end": 33.2}],
                },
            ],
        }
        shifted = shift_transcript(transcript, offset_seconds=600.0, min_start=30.0)
        self.assertEqual(shifted["language"], "pt")
        self.assertEqual(len(shifted["segments"]), 1)
        self.assertAlmostEqual(shifted["segments"][0]["start"], 632.0)
        self.assertAlmostEqual(shifted["segments"][0]["end"], 645.0)
        self.assertEqual(len(shifted["segments"][0]["words"]), 1)
        self.assertAlmostEqual(shifted["segments"][0]["words"][0]["start"], 632.5)
        self.assertAlmostEqual(shifted["segments"][0]["words"][0]["end"], 633.2)

    def test_dedupe_against_stream_spans_drops_overlap_duplicates(self):
        existing_spans = [(560.0, 595.0)]
        candidates = [
            {"start": 0.0, "end": 28.0, "predicted_score": 82},
            {"start": 120.0, "end": 160.0, "predicted_score": 85},
        ]
        kept = dedupe_against_stream_spans(
            candidates,
            existing_stream_spans=existing_spans,
            chunk_stream_offset=570.0,
            ratio=0.25,
        )
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0]["start"], 120.0)


class TestLiveStreamUrlAndMerge(unittest.TestCase):
    def test_normalize_live_candidate_url(self):
        self.assertEqual(
            normalize_live_candidate_url("https://www.youtube.com/@openshorts"),
            "https://www.youtube.com/@openshorts/live",
        )
        self.assertEqual(
            normalize_live_candidate_url("https://www.youtube.com/@openshorts/streams"),
            "https://www.youtube.com/@openshorts/live",
        )
        self.assertEqual(
            normalize_live_candidate_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(
            normalize_live_candidate_url("https://www.twitch.tv/gaules"),
            "https://www.twitch.tv/gaules",
        )

    def test_probe_live_stream_parses_yt_dlp_json(self):
        fake_info = {
            "id": "abc123xyz00",
            "is_live": True,
            "live_status": "is_live",
            "title": "LIVE TESTE",
            "uploader": "Canal Teste",
            "webpage_url": "https://www.youtube.com/watch?v=abc123xyz00",
            "url": "https://manifest.googlevideo.com/api/manifest/hls_variant/test.m3u8",
        }
        fake_yt_dlp = types.ModuleType("yt_dlp")

        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return fake_info

        fake_yt_dlp.YoutubeDL = FakeYDL
        with (
            mock.patch.dict(sys.modules, {"yt_dlp": fake_yt_dlp}),
            mock.patch("security_utils.assert_public_url", return_value="https://www.youtube.com/@canal/live"),
            mock.patch("ssrf_guard.assert_public_url", return_value=fake_info["url"]),
            mock.patch("ssrf_guard.guarded"),
        ):
            res = probe_live_stream("https://www.youtube.com/@canal")
            self.assertTrue(res["is_live"])
            self.assertEqual(res["live_status"], "is_live")
            self.assertEqual(res["webpage_url"], "https://www.youtube.com/watch?v=abc123xyz00")
            self.assertTrue(res["manifest_available"])

    def test_merge_chunk_into_session_metadata_preserves_subtitled_and_shifts_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            session_dir = tmp_path / "session"
            session_dir.mkdir()
            spans: list[tuple[float, float]] = []

            # Chunk 0
            c0_dir = tmp_path / "c0"
            c0_dir.mkdir()
            mp4_0 = c0_dir / "hooked_100_chunk_000_clip_1.mp4"
            mp4_0.write_bytes(b"fake_mp4_0")
            chunk_meta_0 = {
                "source_video": "chunk_000.mp4",
                "output_format": "vertical",
                "transcript": {
                    "language": "pt",
                    "segments": [
                        {
                            "start": 10.0,
                            "end": 40.0,
                            "text": "hello",
                            "words": [{"word": "hello", "start": 10.5, "end": 11.0}],
                        }
                    ],
                },
                "shorts": [
                    {
                        "start": 10.0,
                        "end": 40.0,
                        "predicted_score": 84,
                        "video_title_for_youtube_short": "Clip Zero",
                    }
                ],
                "cost_analysis": {"input_tokens": 100, "output_tokens": 20, "total_cost": 0.001},
            }

            res_0 = merge_chunk_into_session_metadata(
                session_output_dir=str(session_dir),
                session_base_name="live_session",
                job_id="job123",
                chunk_work_dir=str(c0_dir),
                chunk_base_name="chunk_000",
                chunk_metadata=chunk_meta_0,
                chunk_index=0,
                chunk_stream_offset=0.0,
                transcript_offset=0.0,
                existing_stream_spans=spans,
                max_total_clips=5,
            )
            self.assertEqual(len(res_0["added_clips"]), 1)
            new_offset_0 = res_0["new_transcript_offset"]
            self.assertGreater(new_offset_0, 0.0)

            # Simulate concurrent POST /api/subtitle updating clip 0's video_url
            session_meta_path = session_dir / "live_session_metadata.json"
            session_data = json.loads(session_meta_path.read_text(encoding="utf-8"))
            session_data["shorts"][0]["video_url"] = "/videos/job123/subtitled_999_live_session_clip_1.mp4"
            session_meta_path.write_text(json.dumps(session_data), encoding="utf-8")

            # Chunk 1
            c1_dir = tmp_path / "c1"
            c1_dir.mkdir()
            mp4_1 = c1_dir / "hooked_200_chunk_001_clip_1.mp4"
            mp4_1.write_bytes(b"fake_mp4_1")
            chunk_meta_1 = {
                "source_video": "chunk_001.mp4",
                "output_format": "vertical",
                "transcript": {
                    "language": "pt",
                    "segments": [
                        {
                            "start": 50.0,
                            "end": 85.0,
                            "text": "second clip",
                            "words": [{"word": "second", "start": 50.5, "end": 51.0}],
                        }
                    ],
                },
                "shorts": [
                    {
                        "start": 50.0,
                        "end": 85.0,
                        "predicted_score": 91,
                        "video_title_for_youtube_short": "Clip One",
                    }
                ],
                "cost_analysis": {"input_tokens": 150, "output_tokens": 30, "total_cost": 0.002},
            }

            res_1 = merge_chunk_into_session_metadata(
                session_output_dir=str(session_dir),
                session_base_name="live_session",
                job_id="job123",
                chunk_work_dir=str(c1_dir),
                chunk_base_name="chunk_001",
                chunk_metadata=chunk_meta_1,
                chunk_index=1,
                chunk_stream_offset=570.0,
                transcript_offset=new_offset_0,
                existing_stream_spans=spans,
                max_total_clips=5,
            )
            self.assertEqual(len(res_1["added_clips"]), 1)

            merged = json.loads(session_meta_path.read_text(encoding="utf-8"))
            self.assertEqual(len(merged["shorts"]), 2)
            # Clip 0 kept its subtitled URL!
            self.assertEqual(
                merged["shorts"][0]["video_url"],
                "/videos/job123/subtitled_999_live_session_clip_1.mp4",
            )
            # Clip 1 has shifted timestamps so /api/subtitle can locate its words in merged["transcript"]
            self.assertAlmostEqual(merged["shorts"][1]["start"], new_offset_0 + 50.0)
            self.assertAlmostEqual(merged["shorts"][1]["end"], new_offset_0 + 85.0)
            self.assertAlmostEqual(merged["shorts"][1]["stream_start"], 570.0 + 50.0)
            self.assertEqual(merged["cost_analysis"]["input_tokens"], 250)


class TestCloudScopesAndCutProSyncCLI(unittest.TestCase):
    def test_cloud_scopes_allow_live_endpoints(self):
        self.assertTrue(allows("openshorts", "GET", "/api/live/probe"))
        self.assertTrue(allows("openshorts", "POST", "/api/live/probe"))
        self.assertTrue(allows("openshorts", "POST", "/api/live/start"))
        self.assertTrue(allows("openshorts", "GET", "/api/live"))
        self.assertTrue(allows("openshorts", "GET", "/api/live/123e4567-e89b-12d3-a456-426614174000"))
        self.assertTrue(allows("openshorts", "POST", "/api/live/123e4567-e89b-12d3-a456-426614174000/stop"))

    def test_cutpro_sync_cli_live_subcommands(self):
        script_path = (
            Path(__file__).resolve().parent.parent
            / "skills"
            / "openshorts-cutpro"
            / "scripts"
            / "openshorts_cutpro_sync.py"
        )
        spec = importlib.util.spec_from_file_location("openshorts_cutpro_sync", script_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)

        parser = mod.build_parser()
        args = parser.parse_args(
            [
                "live",
                "--url",
                "https://www.youtube.com/@openshorts/live",
                "--chunk-minutes",
                "10",
                "--min-score",
                "78",
                "--max-clips-per-chunk",
                "2",
                "--immediate",
            ]
        )
        self.assertEqual(args.command, "live")
        self.assertEqual(args.chunk_minutes, 10.0)
        self.assertEqual(args.min_score, 78)
        self.assertEqual(args.max_clips_per_chunk, 2)
        self.assertTrue(args.immediate)


if __name__ == "__main__":
    unittest.main()
