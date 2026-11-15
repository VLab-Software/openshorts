"""
Pure helpers for the Gemini clip-selection pipeline.

Standard-library only so both main.py and gemini_worker.py can import it and
the logic stays unit-testable without the heavy video dependencies.
"""
import math

# USD per 1M tokens (input, output incl. thinking), from ai.google.dev pricing.
MODEL_PRICES = {
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),  # deprecated (shut down 2026-06-01)
}


def lookup_model_prices(model_name):
    """Longest-prefix match against MODEL_PRICES; None if unknown."""
    name = str(model_name or "").lower()
    best_key = None
    for key in MODEL_PRICES:
        if name.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return MODEL_PRICES[best_key] if best_key else None


def clip_count_targets(n_windows):
    """How many clips to ask the detail pass for, given the shortlist size.

    Measured on prod 3-ago-2026: 408 of 429 jobs (95%) delivered 3 clips or
    fewer, the mode being ONE, while the prompt was free to return one per
    shortlisted window. Users who received 1-3 clips came back a second day
    0.4% of the time; those who received 4-9 came back 16.1% — so the clip
    count, not the clip quality, is what the retention curve hangs on.

    The old prompt biased hard the other way ("prefer one great clip per
    candidate window") and handed the model two unbounded licences to drop
    clips (the 2-second rule and STANDS ALONE both end in "or skip it"), with
    no floor to stop it collapsing to a single clip. This puts a floor and a
    realistic ceiling on it instead.

    ``CLIP_TARGET_MIN`` / ``CLIP_TARGET_MAX`` override both for A/B runs
    without a deploy (the reframe-testing harness drives them).
    """
    import os

    n = max(1, int(n_windows or 1))
    live_mode = os.environ.get("LIVE_MODE", "").strip() == "1"
    # Floor grows with the material: 3 windows -> 3, 5 -> 4, 10+ -> 6.
    # In LIVE_MODE a boring rolling window should yield 0 clips rather than filler.
    low = 0 if live_mode else max(2, min(6, n // 2 + 2))
    # Ceiling allows a rich window to yield more than one without inviting padding.
    high = min(12, max(4, n * 2))
    low = min(low, high)

    def _override(name, current, minimum=1):
        raw = os.environ.get(name)
        if not raw:
            return current
        try:
            return max(minimum, int(raw))
        except ValueError:
            return current

    low = _override("CLIP_TARGET_MIN", low, minimum=0 if live_mode else 1)
    high = _override("CLIP_TARGET_MAX", high, minimum=1)
    return low, max(low, high)


def shortlist_target(video_duration):
    """How many scored windows the detail pass should get.

    Scales with duration so a long video surfaces more candidates without
    exploding the detail call, floored at 3 so a short one still gets a
    choice. ``score_batches`` exists to make this number reachable.
    """
    try:
        seconds = float(video_duration or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    return max(3, min(10, int(seconds // 90) + 2))


def score_batches(windows, batch_size):
    """Split ``windows`` into near-equal scoring batches.

    Two things were wrong with the old ``range(0, n, batch_size)`` walk plus a
    prompt that said "choose up to 3 windows from this batch".

    The cap made the shortlist unreachable. Its real ceiling was
    ``3 * n_batches``, not ``shortlist_target``: measured 22-sep-2026 on a
    9:10 source, 9 windows batched 8 + 1 returned 3 + 1 = 4 windows against a
    target of 8. ``clip_count_targets`` then derives the clip floor from that
    halved shortlist, so the detail pass was asked for 4-8 clips instead of
    6-12 and the job delivered 4 (3 in prod on the same video). Every source
    under ~30 minutes was starved the same way, which is the mechanism behind
    "95% of jobs deliver 3 clips or fewer" — the clip count the retention
    curve hangs on (see clip_count_targets). The prompt now scores every
    window it is given and the shortlist is the global top ``target``, so the
    selection happens where the scores can actually be compared.

    Per-batch selection also threw away the ranking it was computing: a batch
    holding five great moments could only contribute three, while a batch of
    filler still contributed its own three.

    The remainder was its own bug: a trailing batch of one window came back
    with that window whatever its score, because a cap of 3 cannot filter a
    batch of 1 — the tail of the video entered the shortlist by arithmetic
    rather than merit (``window_009`` did exactly that on the measured run).
    Near-equal batches keep that from happening and also keep each score
    comparable, since a window judged alone is judged against nothing.
    """
    windows = list(windows or [])
    n = len(windows)
    if not n:
        return []
    size = max(1, int(batch_size or 1))
    n_batches = max(1, -(-n // size))       # ceil
    base, extra = divmod(n, n_batches)

    out = []
    start = 0
    for index in range(n_batches):
        take = base + (1 if index < extra else 0)
        batch = windows[start:start + take]
        start += take
        if batch:
            out.append(batch)
    return out


def trim_to_best(shorts, max_clips):
    """Cut an over-long detail-pass result down to ``max_clips`` BY SCORE.

    The detail pass hands its clips back in transcript order, batch after
    batch, so slicing the list keeps the EARLIEST clips rather than the best
    ones. On a 9-minute walkthrough that quietly threw away everything past
    minute three: the model proposed clips across the whole video, and the
    ones covering the demo, the MCP walkthrough and the close were the tail
    that got dropped. Worse, the failure scales the wrong way — the more
    generous the model is, the more of the video disappears.

    That sabotages the windowing: get_viral_clips builds scoring windows
    precisely because "a single call over the whole transcript clusters picks
    near the start", and a positional slice puts the clustering right back.

    Ranking is by ``predicted_score`` (the detail prompt already asks for it,
    and nothing else was reading it here). Ties keep transcript order, and the
    survivors come back in transcript order too, so clip numbering still runs
    front to back the way every caller downstream expects.
    """
    max_clips = max(1, int(max_clips or 1))
    if len(shorts) <= max_clips:
        return list(shorts)

    def score(item):
        try:
            return float(item[1].get("predicted_score") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    indexed = list(enumerate(shorts))
    best = sorted(indexed, key=score, reverse=True)[:max_clips]
    return [item for _, item in sorted(best, key=lambda pair: pair[0])]


def vision_segments(video_duration, segment_seconds, max_segments):
    """Split a silent source into near-equal ``(start, end)`` stretches for
    the vision pass, each at most ``segment_seconds`` long.

    The vision pass sends Gemini the footage itself at ~300 tokens/second, so
    an hour (~1.08M tokens) no longer fits a 1M-token window and a silent
    multi-hour capture failed at the model. Near-equal stretches, not fixed
    ones: a 41-minute source split at 40 minutes would leave a one-minute
    tail that the prompt has to fill with 3+ clips. A source that fits in one
    stretch returns one stretch, so short videos take the single-upload path
    unchanged. Past ``max_segments`` the source is truncated (the caller logs
    it) rather than spending without bound on one job.
    """
    duration = max(0.0, float(video_duration or 0))
    seg = max(60.0, float(segment_seconds))
    if duration <= seg:
        return [(0.0, round(duration, 3))]
    n = int(math.ceil(duration / seg))
    n_kept = max(1, min(n, int(max_segments)))
    step = duration / n
    return [(round(i * step, 3), round(min(duration, (i + 1) * step), 3))
            for i in range(n_kept)]


def clip_duration_bounds():
    """The clip length band (seconds) the selection prompts and word-snapping
    enforce. ``CLIP_MIN_SECONDS`` / ``CLIP_MAX_SECONDS`` override the classic
    15-60 — set per job by /api/process when the user asks for a specific
    length, or by hand for A/B runs. Values are clamped to platform-sane
    limits and re-ordered so bad input degrades instead of breaking the job.
    """
    import os

    def _read(name, default):
        try:
            return float(os.environ.get(name, ""))
        except ValueError:
            return default

    lo = _read("CLIP_MIN_SECONDS", 15.0)
    hi = _read("CLIP_MAX_SECONDS", 60.0)
    lo = min(max(lo, 5.0), 175.0)
    hi = min(max(hi, 10.0), 180.0)
    if hi < lo + 5.0:  # keep a real band: degenerate ranges starve the model
        hi = min(180.0, lo + 5.0)
    return round(lo, 3), round(hi, 3)


def compact_words(words, precision=2):
    """Round word timestamps for prompts — full float precision wastes tokens."""
    return [
        {
            "w": w.get("w", ""),
            "s": round(float(w.get("s", 0)), precision),
            "e": round(float(w.get("e", 0)), precision),
        }
        for w in words
    ]


def build_transcript_windows(transcript_result, video_duration,
                             window_seconds=90, overlap_seconds=30):
    """
    Build scoring windows aligned to Whisper segment boundaries, so a sentence
    (and usually a viral moment) is never cut in half mid-window. Windows grow
    segment by segment to roughly window_seconds (up to 1.25x for the closing
    segment) and the next window starts ~overlap_seconds before the previous
    end, also snapped to a segment start.
    """
    segments = []
    for segment in transcript_result.get("segments", []):
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        segments.append((float(segment.get("start", 0)), float(segment.get("end", 0)), text))

    windows = []
    window_index = 1
    i = 0
    n = len(segments)
    while i < n:
        w_start = segments[i][0]
        j = i
        # Extend while the NEXT segment still fits within a tolerant cap, so the
        # window closes on a segment boundary near window_seconds.
        while j + 1 < n and segments[j + 1][1] - w_start <= window_seconds * 1.25:
            j += 1
            if segments[j][1] - w_start >= window_seconds:
                break
        w_end = segments[j][1]
        windows.append({
            "id": f"window_{window_index:03d}",
            "start": round(w_start, 3),
            "end": round(w_end, 3),
            "text": " ".join(seg[2] for seg in segments[i:j + 1]),
        })
        window_index += 1

        if j >= n - 1:
            break
        # Next window starts at the first segment beginning after (end - overlap),
        # but always makes progress.
        target = w_end - overlap_seconds
        k = i + 1
        while k <= j and segments[k][0] < target:
            k += 1
        i = max(k, i + 1)

    if not windows:
        windows.append({
            "id": "window_001",
            "start": 0.0,
            "end": round(float(video_duration), 3),
            "text": str(transcript_result.get("text", "") or ""),
        })
    return windows


def dedupe_overlapping(shorts, ratio=0.25):
    """Drop clips that overlap an already-kept clip by ``ratio`` of the
    shorter one, keeping the higher ``predicted_score`` (earlier on a tie).

    0.25, not 0.5: two clips sharing 25 s of 59 s (42%) both survived the old
    bar and the same punchline came out twice in one batch.

    The detail prompt's DIVERSITY rule is the only thing that stopped two
    clips from sharing the same seconds, and a rule is not a guarantee: two
    picks from one window can cover the same moment with different edges,
    and the user then downloads the same short twice. Survivors come back in
    transcript order, like ``trim_to_best``.
    """
    def _score(c):
        try:
            return float(c.get("predicted_score") or 0)
        except (TypeError, ValueError, AttributeError):
            return 0.0

    def _span(c):
        try:
            return float(c.get("start", 0)), float(c.get("end", 0))
        except (TypeError, ValueError, AttributeError):
            return 0.0, 0.0

    indexed = list(enumerate(shorts))
    kept = []
    for idx, clip in sorted(indexed, key=lambda p: (-_score(p[1]), p[0])):
        s, e = _span(clip)
        clash = False
        for _, other in kept:
            os_, oe = _span(other)
            overlap = min(e, oe) - max(s, os_)
            shorter = max(1e-6, min(e - s, oe - os_))
            if overlap > 0 and overlap / shorter >= ratio:
                clash = True
                break
        if not clash:
            kept.append((idx, clip))
    return [c for _, c in sorted(kept, key=lambda p: p[0])]


def snap_clip_to_words(start, end, words, video_duration,
                       min_duration=15.0, max_duration=60.0,
                       search_window=1.5, max_lead=0.35, max_tail=0.45):
    """
    Snap Gemini-proposed clip boundaries onto real word boundaries plus a bit
    of the surrounding silence. LLMs are bad at millisecond arithmetic; the
    word-level timestamps are ground truth, so cuts land in pauses instead of
    mid-word.

    words: [{'w','s','e'}, ...] for the whole video, sorted by start.
    Returns (start, end); falls back to the input if no words are nearby or
    snapping cannot satisfy the duration bounds.
    """
    original = (round(float(start), 3), round(float(end), 3))
    if not words:
        return original

    starts = [float(w.get("s", 0)) for w in words]
    ends = [float(w.get("e", 0)) for w in words]

    # START: snap to the nearest word start, then lead into the silence before it.
    new_start = float(start)
    candidates = [s for s in starts if abs(s - new_start) <= search_window]
    if candidates:
        word_start = min(candidates, key=lambda s: abs(s - new_start))
        prev_ends = [e for e in ends if e <= word_start]
        if prev_ends:
            gap = max(0.0, word_start - max(prev_ends))
            lead = min(max_lead, gap / 2)
        else:
            lead = max_lead
        new_start = max(0.0, word_start - lead)

    # END: snap to the nearest word end, then trail into the silence after it.
    new_end = float(end)
    candidates = [e for e in ends if abs(e - new_end) <= search_window]
    if candidates:
        word_end = min(candidates, key=lambda e: abs(e - new_end))
        next_starts = [s for s in starts if s >= word_end]
        if next_starts:
            gap = max(0.0, min(next_starts) - word_end)
            tail = min(max_tail, gap / 2)
        else:
            tail = max_tail
        new_end = min(float(video_duration), word_end + tail)

    # Repair duration bounds while staying on word boundaries.
    if new_end - new_start < min_duration:
        target = new_start + min_duration
        later = sorted(e for e in ends if e >= target)
        if later and later[0] - new_start <= max_duration:
            new_end = min(float(video_duration), later[0] + 0.2)
        else:
            return original
    if new_end - new_start > max_duration:
        target = new_start + max_duration
        earlier = [e for e in ends if new_start < e <= target]
        new_end = (max(earlier) + 0.2) if earlier else target
        new_end = min(new_end, new_start + max_duration, float(video_duration))

    if new_end <= new_start or new_end - new_start < min_duration:
        return original
    return (round(new_start, 3), round(new_end, 3))


def max_window_score(scored_windows) -> float:
    """Highest score among Pass-1 scored transcript windows, or 0.0 if empty."""
    best = 0.0
    for w in scored_windows or []:
        try:
            s = float((w or {}).get("score") or 0.0)
            if s > best:
                best = s
        except (TypeError, ValueError, AttributeError):
            continue
    return best


def filter_by_min_score(shorts, min_score: float):
    """Keep only clips whose ``predicted_score >= min_score`` (preserving order)."""
    try:
        threshold = float(min_score or 0.0)
    except (TypeError, ValueError):
        threshold = 0.0
    if threshold <= 0:
        return list(shorts or [])
    out = []
    for clip in shorts or []:
        try:
            score = float((clip or {}).get("predicted_score") or 0.0)
        except (TypeError, ValueError, AttributeError):
            score = 0.0
        if score >= threshold:
            out.append(clip)
    return out


def shift_transcript(transcript: dict, offset_seconds: float, min_start: float = 0.0) -> dict:
    """Return a copy of ``transcript`` with all segment/word timestamps shifted
    by ``+offset_seconds``.

    Segments ending at or before ``min_start`` (in the chunk's own time, e.g.
    the 30s overlap already covered by the previous chunk) can be filtered out
    by passing ``min_start > 0`` when merging transcripts, or kept when
    ``min_start == 0``.
    """
    offset = float(offset_seconds or 0.0)
    floor = float(min_start or 0.0)
    segments_out = []
    for seg in (transcript or {}).get("segments") or []:
        try:
            s = float(seg.get("start", 0.0))
            e = float(seg.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if e <= floor:
            continue
        new_seg = dict(seg)
        new_seg["start"] = round(s + offset, 3)
        new_seg["end"] = round(e + offset, 3)
        if "words" in seg and isinstance(seg["words"], list):
            words_out = []
            for w in seg["words"]:
                if not isinstance(w, dict):
                    continue
                try:
                    ws = float(w.get("start", s))
                    we = float(w.get("end", e))
                except (TypeError, ValueError):
                    continue
                nw = dict(w)
                nw["start"] = round(ws + offset, 3)
                nw["end"] = round(we + offset, 3)
                words_out.append(nw)
            new_seg["words"] = words_out
        segments_out.append(new_seg)
    return {
        "language": (transcript or {}).get("language", "unknown"),
        "segments": segments_out,
    }


def dedupe_against_stream_spans(shorts, existing_stream_spans, chunk_stream_offset: float,
                                ratio: float = 0.25):
    """Drop clips from a new live chunk that overlap clips already emitted in
    earlier chunks (e.g. inside the 30s boundary overlap).

    ``existing_stream_spans`` is a list of ``(stream_start, stream_end)`` tuples
    in real stream seconds. Each candidate clip in ``shorts`` has chunk-local
    ``start``/``end`` which map to ``start + chunk_stream_offset`` in stream time.
    """
    offset = float(chunk_stream_offset or 0.0)
    kept = []
    for clip in shorts or []:
        try:
            cs = float(clip.get("start", 0.0)) + offset
            ce = float(clip.get("end", 0.0)) + offset
        except (TypeError, ValueError, AttributeError):
            continue
        clash = False
        for es, ee in existing_stream_spans or []:
            overlap = min(ce, float(ee)) - max(cs, float(es))
            shorter = max(1e-6, min(ce - cs, float(ee) - float(es)))
            if overlap > 0 and (overlap / shorter) >= ratio:
                clash = True
                break
        if not clash:
            kept.append(clip)
    return kept


_SPECULATIVE_MARKERS = (
    "acho", "opinião", "opiniao", "rumor", "boato", "teoria", "vazamento",
    "vazou", "especulação", "especulacao", "talvez", "pode ser", "chute",
    "previsão", "previsao", "supostamente", "hipótese", "hipotese",
    "rumour", "leak", "leaked", "theory", "speculation", "i think",
    "my opinion", "might be", "allegedly", "prediction", "guess",
)

_FACTUAL_ASSERTION_MARKERS = (
    "confirmado", "oficial", "é fato", "vai custar", "revelado", "anunciado",
    "100% confirmado", "confirmed", "official", "officially", "will cost",
    "is confirmed", "revealed",
)

_QUALIFIER_MARKERS = (
    "rumor", "boato", "teoria", "opinião", "opiniao", "será", "sera",
    "diz", "acha", "segundo", "vazamento", "?", "pode", "talvez",
    "rumour", "theory", "opinion", "leak", "might", "could", "claims",
    "thinks",
)

_DANGLING_START_PREFIXES = (
    "e aí", "e ai", "mas então", "mas entao", "então tipo", "entao tipo",
    "porque tipo", "and then", "but so", "so anyway",
)

_STOPWORDS = frozenset({
    "de", "da", "do", "das", "dos", "em", "no", "na", "nos", "nas", "para",
    "por", "com", "um", "uma", "o", "a", "os", "as", "e", "ou", "que", "se",
    "the", "of", "in", "on", "to", "for", "with", "a", "an", "and", "or",
    "is", "are", "video", "vídeo", "clip", "clipe", "podcast", "cortes",
    "live", "stream", "react", "reação", "reacao", "trailer", "gameplay",
})


def _clip_transcript_text(clip, transcript):
    """Extract spoken text inside ``[clip.start, clip.end]`` from ``transcript``."""
    if not transcript or not isinstance(transcript, dict):
        return ""
    try:
        c_start = float(clip.get("start", 0.0))
        c_end = float(clip.get("end", 0.0))
    except (TypeError, ValueError, AttributeError):
        return ""
    parts = []
    for seg in transcript.get("segments") or []:
        words = seg.get("words") or []
        if words:
            for w in words:
                try:
                    ws = float(w.get("start", w.get("s", 0.0)))
                    we = float(w.get("end", w.get("e", 0.0)))
                except (TypeError, ValueError):
                    continue
                if we > c_start and ws < c_end:
                    token = str(w.get("word", w.get("w", ""))).strip()
                    if token:
                        parts.append(token)
        else:
            try:
                ss = float(seg.get("start", 0.0))
                se = float(seg.get("end", 0.0))
            except (TypeError, ValueError):
                continue
            if se > c_start and ss < c_end:
                txt = str(seg.get("text") or "").strip()
                if txt:
                    parts.append(txt)
    return " ".join(parts).strip()


def evaluate_editorial_quality(clip, transcript=None, source_title=None):
    """Evaluate a candidate clip's editorial fidelity against the spoken transcript.

    Checks:
      1. ``sentence_complete``: whether the clip starts and ends on complete
         sentences rather than a dangling connective or mid-thought cut.
      2. ``rumor_qualified``: when the transcript discusses rumors, leaks,
         predictions or personal opinions, ensures the hook and title do not
         present them as unqualified facts.
      3. ``topic_relevant``: when ``source_title`` names a specific subject
         (e.g. "GTA 6"), flags clips that only discuss a generic tangent
         without mentioning the subject.
    """
    import re

    warnings = []
    spoken = _clip_transcript_text(clip or {}, transcript)
    spoken_lower = spoken.lower()
    hook = str((clip or {}).get("viral_hook_text") or "").strip()
    title = str((clip or {}).get("video_title_for_youtube_short") or "").strip()
    copy_lower = f"{hook} {title}".lower()

    # 1. Sentence boundary check
    sentence_complete = True
    if spoken:
        for prefix in _DANGLING_START_PREFIXES:
            if spoken_lower.startswith(prefix + " "):
                sentence_complete = False
                warnings.append("starts_on_dangling_connective")
                break
        if spoken.startswith("-") or spoken.startswith("..."):
            sentence_complete = False
            warnings.append("starts_mid_word_or_thought")
        if not re.search(r'[.!?…"”\'\)]$', spoken):
            # If the transcript carries punctuation elsewhere but not at the end,
            # flag it as a possible mid-sentence tail cut.
            if any(p in spoken for p in (".", "!", "?")):
                sentence_complete = False
                warnings.append("ends_without_terminal_punctuation")

    # 2. Rumor / opinion epistemic check
    rumor_or_opinion = any(m in spoken_lower for m in _SPECULATIVE_MARKERS)
    rumor_qualified = True
    if rumor_or_opinion and copy_lower.strip():
        has_factual_claim = any(m in copy_lower for m in _FACTUAL_ASSERTION_MARKERS)
        has_qualifier = any(q in copy_lower for q in _QUALIFIER_MARKERS)
        if has_factual_claim and not has_qualifier:
            rumor_qualified = False
            warnings.append("opinion_or_rumor_stated_as_fact")

    # 3. Topic relevance check against source_title
    topic_relevant = True
    if source_title and spoken:
        tokens = [
            t.lower()
            for t in re.findall(r"[A-Za-zÀ-ÿ0-9]+", str(source_title))
            if len(t) >= 3 and t.lower() not in _STOPWORDS
        ]
        if tokens:
            matched = [t for t in tokens if t in spoken_lower or t in copy_lower]
            if not matched:
                topic_relevant = False
                warnings.append("tangent_from_source_topic")

    needs_review = (not sentence_complete
                    or not rumor_qualified
                    or not topic_relevant)
    return {
        "sentence_complete": sentence_complete,
        "rumor_or_opinion_detected": rumor_or_opinion,
        "rumor_qualified": rumor_qualified,
        "topic_relevant": topic_relevant,
        "needs_editorial_review": needs_review,
        "warnings": warnings,
    }

