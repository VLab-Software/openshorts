"""Real-time live stream monitoring and rolling-window clipping for OpenShorts.

Architecture:
1. ``probe_live_stream(url)`` checks whether a URL (or channel ``/live`` link)
   is currently broadcasting live and resolves its HLS/stream URL.
2. ``LiveSession`` continuously captures the live stream in rolling segments
   (``chunk_minutes``, default 10 min) with a seamless boundary overlap
   (``overlap_seconds``, default 30 s) so sentences across chunk boundaries are
   never cut in half.
3. Each closed chunk runs through ``main.py`` with ``LIVE_MODE=1``:
   - Audio/Speech Density Gate: chunks with sparse speech (AFK, "Starting soon",
     music breaks) are discarded before Gemini or GPU rendering.
   - AI Transcript Score Gate: Pass 1 aborts early if no window reaches
     ``min_window_score``, and Pass 2 keeps only clips with
     ``predicted_score >= min_score`` (no forced ``min_clips`` floor).
4. Approved clips are merged incrementally into the parent job's directory
   (``outputs/{job_id}/``) and ``*_metadata.json`` with unified transcript
   offsets so ``GET /api/status/{job_id}``, ``POST /api/subtitle`` (e.g.
   ``preset="hormozi"``) and ``/videos/{job_id}/...`` work in real time while
   the live stream is still on the air.
5. Every raw chunk file (``.ts`` / ``.mp4``) and temporary work folder is
   deleted immediately in a ``finally`` block, preventing multi-gigabyte live
   recordings from accumulating on disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from clip_selection import (
    dedupe_against_stream_spans,
    shift_transcript,
)
from jsonfile import write_json_atomic


LIVE_METADATA_BASENAME = "live_session"
MIN_PARTIAL_CHUNK_SECONDS = 45.0


@dataclass
class LiveConfig:
    url: str
    chunk_minutes: float = 10.0
    overlap_seconds: float = 30.0
    min_score: float = 75.0
    min_window_score: Optional[float] = None
    min_words_per_min: float = 20.0
    max_clips_per_chunk: int = 2
    max_total_clips: int = 30
    max_duration_minutes: int = 360
    captions: bool = True
    auto_hook: bool = True
    auto_hook_style: str = "pill"
    layouts: List[str] = field(default_factory=lambda: ["auto"])
    output_format: str = "vertical"
    clip_min_seconds: float = 30.0
    clip_max_seconds: float = 60.0
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None

    def normalized(self) -> "LiveConfig":
        chunk_min = min(max(float(self.chunk_minutes or 10.0), 1.0), 60.0)
        overlap = min(max(float(self.overlap_seconds if self.overlap_seconds is not None else 30.0), 0.0), 120.0)
        if overlap >= chunk_min * 60.0 * 0.5:
            overlap = round(chunk_min * 60.0 * 0.25, 1)
        min_sc = min(max(float(self.min_score if self.min_score is not None else 75.0), 0.0), 100.0)
        if self.min_window_score is not None:
            min_win = min(max(float(self.min_window_score), 0.0), 100.0)
        else:
            min_win = max(50.0, min_sc - 10.0) if min_sc > 0 else 0.0
        min_wpm = min(max(float(self.min_words_per_min if self.min_words_per_min is not None else 20.0), 0.0), 200.0)
        max_per_chunk = min(max(int(self.max_clips_per_chunk or 2), 1), 10)
        max_total = min(max(int(self.max_total_clips or 30), 1), 200)
        max_dur = min(max(int(self.max_duration_minutes or 360), 5), 1440)
        c_min = min(max(float(self.clip_min_seconds or 30.0), 5.0), 175.0)
        c_max = min(max(float(self.clip_max_seconds or 60.0), 10.0), 180.0)
        if c_max < c_min + 5.0:
            c_max = min(180.0, c_min + 5.0)
        fmt = self.output_format if self.output_format in ("vertical", "horizontal", "square") else "vertical"
        return LiveConfig(
            url=str(self.url or "").strip(),
            chunk_minutes=round(chunk_min, 2),
            overlap_seconds=round(overlap, 1),
            min_score=round(min_sc, 1),
            min_window_score=round(min_win, 1),
            min_words_per_min=round(min_wpm, 1),
            max_clips_per_chunk=max_per_chunk,
            max_total_clips=max_total,
            max_duration_minutes=max_dur,
            captions=bool(self.captions),
            auto_hook=bool(self.auto_hook),
            auto_hook_style=str(self.auto_hook_style or "pill").strip() or "pill",
            layouts=list(self.layouts or ["auto"]),
            output_format=fmt,
            clip_min_seconds=round(c_min, 1),
            clip_max_seconds=round(c_max, 1),
            webhook_url=self.webhook_url,
            webhook_secret=self.webhook_secret,
        )

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def normalize_live_candidate_url(url: str) -> str:
    """Turn a bare YouTube channel handle (``/@name``) into ``/@name/live`` so
    yt-dlp resolves the channel's active live stream instead of walking its
    entire uploads tab."""
    raw = str(url or "").strip()
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return raw
    host = (parsed.hostname or "").lower()
    if not (host == "youtube.com" or host.endswith(".youtube.com")):
        return raw
    segments = [s for s in (parsed.path or "").split("/") if s]
    if len(segments) == 1 and segments[0].startswith("@"):
        new_path = f"/{segments[0]}/live"
        return urllib.parse.urlunparse(parsed._replace(path=new_path))
    if len(segments) == 2 and segments[0].startswith("@") and segments[1] in ("streams", "featured", "videos"):
        new_path = f"/{segments[0]}/live"
        return urllib.parse.urlunparse(parsed._replace(path=new_path))
    if len(segments) == 2 and segments[0] in ("channel", "c", "user"):
        new_path = f"/{segments[0]}/{segments[1]}/live"
        return urllib.parse.urlunparse(parsed._replace(path=new_path))
    return raw


def _pick_live_stream_urls(info: dict) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(video_stream_url, audio_stream_url)`` from a yt-dlp info dict.

    Prefers a combined HLS/HTTP stream up to 1080p so a single FFmpeg input
    captures both video and audio. Falls back to separate video + audio URLs.
    """
    if not isinstance(info, dict):
        return None, None
    direct_url = info.get("url")
    formats = info.get("formats") or []
    if not formats and direct_url:
        return str(direct_url), None

    combined = []
    video_only = []
    audio_only = []
    for f in formats:
        if not isinstance(f, dict) or not f.get("url"):
            continue
        proto = str(f.get("protocol") or "")
        if proto == "mhtml" or f.get("ext") == "mhtml":
            continue
        vcodec = str(f.get("vcodec") or "none")
        acodec = str(f.get("acodec") or "none")
        height = int(f.get("height") or 0)
        tbr = float(f.get("tbr") or 0.0)
        has_v = vcodec != "none"
        has_a = acodec != "none"
        if has_v and has_a:
            if height <= 1080:
                combined.append((1 if height >= 720 else 0, height, tbr, str(f["url"])))
        elif has_v:
            if height <= 1080:
                video_only.append((1 if height >= 720 else 0, height, tbr, str(f["url"])))
        elif has_a:
            audio_only.append((tbr, str(f["url"])))

    if combined:
        combined.sort(reverse=True)
        return combined[0][3], None
    if video_only and audio_only:
        video_only.sort(reverse=True)
        audio_only.sort(reverse=True)
        return video_only[0][3], audio_only[0][3]
    if direct_url:
        return str(direct_url), None
    if video_only:
        video_only.sort(reverse=True)
        return video_only[0][3], None
    return None, None


def probe_live_stream(url: str, timeout: int = 25) -> Dict[str, Any]:
    """Check whether ``url`` is currently live and extract stream metadata.

    Uses ``STATIC_PROXY_URLS`` (or direct), never the paid per-GB ``PROXY_URL``.
    Safe to call from API endpoints (`GET /api/live/probe`) and the recorder.
    """
    from security_utils import assert_public_url
    import ssrf_guard
    import yt_account
    import yt_dlp

    target_url = normalize_live_candidate_url(url)
    assert_public_url(target_url)

    statics = [p.strip() for p in os.environ.get("STATIC_PROXY_URLS", "").split(",") if p.strip()]
    proxy = statics[0] if statics else None

    yt_session = yt_account.Session()
    cookies_path = yt_session.path if yt_account.cooling_down() <= 0 else None

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlist_items": "1",
        "socket_timeout": timeout,
        "retries": 2,
        "cachedir": False,
        "proxy": proxy,
        "cookiefile": cookies_path,
    }

    info = None
    last_error = None
    for use_cookies in ([True, False] if cookies_path else [False]):
        attempt_opts = {**opts, "cookiefile": cookies_path if use_cookies else None}
        try:
            with (yt_session.request() if use_cookies else contextlib.nullcontext()):
                with ssrf_guard.guarded(), yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(target_url, download=False)
            if use_cookies:
                yt_session.ok()
            break
        except Exception as e:
            last_error = e
            if use_cookies and yt_account.is_rate_limit(str(e)):
                yt_session.limited()
            continue

    if info is None:
        err_text = str(last_error or "Unable to probe URL")
        # Many platforms raise a specific message when a channel is offline.
        offline_markers = (
            "is offline",
            "not currently live",
            "this live event will begin",
            "premieres in",
            "live stream offline",
        )
        if any(m in err_text.lower() for m in offline_markers):
            return {
                "url": target_url,
                "is_live": False,
                "live_status": "not_live",
                "video_id": None,
                "title": None,
                "uploader": None,
                "webpage_url": target_url,
                "stream_url": None,
                "audio_stream_url": None,
                "detail": err_text[:300],
            }
        raise RuntimeError(f"Could not probe live URL: {err_text[:300]}")

    if "entries" in info and isinstance(info["entries"], list) and info["entries"]:
        info = info["entries"][0] or info

    live_status = str(info.get("live_status") or ("is_live" if info.get("is_live") else "not_live"))
    is_live = bool(info.get("is_live") or live_status == "is_live")
    stream_url, audio_stream_url = _pick_live_stream_urls(info)
    if stream_url:
        ssrf_guard.assert_public_url(stream_url)
    if audio_stream_url:
        ssrf_guard.assert_public_url(audio_stream_url)

    return {
        "url": target_url,
        "is_live": is_live,
        "live_status": live_status,
        "video_id": info.get("id"),
        "title": info.get("title") or "Live Stream",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "webpage_url": info.get("webpage_url") or target_url,
        "stream_url": stream_url,
        "audio_stream_url": audio_stream_url,
        "manifest_available": bool(stream_url),
    }


def _media_duration_seconds(path: str) -> float:
    """Fast ffprobe duration check for a recorded segment/chunk."""
    if not path or not os.path.exists(path):
        return 0.0
    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return max(0.0, float((res.stdout or "").strip() or 0.0))
    except Exception:
        return 0.0


def _remux_with_optional_overlap(
    raw_segment_path: str,
    prev_tail_path: Optional[str],
    out_mp4_path: str,
    next_tail_path: Optional[str],
    overlap_seconds: float,
) -> float:
    """Build ``out_mp4_path`` from ``prev_tail_path`` (if present) + ``raw_segment_path``
    using stream copy (`-c copy`), and save the last ``overlap_seconds`` of
    ``raw_segment_path`` into ``next_tail_path`` for the following chunk.

    Returns the duration of ``out_mp4_path`` in seconds.
    """
    work_dir = os.path.dirname(out_mp4_path)
    concat_list_path = os.path.join(work_dir, f".concat_{os.path.basename(out_mp4_path)}.txt")
    try:
        if prev_tail_path and os.path.exists(prev_tail_path) and os.path.getsize(prev_tail_path) > 0:
            with open(concat_list_path, "w", encoding="utf-8") as f:
                p1 = prev_tail_path.replace("'", "'\\''")
                p2 = raw_segment_path.replace("'", "'\\''")
                f.write(f"file '{p1}'\nfile '{p2}'\n")
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", concat_list_path,
                "-c", "copy", "-movflags", "+faststart", out_mp4_path,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if res.returncode != 0 or not os.path.exists(out_mp4_path):
                # Fallback: remux raw segment alone without the overlap tail
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", raw_segment_path, "-c", "copy", "-movflags", "+faststart",
                        out_mp4_path,
                    ],
                    check=True, capture_output=True, text=True, timeout=180,
                )
        else:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", raw_segment_path, "-c", "copy", "-movflags", "+faststart",
                    out_mp4_path,
                ],
                check=True, capture_output=True, text=True, timeout=180,
            )
    finally:
        if os.path.exists(concat_list_path):
            try:
                os.remove(concat_list_path)
            except OSError:
                pass

    # Save the last `overlap_seconds` of raw_segment_path for the next chunk
    if next_tail_path and overlap_seconds > 0:
        tmp_tail = f"{next_tail_path}.tmp.ts"
        try:
            res = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-sseof", f"-{overlap_seconds:.1f}", "-i", raw_segment_path,
                    "-c", "copy", "-f", "mpegts", tmp_tail,
                ],
                capture_output=True, text=True, timeout=60,
            )
            if res.returncode == 0 and os.path.exists(tmp_tail) and os.path.getsize(tmp_tail) > 0:
                os.replace(tmp_tail, next_tail_path)
            elif os.path.exists(tmp_tail):
                os.remove(tmp_tail)
        except Exception:
            if os.path.exists(tmp_tail):
                try:
                    os.remove(tmp_tail)
                except OSError:
                    pass

    return _media_duration_seconds(out_mp4_path)


def merge_chunk_into_session_metadata(
    session_output_dir: str,
    session_base_name: str,
    job_id: str,
    chunk_work_dir: str,
    chunk_base_name: str,
    chunk_metadata: dict,
    chunk_index: int,
    chunk_stream_offset: float,
    transcript_offset: float,
    existing_stream_spans: List[Tuple[float, float]],
    max_total_clips: int = 30,
) -> Dict[str, Any]:
    """Merge approved clips and shifted transcript segments from a completed
    live chunk into the master session directory and ``*_metadata.json``.

    Preserves any changes (e.g. ``video_url`` or ``caption_style``) already
    written to earlier clips by concurrent ``POST /api/subtitle`` requests.
    Returns ``{"added_clips": [...], "new_transcript_offset": float}``.
    """
    os.makedirs(session_output_dir, exist_ok=True)
    master_meta_path = os.path.join(session_output_dir, f"{session_base_name}_metadata.json")
    if os.path.exists(master_meta_path):
        try:
            with open(master_meta_path, "r", encoding="utf-8") as f:
                master_data = json.load(f)
        except Exception:
            master_data = {}
    else:
        master_data = {}

    master_shorts = list(master_data.get("shorts") or [])
    master_transcript = master_data.get("transcript") or {"language": "unknown", "segments": []}
    master_segments = list(master_transcript.get("segments") or [])

    raw_shorts = list((chunk_metadata or {}).get("shorts") or [])
    # Deduplicate against clips emitted by previous chunks (e.g. 30s overlap)
    candidates = dedupe_against_stream_spans(
        raw_shorts, existing_stream_spans, chunk_stream_offset, ratio=0.25
    )

    remaining_budget = max(0, int(max_total_clips) - len(master_shorts))
    candidates = candidates[:remaining_budget]

    chunk_transcript = (chunk_metadata or {}).get("transcript") or {"language": "unknown", "segments": []}
    shifted_tr = shift_transcript(chunk_transcript, transcript_offset)
    chunk_max_end = 0.0
    for seg in (chunk_transcript.get("segments") or []):
        try:
            chunk_max_end = max(chunk_max_end, float(seg.get("end") or 0.0))
        except (TypeError, ValueError):
            pass

    if not candidates:
        return {
            "added_clips": [],
            "all_clips": master_shorts,
            "new_transcript_offset": transcript_offset,
        }

    # Map each candidate back to its original 0-based index in raw_shorts so we
    # move the right rendered files (`{chunk_base_name}_clip_{orig_idx+1}.mp4`).
    added_clips = []
    for clip in candidates:
        try:
            orig_idx = raw_shorts.index(clip)
        except ValueError:
            continue
        new_idx = len(master_shorts)
        old_clip_stem = f"{chunk_base_name}_clip_{orig_idx + 1}"
        new_clip_stem = f"{session_base_name}_clip_{new_idx + 1}"

        # Move all files belonging to this clip (clean reframe, hooked_, subtitled_, wm_, layout_ranges)
        served_name = None
        for entry in sorted(os.listdir(chunk_work_dir)):
            if old_clip_stem not in entry:
                continue
            src_path = os.path.join(chunk_work_dir, entry)
            if not os.path.isfile(src_path):
                continue
            new_entry = entry.replace(old_clip_stem, new_clip_stem)
            dst_path = os.path.join(session_output_dir, new_entry)
            shutil.move(src_path, dst_path)

        # Determine the canonical served file for this clip
        for prefix in ("wm_subtitled_", "subtitled_", "wm_hooked_", "hooked_", "wm_", ""):
            matches = sorted(
                glob.glob(os.path.join(session_output_dir, f"{prefix}*{new_clip_stem}.mp4")),
                key=os.path.getmtime,
                reverse=True,
            )
            if matches:
                served_name = os.path.basename(matches[0])
                break
        if not served_name:
            continue

        orig_start = float(clip.get("start") or 0.0)
        orig_end = float(clip.get("end") or 0.0)
        chunk_max_end = max(chunk_max_end, orig_end)

        merged_clip = dict(clip)
        merged_clip["clip_index"] = new_idx
        merged_clip["chunk_index"] = chunk_index
        merged_clip["chunk_start"] = round(orig_start, 3)
        merged_clip["chunk_end"] = round(orig_end, 3)
        merged_clip["stream_start"] = round(orig_start + chunk_stream_offset, 3)
        merged_clip["stream_end"] = round(orig_end + chunk_stream_offset, 3)
        merged_clip["start"] = round(orig_start + transcript_offset, 3)
        merged_clip["end"] = round(orig_end + transcript_offset, 3)
        merged_clip["video_url"] = f"/videos/{job_id}/{served_name}"
        merged_clip["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

        existing_stream_spans.append((merged_clip["stream_start"], merged_clip["stream_end"]))
        master_shorts.append(merged_clip)
        added_clips.append(merged_clip)

    if added_clips:
        master_segments.extend(shifted_tr.get("segments") or [])
        lang = shifted_tr.get("language") or master_transcript.get("language") or "unknown"
        master_data["transcript"] = {"language": lang, "segments": master_segments}
        master_data["shorts"] = master_shorts
        master_data["output_format"] = (
            chunk_metadata.get("output_format")
            or master_data.get("output_format")
            or "vertical"
        )
        # Aggregate cost_analysis across chunks
        chunk_cost = (chunk_metadata or {}).get("cost_analysis")
        if chunk_cost:
            prev_cost = master_data.get("cost_analysis") or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_cost": 0.0,
                "model": chunk_cost.get("model", ""),
            }
            master_data["cost_analysis"] = {
                "input_tokens": int(prev_cost.get("input_tokens", 0)) + int(chunk_cost.get("input_tokens", 0)),
                "output_tokens": int(prev_cost.get("output_tokens", 0)) + int(chunk_cost.get("output_tokens", 0)),
                "total_cost": round(float(prev_cost.get("total_cost", 0.0)) + float(chunk_cost.get("total_cost", 0.0)), 6),
                "model": chunk_cost.get("model") or prev_cost.get("model", ""),
            }
        write_json_atomic(master_meta_path, master_data, indent=2)

    next_offset = transcript_offset + max(chunk_max_end, 60.0) + 10.0
    return {
        "added_clips": added_clips,
        "all_clips": master_shorts,
        "new_transcript_offset": round(next_offset, 3),
    }


class LiveSession:
    """Manages one real-time live stream monitoring & clipping session."""

    def __init__(
        self,
        job_id: str,
        output_dir: str,
        config: LiveConfig,
        base_env: Dict[str, str],
        job_record: Dict[str, Any],
        owner_id: Optional[str] = None,
        on_clip_ready: Optional[Callable[[str, dict], None]] = None,
        on_session_finished: Optional[Callable[[str, dict], None]] = None,
    ):
        self.job_id = job_id
        self.output_dir = output_dir
        self.chunks_dir = os.path.join(output_dir, ".live_chunks")
        self.config = config.normalized()
        self.base_env = dict(base_env or {})
        self.job_record = job_record
        self.owner_id = str(owner_id) if owner_id else None
        self.on_clip_ready = on_clip_ready
        self.on_session_finished = on_session_finished

        self.title = "Live Stream"
        self.uploader = ""
        self.video_id: Optional[str] = None
        self.webpage_url = self.config.url
        self.state = "starting"  # starting | monitoring | stopping | completed | failed
        self.active = True
        self.stop_requested = False
        self.process_partial_on_stop = True
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.started_at_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        self.ended_at_iso: Optional[str] = None

        self.stats = {
            "chunks_captured": 0,
            "chunks_processing": 0,
            "chunks_processed": 0,
            "chunks_skipped_sparse": 0,
            "chunks_skipped_low_score": 0,
            "chunks_with_clips": 0,
            "clips_generated": 0,
            "disk_cleaned_bytes": 0,
        }
        self.chunks: List[Dict[str, Any]] = []
        self.emitted_stream_spans: List[Tuple[float, float]] = []
        self.transcript_offset: float = 0.0
        self.metadata_lock = threading.Lock()

        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._main_task: Optional[asyncio.Task] = None
        self._analysis_queue: Optional[asyncio.Queue] = None

    def _log(self, message: str) -> None:
        print(f"🔴 [live:{self.job_id[:8]}] {message}", flush=True)
        logs = self.job_record.get("logs")
        if isinstance(logs, list):
            logs.append(message)
        times = self.job_record.get("log_times")
        if isinstance(times, list):
            times.append(round(time.time(), 3))

    def _record_cleaned_bytes(self, nbytes: int) -> None:
        if nbytes > 0:
            self.stats["disk_cleaned_bytes"] += int(nbytes)

    def _remove_file_counted(self, path: Optional[str]) -> int:
        if not path or not os.path.exists(path):
            return 0
        try:
            sz = os.path.getsize(path)
        except OSError:
            sz = 0
        try:
            os.remove(path)
            self._record_cleaned_bytes(sz)
            return sz
        except OSError:
            return 0

    def _remove_dir_counted(self, dir_path: Optional[str]) -> int:
        if not dir_path or not os.path.exists(dir_path):
            return 0
        total = 0
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                fp = os.path.join(root, fname)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
        try:
            shutil.rmtree(dir_path, ignore_errors=True)
            self._record_cleaned_bytes(total)
            return total
        except OSError:
            return 0

    def snapshot(self) -> Dict[str, Any]:
        elapsed = round(max(0.0, time.time() - self.started_at), 1)
        cleaned_mb = round(self.stats["disk_cleaned_bytes"] / (1024 * 1024), 2)
        return {
            "job_id": self.job_id,
            "url": self.config.url,
            "webpage_url": self.webpage_url,
            "video_id": self.video_id,
            "title": self.title,
            "uploader": self.uploader,
            "state": self.state,
            "active": self.active,
            "stop_requested": self.stop_requested,
            "error": self.error,
            "started_at": self.started_at_iso,
            "ended_at": self.ended_at_iso,
            "elapsed_seconds": elapsed,
            "config": self.config.to_dict(),
            "stats": {
                **self.stats,
                "disk_cleaned_mb": cleaned_mb,
            },
            "chunks": list(self.chunks[-50:]),
        }

    def start(self) -> asyncio.Task:
        self._main_task = asyncio.create_task(self._run_loop())
        return self._main_task

    def request_stop(self, process_partial: bool = True) -> None:
        self.stop_requested = True
        self.process_partial_on_stop = bool(process_partial)
        if self.state in ("starting", "monitoring"):
            self.state = "stopping"
        self._log(
            "⏹️ Solicitação de parada recebida — encerrando gravação da live"
            + (" e analisando o bloco atual..." if process_partial else " imediatamente...")
        )
        proc = self._ffmpeg_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    async def _run_loop(self) -> None:
        os.makedirs(self.chunks_dir, exist_ok=True)
        self._analysis_queue = asyncio.Queue()
        analyzer_task = asyncio.create_task(self._analyzer_worker())
        chunk_seconds = int(round(self.config.chunk_minutes * 60.0))
        deadline = self.started_at + (self.config.max_duration_minutes * 60.0)
        prev_tail_path = os.path.join(self.chunks_dir, "prev_tail.ts")
        chunk_counter = 0

        try:
            self._log(f"🔍 Verificando transmissão ao vivo: {self.config.url}")
            probe = await asyncio.to_thread(probe_live_stream, self.config.url)
            self.title = probe.get("title") or "Live Stream"
            self.uploader = probe.get("uploader") or ""
            self.video_id = probe.get("video_id")
            self.webpage_url = probe.get("webpage_url") or self.config.url

            if not probe.get("is_live"):
                raise RuntimeError(
                    f"Esta URL não está ao vivo no momento (status={probe.get('live_status')})."
                )
            if not probe.get("stream_url"):
                raise RuntimeError("Não foi possível obter o manifesto de vídeo da live.")

            self.state = "monitoring"
            self._log(
                f"🔴 Live conectada: \"{self.title}\" | Janelas de {self.config.chunk_minutes:g} min "
                f"(overlap {self.config.overlap_seconds:g}s) | Score mínimo IA: {self.config.min_score:g}"
            )

            while not self.stop_requested and time.time() < deadline:
                if self.stats["clips_generated"] >= self.config.max_total_clips:
                    self._log(
                        f"🎯 Limite de {self.config.max_total_clips} cortes da sessão atingido. Encerrando acompanhamento."
                    )
                    break

                stream_url = probe.get("stream_url")
                audio_url = probe.get("audio_stream_url")
                if not stream_url:
                    break

                batch_prefix = f"batch_{chunk_counter:04d}"
                seg_pattern = os.path.join(self.chunks_dir, f"{batch_prefix}_%04d.ts")
                seg_list_file = os.path.join(self.chunks_dir, f"{batch_prefix}_list.txt")

                ffmpeg_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning"]
                ffmpeg_cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
                ffmpeg_cmd.extend(["-i", stream_url])
                if audio_url:
                    ffmpeg_cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
                    ffmpeg_cmd.extend(["-i", audio_url, "-map", "0:v:0", "-map", "1:a:0"])
                ffmpeg_cmd.extend([
                    "-c", "copy",
                    "-f", "segment",
                    "-segment_time", str(chunk_seconds),
                    "-segment_format", "mpegts",
                    "-reset_timestamps", "1",
                    "-segment_list", seg_list_file,
                    "-segment_list_type", "flat",
                    "-segment_list_flags", "+live",
                    seg_pattern,
                ])

                self._ffmpeg_proc = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                seen_segments: set = set()

                while self._ffmpeg_proc.poll() is None:
                    if self.stop_requested or time.time() >= deadline:
                        try:
                            self._ffmpeg_proc.terminate()
                        except Exception:
                            pass
                        break
                    if self.stats["clips_generated"] >= self.config.max_total_clips:
                        try:
                            self._ffmpeg_proc.terminate()
                        except Exception:
                            pass
                        break

                    # Check segment_list for newly closed segments
                    if os.path.exists(seg_list_file):
                        try:
                            with open(seg_list_file, "r", encoding="utf-8") as lf:
                                lines = [ln.strip() for ln in lf.read().splitlines() if ln.strip()]
                        except OSError:
                            lines = []
                        for seg_name in lines:
                            if seg_name in seen_segments:
                                continue
                            seg_path = os.path.join(self.chunks_dir, os.path.basename(seg_name))
                            if os.path.exists(seg_path):
                                seen_segments.add(seg_name)
                                await self._enqueue_raw_segment(
                                    seg_path, prev_tail_path, chunk_counter
                                )
                                chunk_counter += 1

                    await asyncio.sleep(2.0)

                # Wait briefly for ffmpeg to flush on exit
                try:
                    await asyncio.to_thread(self._ffmpeg_proc.wait, 5)
                except Exception:
                    try:
                        self._ffmpeg_proc.kill()
                    except Exception:
                        pass

                # Check any remaining segments written before exit
                for candidate in sorted(glob.glob(os.path.join(self.chunks_dir, f"{batch_prefix}_*.ts"))):
                    bname = os.path.basename(candidate)
                    if bname in seen_segments:
                        continue
                    seen_segments.add(bname)
                    if self.stop_requested and not self.process_partial_on_stop:
                        self._remove_file_counted(candidate)
                        continue
                    dur = await asyncio.to_thread(_media_duration_seconds, candidate)
                    if dur >= MIN_PARTIAL_CHUNK_SECONDS:
                        await self._enqueue_raw_segment(candidate, prev_tail_path, chunk_counter)
                        chunk_counter += 1
                    else:
                        self._remove_file_counted(candidate)

                if os.path.exists(seg_list_file):
                    self._remove_file_counted(seg_list_file)

                if self.stop_requested or time.time() >= deadline:
                    break
                if self.stats["clips_generated"] >= self.config.max_total_clips:
                    break

                # FFmpeg exited on its own: check if the live stream is still running
                await asyncio.sleep(3.0)
                try:
                    probe = await asyncio.to_thread(probe_live_stream, self.config.url)
                except Exception:
                    break
                if not probe.get("is_live") or not probe.get("stream_url"):
                    self._log("🏁 A transmissão ao vivo foi encerrada pelo canal.")
                    break
                self._log("🔄 Reconectando ao stream da live...")

            # Signal analyzer queue to finish all queued chunks
            await self._analysis_queue.put(None)
            await analyzer_task

            self.state = "completed"
            self.active = False
            self.ended_at_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            self.job_record["status"] = "completed"
            if self.job_record.get("result") is None:
                self.job_record["result"] = {"clips": []}
            cleaned_mb = self.stats["disk_cleaned_bytes"] / (1024 * 1024)
            self._log(
                f"✅ Acompanhamento da live finalizado: {self.stats['clips_generated']} corte(s) gerado(s) "
                f"de {self.stats['chunks_processed']} janela(s) analisada(s) "
                f"({cleaned_mb:.1f} MB de vídeo bruto descartados automaticamente)."
            )
        except Exception as e:
            self.error = str(e)
            self.state = "failed"
            self.active = False
            self.ended_at_iso = dt.datetime.now(dt.timezone.utc).isoformat()
            self.job_record["status"] = "failed"
            self._log(f"❌ Erro no acompanhamento da live: {e}")
            if self._analysis_queue is not None:
                await self._analysis_queue.put(None)
            with contextlib.suppress(Exception):
                await analyzer_task
        finally:
            self.active = False
            self._remove_dir_counted(self.chunks_dir)
            if self.on_session_finished:
                try:
                    res = self.on_session_finished(self.job_id, self.snapshot())
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as cb_err:
                    print(f"⚠️ Live finish callback error: {cb_err}")

    async def _enqueue_raw_segment(
        self, raw_seg_path: str, prev_tail_path: str, chunk_index: int
    ) -> None:
        """Remux a finished `.ts` segment (+ previous 30s overlap tail) into an
        `.mp4` chunk, delete the `.ts` immediately, and queue the `.mp4` for AI analysis."""
        chunk_mp4 = os.path.join(self.chunks_dir, f"chunk_{chunk_index:04d}.mp4")
        next_tail = os.path.join(self.chunks_dir, f"tail_{chunk_index:04d}.ts")
        had_prev_tail = os.path.exists(prev_tail_path) and os.path.getsize(prev_tail_path) > 0
        try:
            duration = await asyncio.to_thread(
                _remux_with_optional_overlap,
                raw_seg_path,
                prev_tail_path if had_prev_tail else None,
                chunk_mp4,
                next_tail,
                self.config.overlap_seconds,
            )
        except Exception as e:
            self._log(f"⚠️ Falha ao preparar bloco #{chunk_index + 1}: {e}")
            self._remove_file_counted(raw_seg_path)
            return
        finally:
            self._remove_file_counted(raw_seg_path)
            if had_prev_tail:
                self._remove_file_counted(prev_tail_path)
            if os.path.exists(next_tail):
                try:
                    os.replace(next_tail, prev_tail_path)
                except OSError:
                    pass

        stream_start = max(
            0.0,
            chunk_index * (self.config.chunk_minutes * 60.0)
            - (self.config.overlap_seconds if chunk_index > 0 else 0.0),
        )
        stream_end = stream_start + duration
        chunk_rec = {
            "chunk_index": chunk_index,
            "stream_start": round(stream_start, 1),
            "stream_end": round(stream_end, 1),
            "duration": round(duration, 1),
            "status": "queued",
            "reason": None,
            "words_per_min": None,
            "max_window_score": None,
            "clips_count": 0,
            "cleaned_bytes": 0,
        }
        self.chunks.append(chunk_rec)
        self.stats["chunks_captured"] += 1
        self._log(
            f"📦 Janela #{chunk_index + 1} capturada ({stream_start / 60:.1f}m – {stream_end / 60:.1f}m da live, "
            f"{duration:.0f}s) → enviando para transcrição e filtro IA..."
        )
        await self._analysis_queue.put((chunk_index, chunk_mp4, chunk_rec, stream_start))

    async def _analyzer_worker(self) -> None:
        while True:
            item = await self._analysis_queue.get()
            if item is None:
                self._analysis_queue.task_done()
                break
            chunk_index, chunk_mp4, chunk_rec, stream_start = item
            try:
                await self._process_one_chunk(chunk_index, chunk_mp4, chunk_rec, stream_start)
            except Exception as e:
                chunk_rec["status"] = "failed"
                chunk_rec["reason"] = str(e)[:200]
                self._log(f"⚠️ Erro na análise da janela #{chunk_index + 1}: {e}")
            finally:
                self._analysis_queue.task_done()

    async def _process_one_chunk(
        self,
        chunk_index: int,
        chunk_mp4: str,
        chunk_rec: Dict[str, Any],
        stream_start: float,
    ) -> None:
        work_dir = os.path.join(self.chunks_dir, f"work_{chunk_index:04d}")
        os.makedirs(work_dir, exist_ok=True)
        chunk_rec["status"] = "processing"
        self.stats["chunks_processing"] += 1
        before_cleaned = self.stats["disk_cleaned_bytes"]

        try:
            env = dict(self.base_env)
            env["PYTHONIOENCODING"] = "utf-8"
            env["LIVE_MODE"] = "1"
            env["LIVE_MIN_SCORE"] = str(self.config.min_score)
            if self.config.min_window_score is not None:
                env["LIVE_MIN_WINDOW_SCORE"] = str(self.config.min_window_score)
            env["LIVE_MIN_WORDS_PER_MIN"] = str(self.config.min_words_per_min)
            env["CLIP_TARGET_MIN"] = "0"
            env["CLIP_TARGET_MAX"] = str(self.config.max_clips_per_chunk)
            env["CLIP_MIN_SECONDS"] = str(self.config.clip_min_seconds)
            env["CLIP_MAX_SECONDS"] = str(self.config.clip_max_seconds)
            env["AUTO_CAPTIONS"] = "1" if self.config.captions else "0"
            if self.config.auto_hook:
                env["AUTO_HOOK"] = "1"
                env["AUTO_HOOK_STYLE"] = self.config.auto_hook_style
            else:
                env.pop("AUTO_HOOK", None)

            cmd = [
                sys.executable, "-u", "main.py",
                "-i", chunk_mp4,
                "-o", work_dir,
                "--format", self.config.output_format,
            ]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.getcwd(),
            )

            skipped_reason = None
            words_per_min = None
            max_win_score = None

            def _read_chunk_stdout():
                nonlocal skipped_reason, words_per_min, max_win_score
                for raw in iter(proc.stdout.readline, b""):
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    if line.startswith("LIVE_WORDS_PER_MIN "):
                        with contextlib.suppress(ValueError):
                            words_per_min = float(line.split()[1])
                    elif line.startswith("LIVE_MAX_WINDOW_SCORE "):
                        with contextlib.suppress(ValueError):
                            max_win_score = float(line.split()[1])
                    elif line.startswith("LIVE_SKIPPED "):
                        parts = line.split()
                        skipped_reason = parts[1] if len(parts) > 1 else "low_score"
                        if len(parts) > 2 and max_win_score is None:
                            with contextlib.suppress(ValueError):
                                max_win_score = float(parts[2])

            reader = threading.Thread(target=_read_chunk_stdout, daemon=True)
            reader.start()
            while proc.poll() is None:
                await asyncio.sleep(1.5)
            await asyncio.to_thread(reader.join, 5)

            chunk_rec["words_per_min"] = words_per_min
            chunk_rec["max_window_score"] = max_win_score

            meta_files = glob.glob(os.path.join(work_dir, "*_metadata.json"))
            chunk_meta = {}
            chunk_base = f"chunk_{chunk_index:04d}"
            if meta_files:
                chunk_base = os.path.basename(meta_files[0]).replace("_metadata.json", "")
                with open(meta_files[0], "r", encoding="utf-8") as mf:
                    chunk_meta = json.load(mf)

            if not skipped_reason and chunk_meta.get("live_skipped"):
                skipped_reason = chunk_meta.get("live_skipped")

            shorts = chunk_meta.get("shorts") or []
            if skipped_reason or not shorts:
                reason = skipped_reason or "low_score"
                if reason == "sparse_speech":
                    self.stats["chunks_skipped_sparse"] += 1
                    chunk_rec["status"] = "skipped_sparse"
                    chunk_rec["reason"] = "sparse_speech"
                    self._log(
                        f"⏭️ Janela #{chunk_index + 1} ignorada (fala esparsa/silêncio: "
                        f"{words_per_min or 0:.1f} palavras/min). Vídeo bruto descartado."
                    )
                else:
                    self.stats["chunks_skipped_low_score"] += 1
                    chunk_rec["status"] = "skipped_low_score"
                    chunk_rec["reason"] = reason
                    self._log(
                        f"⏭️ Janela #{chunk_index + 1} ignorada (nenhum momento atingiu score >= "
                        f"{self.config.min_score:g}; máx janela={max_win_score or 0:.0f}). Vídeo bruto descartado."
                    )
                return

            with self.metadata_lock:
                merged = merge_chunk_into_session_metadata(
                    session_output_dir=self.output_dir,
                    session_base_name=LIVE_METADATA_BASENAME,
                    job_id=self.job_id,
                    chunk_work_dir=work_dir,
                    chunk_base_name=chunk_base,
                    chunk_metadata=chunk_meta,
                    chunk_index=chunk_index,
                    chunk_stream_offset=stream_start,
                    transcript_offset=self.transcript_offset,
                    existing_stream_spans=self.emitted_stream_spans,
                    max_total_clips=self.config.max_total_clips,
                )
                self.transcript_offset = merged["new_transcript_offset"]
                added_clips = merged["added_clips"]
                all_clips = merged["all_clips"]

                self.job_record["result"] = {"clips": all_clips}
                ready_files = self.job_record.setdefault("ready_files", {})
                for c in all_clips:
                    cidx = c.get("clip_index")
                    vurl = c.get("video_url") or ""
                    if cidx is not None and vurl:
                        ready_files[int(cidx)] = os.path.basename(vurl)

            if added_clips:
                self.stats["chunks_with_clips"] += 1
                self.stats["clips_generated"] = len(all_clips)
                chunk_rec["status"] = "completed"
                chunk_rec["clips_count"] = len(added_clips)
                titles = ", ".join(
                    f"#{c['clip_index'] + 1} ({c.get('predicted_score', '?')} pts)"
                    for c in added_clips
                )
                self._log(
                    f"🔥 Janela #{chunk_index + 1}: {len(added_clips)} novo(s) corte(s) pronto(s) [{titles}]!"
                )
                if self.on_clip_ready:
                    for clip in added_clips:
                        try:
                            res = self.on_clip_ready(self.job_id, clip)
                            if asyncio.iscoroutine(res):
                                await res
                        except Exception as cb_err:
                            print(f"⚠️ on_clip_ready callback error: {cb_err}")
            else:
                self.stats["chunks_skipped_low_score"] += 1
                chunk_rec["status"] = "skipped_duplicate"
                chunk_rec["reason"] = "duplicate_overlap"
                self._log(
                    f"⏭️ Janela #{chunk_index + 1}: cortes coincidiam com o overlap da janela anterior."
                )
        finally:
            self.stats["chunks_processing"] = max(0, self.stats["chunks_processing"] - 1)
            self.stats["chunks_processed"] += 1
            self._remove_file_counted(chunk_mp4)
            self._remove_dir_counted(work_dir)
            chunk_rec["cleaned_bytes"] = self.stats["disk_cleaned_bytes"] - before_cleaned


# --------------------------------------------------------------------------- #
# Session Registry (in-process, shared with app.py)
# --------------------------------------------------------------------------- #
_live_sessions: Dict[str, LiveSession] = {}


def get_session(job_id: str) -> Optional[LiveSession]:
    return _live_sessions.get(job_id)


def find_active_session_by_url(url: str, owner_id: Optional[str] = None) -> Optional[LiveSession]:
    norm = normalize_live_candidate_url(url).rstrip("/")
    for sess in _live_sessions.values():
        if not sess.active:
            continue
        if owner_id is not None and sess.owner_id != str(owner_id):
            continue
        if normalize_live_candidate_url(sess.config.url).rstrip("/") == norm or (
            sess.webpage_url and sess.webpage_url.rstrip("/") == norm
        ):
            return sess
    return None


def register_session(session: LiveSession) -> LiveSession:
    _live_sessions[session.job_id] = session
    return session


def list_sessions(owner_id: Optional[str] = None, active_only: bool = False) -> List[Dict[str, Any]]:
    out = []
    for sess in sorted(_live_sessions.values(), key=lambda s: s.started_at, reverse=True):
        if owner_id is not None and sess.owner_id != str(owner_id):
            continue
        if active_only and not sess.active:
            continue
        out.append(sess.snapshot())
    return out
