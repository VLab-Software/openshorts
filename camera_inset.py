"""Find the webcam inset in a screen recording, and frame the two apart.

The case: one source, the whole screen (a game, a desktop, an editor) with the
person composited into a corner. It is how OBS records and how every stream VOD
looks, and it is the case screencast_layout gets wrong. There the speaker band
is a large crop taken AROUND the face, which on a full-screen source means the
band is mostly more screen: measured on an Excel walkthrough, the output showed
the same spreadsheet twice, once whole and once enlarged.

What this needs instead is the inset's own rectangle, so the two bands can hold
genuinely different things — screen above, person below.

Finding it exactly is harder than it looks. The inset is not always a rectangle
(circles and clipped polygons are common), it has no reliable border, and on
gameplay the background moves as much as the person does, which rules out
temporal-variance tricks. What IS reliable is that the inset is anchored to a
corner and that a person detector fires inside it. So: locate the person, decide
which corner they are nearest, and grow a box from that corner until it covers
them with margin.
"""
import os

from ffmpeg_utils import blurred_backdrop

CORNER_MARGIN = 0.20   # a subject this far from an edge (as a fraction of the
                       # frame) still counts as anchored to it

# An inset subject is SMALL and OFF-CENTRE; a presenter filling the shot is
# neither. Requiring the detection to actually touch two edges was tried first
# and rejected: YOLO returns an upper-body box that stops at the chest, so a
# webcam pinned to the right edge measured 18% clear of the bottom and got
# thrown out. Measured on four real clips, these two properties separate the
# cases where "touching a corner" did not.
MAX_SUBJECT_HEIGHT = 0.35   # fraction of frame height

# Horizontal offset from centre, as a fraction of frame width. HORIZONTAL
# specifically: a composited camera is pinned to the left or right side, while a
# talking head is centred left-to-right even when their face sits high in the
# shot. Accepting offset on either axis let four talking heads through, all of
# them with a face near the top edge. Measured on those nine candidates the two
# groups do not overlap: real insets sat 0.37-0.43 from centre, the talking
# heads 0.01-0.12.
MIN_OFFSET = 0.18

# How much bigger than the detected head/upper body the inset is assumed to be.
# A webcam frames head and shoulders, and detectors return the head or the upper
# body, so the box has to grow to reach the inset's real edges.
INSET_PADDING = float(os.environ.get("INSET_PADDING", "1.45"))

# Guard rails as a fraction of frame height. Below the floor there is nothing
# worth showing; above the ceiling it stopped being an inset and the layout
# should not be used at all.
MIN_INSET_HEIGHT = 0.10
MAX_INSET_HEIGHT = 0.38

# An inset is pinned to the same pixels for the whole recording; a presenter
# walks about. Spread of the detected centres, as a fraction of frame width,
# above which this is a person in a room rather than a composited camera.
# Measured on the two false positives this rule was written for: a real inset
# moved 3-11px across samples, a presenter 316px.
MAX_CENTRE_SPREAD = 0.05

# Room kept above the detection, as a fraction of its height (see inset_box).
FACE_HEADROOM = 0.35
BODY_HEADROOM = 0.10

# A webcam window frames head and shoulders: the face is about a third of its
# width. Used for overlays that float mid-screen, where inset_box's corner
# anchoring does not apply and pushed the crop up onto the ceiling, and to grow
# a face detection to the inset in a corner.
OVERLAY_FACE_WIDTHS = 3.0


def nearest_corner(box, frame_w, frame_h):
    """Which corner the subject sits in: (horizontal, vertical) as strings.

    Returns e.g. ("left", "bottom"). A subject in the middle of the frame is
    reported by its nearest edges anyway; callers use `is_cornered` to reject.
    """
    cx = box[0] + box[2] / 2.0
    cy = box[1] + box[3] / 2.0
    return ("left" if cx < frame_w / 2 else "right",
            "top" if cy < frame_h / 2 else "bottom")


def is_cornered(box, frame_w, frame_h, margin=CORNER_MARGIN):
    """True when the subject looks like a webcam inset rather than the shot.

    Small and off-centre, not "touches two edges": see MAX_SUBJECT_HEIGHT.

    This is a sanity filter, not the decision. Whether the video is a screen
    recording with a camera in it at all is answered upstream by layout_picker;
    a game character standing in a corner would pass this test, and is kept out
    by the fact that nobody asked for this layout on that video.
    """
    x, y, w, h = box
    if h > frame_h * MAX_SUBJECT_HEIGHT:
        return False

    cx = (x + w / 2.0) / frame_w
    off_centre = abs(cx - 0.5) >= MIN_OFFSET

    near_edge = (x <= frame_w * margin or (x + w) >= frame_w * (1 - margin)
                 or y <= frame_h * margin or (y + h) >= frame_h * (1 - margin))
    return off_centre and near_edge


INSET_ASPECT = 16 / 9.0


def _place(x, y, w, h, new_w, new_h, frame_w, frame_h, horizontal, vertical):
    """Top-left corner of a new_w x new_h box grown around the detection."""
    # Anchor: keep the edge the subject is already near, grow the other way.
    if horizontal == "left":
        new_x = max(0, min(x - (new_w - w) / 2.0, frame_w - new_w))
        if x <= frame_w * CORNER_MARGIN:
            new_x = 0
    else:
        new_x = max(0, min(x + w + (new_w - w) / 2.0 - new_w, frame_w - new_w))
        if (x + w) >= frame_w * (1 - CORNER_MARGIN):
            new_x = frame_w - new_w

    # Vertical growth is biased upwards: detectors return the head or the chest,
    # and a portrait needs headroom, not more torso. Splitting the growth evenly
    # cropped the top of the head off on real clips.
    grow = new_h - h
    if vertical == "top":
        new_y = max(0, min(y - grow * 0.6, frame_h - new_h))
        if y <= frame_h * CORNER_MARGIN:
            new_y = 0
    else:
        # Pin to the bottom edge: the inset is there, and the detection sits
        # somewhere inside it rather than at its top.
        new_y = frame_h - new_h
        if (y + h) < frame_h * (1 - CORNER_MARGIN):
            new_y = max(0, min(y - grow * 0.6, frame_h - new_h))
    return new_x, new_y


def inset_box(box, frame_w, frame_h, padding=None, face=False):
    """Estimated inset rectangle (x, y, w, h) around a detected subject.

    Grown from the corner the subject is anchored to, so the box hugs the same
    edges the inset does instead of floating around the face.

    The height comes from assuming a 16:9 inset rather than from scaling the
    detection, because the detection is often a torso: YOLO returned a 246x86
    box for a webcam whose real rectangle was about 325x180, and padding that
    shape upwards still cut the head off. Deriving the height from the width
    and pinning the result to the corner reaches the top of the inset instead.

    ``face`` says the detection is a face box, not YOLO's upper body. A face is
    a third of a webcam's width (OVERLAY_FACE_WIDTHS), not 1/1.45 of it: grown
    by the body padding, a 128px face on a 720p Excel walkthrough gave a
    181px box pinned to the bottom edge that started 41px below the top of
    the face, and the enlarged band showed a forehead-less face (17-oct-2026).
    """
    if padding is None:
        padding = OVERLAY_FACE_WIDTHS if face else INSET_PADDING
    x, y, w, h = box
    horizontal, vertical = nearest_corner(box, frame_w, frame_h)

    new_w = min(frame_w, w * padding)
    # A face box is square, so tripling its height too would make a portrait
    # crop; the 16:9 assumption alone sets the height.
    new_h = new_w / INSET_ASPECT if face else max(h * padding, new_w / INSET_ASPECT)
    new_h = min(frame_h, new_h)
    # Never start below the head. A face box stops at the brows, so the hair
    # needs about a third of its height on top; YOLO's box already starts at
    # the top of the head.
    top_limit = max(0.0, y - h * (FACE_HEADROOM if face else BODY_HEADROOM))

    new_x, new_y = _place(x, y, w, h, new_w, new_h, frame_w, frame_h,
                          horizontal, vertical)
    if new_y > top_limit:
        if new_y + new_h >= frame_h - 1:
            # Pinned to the bottom edge: grow upwards (and keep 16:9) rather
            # than slide off the edge the inset sits on.
            new_h = frame_h - top_limit
            new_w = min(frame_w, max(new_w, new_h * INSET_ASPECT))
            new_x, _ = _place(x, y, w, h, new_w, new_h, frame_w, frame_h,
                              horizontal, vertical)
        new_y = top_limit

    return (int(round(new_x)), int(round(new_y)),
            int(round(new_w)), int(round(new_h)))


def usable(box, frame_h, min_ratio=MIN_INSET_HEIGHT, max_ratio=MAX_INSET_HEIGHT):
    """Whether an inset of this size is worth building a layout around."""
    return min_ratio * frame_h <= box[3] <= max_ratio * frame_h


def detect(video_path, samples=10):
    """Median inset rectangle across sampled frames, or None.

    None means "no usable inset here", which callers must treat as "use another
    layout" rather than as an error.
    """
    import cv2
    import numpy as np
    import main as m
    import screencast_layout

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if total <= 0 or not frame_w:
        cap.release()
        return None

    boxes = []
    try:
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * total / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            # Faces first, body as fallback: a webcam inset is small, and on a
            # 1080p source the face inside it is often too few pixels for
            # BlazeFace even at full resolution, while YOLO still finds the
            # person.
            faces = screencast_layout.detect_faces_full_res(frame)
            if faces:
                box = max(faces, key=lambda c: c['score'])['box']
            else:
                box = m.detect_person_yolo(frame)
            if not box:
                continue
            if not is_cornered(box, frame_w, frame_h):
                continue
            boxes.append(inset_box(box, frame_w, frame_h, face=bool(faces)))
    finally:
        cap.release()

    if len(boxes) < max(3, samples // 3):
        return None

    return stable_box(boxes, frame_w, frame_h)


def stable_box(boxes, frame_w, frame_h, min_agree=3):
    """Median of the inset boxes that agree with each other, or None."""
    import numpy as np

    arr = np.array(boxes, dtype=float)

    # Stability gate. Without it, a presenter standing in front of a screen and
    # an ordinary talking head both come back as "insets" and the layout puts a
    # zoom of someone's face under a copy of the same shot.
    #
    # Measured against the plain min-to-max range first, which was far too
    # brittle: one sample landing on a face elsewhere in the frame killed three
    # genuine insets. So cluster around the median instead and require most
    # detections to agree, which tolerates the odd stray without tolerating a
    # subject that actually moves.
    centres_x = arr[:, 0] + arr[:, 2] / 2.0
    centres_y = arr[:, 1] + arr[:, 3] / 2.0
    mid_x, mid_y = np.median(centres_x), np.median(centres_y)
    tolerance = frame_w * MAX_CENTRE_SPREAD
    close = ((np.abs(centres_x - mid_x) <= tolerance)
             & (np.abs(centres_y - mid_y) <= tolerance))
    if close.sum() < max(min_agree, 0.6 * len(boxes)):
        return None

    arr = arr[close]
    median = tuple(int(v) for v in np.median(arr, axis=0))
    if not usable(median, frame_h):
        return None
    return median


def overlay_box(box, frame_w, frame_h, widen=OVERLAY_FACE_WIDTHS):
    """A 16:9 box centred on a detected face (or upper body), clamped."""
    x, y, w, h = box
    new_w = min(frame_w, w * widen)
    new_h = min(frame_h, new_w / INSET_ASPECT)
    cx, cy = x + w / 2.0, y + h / 2.0
    new_x = max(0, min(cx - new_w / 2.0, frame_w - new_w))
    new_y = max(0, min(cy - new_h * 0.45, frame_h - new_h))
    return (int(round(new_x)), int(round(new_y)),
            int(round(new_w)), int(round(new_h)))


def detect_in_scene(video_path, start_f, end_f, samples=5):
    """A presenter window laid over the screen in this one scene, or None.

    ``detect`` is the clip-wide check and insists on a corner, because nothing
    upstream of it says the frame is a screen. Here the shot check already did
    (this runs only on scenes Gemini called "screen"), so a small face that
    stays put is the presenter's camera wherever it floats: a QuickTime or Loom
    window over a document sits mid-left, not in a corner, and the screen-only
    layout shrank both into a 1080px-wide strip (Ty Myers tutorial,
    6-oct-2026). What still rules a subject out: being big (that is the shot,
    not an overlay) or moving between samples.

    The caller only asks where the shot check said a live presenter camera is
    laid over the screen (presenter_cam). That gate is what keeps game
    characters, photos on slides and cover art out; the YOLO body fallback
    stays because BlazeFace misses the webcam face in about half the samples
    of a dim or small window (ticker_VUqnFqEHy2E), and without it a real
    presenter window is lost.
    """
    import cv2
    import numpy as np
    import main as m
    import screencast_layout

    if end_f - 1 < start_f:
        return None
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    boxes = []
    try:
        for f_idx in np.linspace(start_f, end_f - 1, samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
            ok, frame = cap.read()
            if not ok:
                continue
            faces = screencast_layout.detect_faces_full_res(frame)
            if faces:
                box = max(faces, key=lambda c: c['score'])['box']
                widen = OVERLAY_FACE_WIDTHS
            else:
                box = m.detect_person_yolo(frame)
                widen = INSET_PADDING
            if not box or box[3] > frame_h * MAX_SUBJECT_HEIGHT:
                continue
            boxes.append(overlay_box(box, frame_w, frame_h, widen))
    finally:
        cap.release()
    need = samples // 2 + 1
    if len(boxes) < need:
        return None
    return stable_box(boxes, frame_w, frame_h, min_agree=need)


def _centre_inside(det, box, slack):
    cx = det[0] + det[2] / 2.0
    cy = det[1] + det[3] / 2.0
    x, y, w, h = box
    return (x - slack <= cx <= x + w + slack) and (y - slack <= cy <= y + h + slack)


def present_in_scene(video_path, box, start_f, end_f, samples=3):
    """Whether the webcam really sits in ``box`` during this scene.

    ``detect`` finds the box once per clip, from samples spread over every
    scene, and a clip cut from a tutorial mixes shots: the bubble is in the
    corner during the screen recording and gone in the editor shot next to it.
    Before this check every screen scene of a clip with an inset anywhere got
    the INSET layout, and on a scene without the camera the bottom band was an
    enlarged crop of a toolbar (measured on a Canva + QuickTime tutorial,
    6-oct-2026). A face or a person whose centre falls in the box in most
    samples is the answer.
    """
    import cv2
    import numpy as np
    import main as m
    import screencast_layout

    if end_f - 1 < start_f:
        return False
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return False
    slack = max(box[2], box[3]) * 0.1
    hits = 0
    try:
        for f_idx in np.linspace(start_f, end_f - 1, samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(round(f_idx)))
            ok, frame = cap.read()
            if not ok:
                continue
            dets = [c['box'] for c in screencast_layout.detect_faces_full_res(frame)]
            if not any(_centre_inside(d, box, slack) for d in dets):
                person = m.detect_person_yolo(frame)
                dets = [person] if person else []
            if any(_centre_inside(d, box, slack) for d in dets):
                hits += 1
    finally:
        cap.release()
    return hits * 2 > samples


MAX_CAMERA_RATIO = 0.40
MIN_SCREEN_RATIO = 0.28
DEFAULT_CAPTION_BAND_RATIO = float(os.environ.get("CAPTION_BAND_RATIO", "0.08"))
TOP_SAFE_RATIO = 0.05
BOTTOM_SAFE_RATIO = 0.12
MIN_CONFIDENCE = float(os.environ.get("INSET_MIN_CONFIDENCE", "0.55"))
ENABLED = os.environ.get("CAM_SCREEN_LAYOUT", os.environ.get("INSET_LAYOUT", "0")) == "1"


def score_inset_samples(boxes, frame_w, frame_h, total_samples=None, min_agree=3):
    """Evaluate sampled inset candidates and return confidence + diagnostics.

    Pure function so tests can verify confidence scoring, layout changes and
    fallback triggers without decoding video.

    Returns a dict:
      - ``box``: median (x, y, w, h) tuple, or None when unusable/unstable
      - ``confidence``: float in [0.0, 1.0]
      - ``presence_ratio``: share of sampled frames with a cornered candidate
      - ``agreement_ratio``: share of candidates matching the dominant position
      - ``position_changes``: number of quadrant shifts across samples
      - ``fallback_recommended``: True when uncertain or layout is unstable
    """
    import numpy as np

    total = max(1, int(total_samples if total_samples is not None else len(boxes) or 1))
    presence_ratio = len(boxes) / float(total)
    if not boxes:
        return {
            "box": None,
            "confidence": 0.0,
            "presence_ratio": 0.0,
            "agreement_ratio": 0.0,
            "position_changes": 0,
            "fallback_recommended": True,
        }

    quadrants = [nearest_corner(b, frame_w, frame_h) for b in boxes]
    position_changes = sum(
        1 for i in range(1, len(quadrants)) if quadrants[i] != quadrants[i - 1]
    )

    arr = np.array(boxes, dtype=float)
    centres_x = arr[:, 0] + arr[:, 2] / 2.0
    centres_y = arr[:, 1] + arr[:, 3] / 2.0
    mid_x, mid_y = np.median(centres_x), np.median(centres_y)
    tolerance = frame_w * MAX_CENTRE_SPREAD
    close = ((np.abs(centres_x - mid_x) <= tolerance)
             & (np.abs(centres_y - mid_y) <= tolerance))
    agreement_ratio = float(close.sum()) / float(len(boxes))

    median = stable_box(boxes, frame_w, frame_h, min_agree=min_agree)
    if median is None:
        conf = round(min(0.45, presence_ratio * agreement_ratio * 0.6), 3)
        return {
            "box": None,
            "confidence": conf,
            "presence_ratio": round(presence_ratio, 3),
            "agreement_ratio": round(agreement_ratio, 3),
            "position_changes": position_changes,
            "fallback_recommended": True,
        }

    # Combine presence across the clip, spatial stability, and quadrant consistency.
    change_penalty = min(0.4, position_changes * 0.15)
    raw_conf = (0.45 * min(1.0, presence_ratio / 0.7)
                + 0.45 * agreement_ratio
                + 0.10 * (1.0 if position_changes == 0 else 0.0)
                - change_penalty)
    conf = round(max(0.0, min(1.0, raw_conf)), 3)
    return {
        "box": median,
        "confidence": conf,
        "presence_ratio": round(presence_ratio, 3),
        "agreement_ratio": round(agreement_ratio, 3),
        "position_changes": position_changes,
        "fallback_recommended": conf < MIN_CONFIDENCE or position_changes > 1,
    }


def detect_with_confidence(video_path, samples=10):
    """Inspect distributed samples of ``video_path`` and return inset box +
    confidence report (see ``score_inset_samples``)."""
    import cv2
    import main as m
    import screencast_layout

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return score_inset_samples([], 0, 0, total_samples=samples)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if total <= 0 or not frame_w:
        cap.release()
        return score_inset_samples([], frame_w, frame_h, total_samples=samples)

    boxes = []
    valid_samples = 0
    try:
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i * total / samples))
            ok, frame = cap.read()
            if not ok:
                continue
            valid_samples += 1
            faces = screencast_layout.detect_faces_full_res(frame)
            if faces:
                box = max(faces, key=lambda c: c['score'])['box']
            else:
                box = m.detect_person_yolo(frame)
            if not box:
                continue
            if not is_cornered(box, frame_w, frame_h):
                continue
            boxes.append(inset_box(box, frame_w, frame_h, face=bool(faces)))
    finally:
        cap.release()

    need = max(3, samples // 3)
    if len(boxes) < need:
        return score_inset_samples([], frame_w, frame_h,
                                   total_samples=max(1, valid_samples))
    return score_inset_samples(boxes, frame_w, frame_h,
                               total_samples=max(1, valid_samples),
                               min_agree=need)


def smooth_scene_insets(scenes, scene_boxes, fps, max_gap_seconds=1.5):
    """Smooth per-scene inset presence so brief detector misses do not cause
    jarring layout jumps, while genuine camera moves or absences fall back.

    ``scenes``: list of (start, end) frame timecodes or frame-index pairs.
    ``scene_boxes``: dict keyed by ``start_f`` (as in ``reframe_v2``) or
    ``scene_idx``, mapping to ``(x, y, w, h) | dict | None``.
    Returns a new dict with the same key convention, with short gaps bridged
    and agreeing neighbour boxes stabilized to a single median box so the
    camera crop does not jitter across scene boundaries.
    """
    import numpy as np

    if not scenes or not scene_boxes:
        return dict(scene_boxes or {})

    def _frames(s, e):
        sf = s.get_frames() if hasattr(s, "get_frames") else int(s)
        ef = e.get_frames() if hasattr(e, "get_frames") else int(e)
        return sf, ef

    frame_pairs = [_frames(s, e) for s, e in scenes]
    start_frames = [sf for sf, _ in frame_pairs]
    # Determine whether caller keyed scene_boxes by start_f or by scene index.
    non_zero_starts = [sf for sf in start_frames if sf != 0]
    keyed_by_start = any(sf in scene_boxes for sf in non_zero_starts)

    def _key(idx):
        return start_frames[idx] if keyed_by_start else idx

    by_idx = {}
    for idx in range(len(scenes)):
        val = scene_boxes.get(_key(idx))
        if val is None and keyed_by_start:
            val = scene_boxes.get(idx)
        if val is not None:
            by_idx[idx] = val

    def _box_tuple(val):
        if isinstance(val, dict):
            return tuple(val.get("camera") or ())
        return tuple(val) if val else ()

    def _same_spot(a, b):
        ta, tb = _box_tuple(a), _box_tuple(b)
        if len(ta) < 4 or len(tb) < 4:
            return False
        tol = max(ta[2], tb[2], 20) * 0.25
        return abs(ta[0] - tb[0]) <= tol and abs(ta[1] - tb[1]) <= tol

    fps = float(fps or 30.0)
    for i in range(1, len(scenes) - 1):
        if i in by_idx:
            continue
        prev_box = by_idx.get(i - 1)
        next_box = by_idx.get(i + 1)
        if not prev_box or not next_box:
            continue
        sf, ef = frame_pairs[i]
        dur = (ef - sf) / fps
        if dur <= max_gap_seconds and _same_spot(prev_box, next_box):
            by_idx[i] = prev_box

    # Stabilize consecutive agreeing tuple boxes to their median so scene cuts
    # do not shift the webcam crop by a few pixels.
    idx_list = sorted(by_idx.keys())
    i = 0
    while i < len(idx_list):
        run = [idx_list[i]]
        j = i + 1
        while j < len(idx_list) and _same_spot(by_idx[idx_list[j - 1]], by_idx[idx_list[j]]):
            run.append(idx_list[j])
            j += 1
        if len(run) > 1 and all(not isinstance(by_idx[k], dict) for k in run):
            arr = np.array([_box_tuple(by_idx[k]) for k in run], dtype=float)
            med = tuple(int(round(v)) for v in np.median(arr, axis=0))
            for k in run:
                by_idx[k] = med
        i = j

    return {_key(idx): val for idx, val in by_idx.items()}


def _unpack_spec(box, screen_box=None):
    """Accept either a bare (x, y, w, h) camera box or a dict
    ``{"camera": (cx, cy, cw, ch), "screen": (sx, sy, sw, sh)}``."""
    if isinstance(box, dict):
        cam = box.get("camera") or box.get("box")
        scr = screen_box if screen_box is not None else box.get("screen")
        return cam, scr
    return box, screen_box


def cam_screen_geometry(orig_w, orig_h, out_w, out_h, box, camera_ratio=None,
                        screen_box=None, caption_band_ratio=None):
    """Compute exact, distortion-free geometry for the 9:16 Camera + Screen layout:
      - Streamer camera on TOP
      - Central subtitle band in the middle (seam)
      - Game / screen on BOTTOM

    Preserves the aspect ratio of both source regions without stretching faces,
    HUD or interface elements, and keeps the screen band above ``MIN_SCREEN_RATIO``
    so the central subtitle band never makes the screen illegible.
    """
    cam_box, screen_box = _unpack_spec(box, screen_box)
    box_w = max(2, int(round(cam_box[2])))
    box_h = max(2, int(round(cam_box[3])))

    if camera_ratio is not None:
        cam_h = int(out_h * camera_ratio)
    else:
        cam_h = int(round(out_w * box_h / float(box_w)))
    cam_h = min(cam_h, int(out_h * MAX_CAMERA_RATIO))
    cam_h = max(2, cam_h - (cam_h % 2))

    # Widen (or heighten) the camera crop in source coordinates so its aspect
    # matches out_w / cam_h. If clamping against the source frame stops it from
    # reaching the target aspect, adjust the other dimension so the scale stays
    # 100% uniform (never stretching the face).
    target_aspect = out_w / float(cam_h)
    x, y, w, h = [int(round(v)) for v in cam_box[:4]]
    if w / float(h) < target_aspect:
        want_w = min(orig_w, int(round(h * target_aspect)))
        x = int(round(x + w / 2.0 - want_w / 2.0))
        w = want_w
    else:
        want_h = min(orig_h, int(round(w / target_aspect)))
        y = int(round(y + h / 2.0 - want_h / 2.0))
        h = want_h
    x = max(0, min(x, orig_w - w))
    y = max(0, min(y, orig_h - h))

    w = max(2, w - (w % 2))
    h = max(2, h - (h % 2))
    x = max(0, min(x - (x % 2), orig_w - w))
    y = max(0, min(y - (y % 2), orig_h - h))

    # If source clamping still left an aspect mismatch (>2%), adjust cam_h to
    # match the actual clamped source crop aspect so faces never stretch.
    actual_aspect = w / float(h)
    if abs(actual_aspect - target_aspect) > 0.02:
        adjusted_cam_h = int(round(out_w / actual_aspect))
        adjusted_cam_h -= adjusted_cam_h % 2
        if 2 <= adjusted_cam_h <= int(out_h * MAX_CAMERA_RATIO):
            cam_h = adjusted_cam_h
        else:
            h = max(2, min(orig_h, int(round(w / target_aspect))))
            h -= h % 2
            y = max(0, min(y, orig_h - h))
            y -= y % 2

    # Screen region: full source frame by default, or user-selected sub-region.
    crop_screen = False
    if screen_box is not None:
        sx, sy, sw, sh = [int(round(v)) for v in screen_box[:4]]
        sw = max(2, min(sw, orig_w))
        sh = max(2, min(sh, orig_h))
        sx = max(0, min(sx, orig_w - sw))
        sy = max(0, min(sy, orig_h - sh))
        sw = max(2, sw - (sw % 2))
        sh = max(2, sh - (sh % 2))
        sx -= sx % 2
        sy -= sy % 2
        crop_screen = (sx != 0 or sy != 0 or sw != orig_w or sh != orig_h)
    else:
        sx, sy, sw, sh = 0, 0, orig_w, orig_h

    screen_h = int(round(out_w * sh / float(sw)))
    screen_h -= screen_h % 2
    min_screen_h = int(out_h * MIN_SCREEN_RATIO)
    min_screen_h -= min_screen_h % 2

    # Ensure screen is never squeezed below readability or past available height.
    max_screen_h = max(2, out_h - cam_h - 2)
    if screen_h > max_screen_h:
        screen_h = max_screen_h - (max_screen_h % 2)
        if crop_screen:
            # Adjust source height to preserve aspect ratio without stretching.
            sh = max(2, min(orig_h, int(round(sw * screen_h / float(out_w)))))
            sh -= sh % 2
            sy = max(0, min(sy, orig_h - sh))
            sy -= sy % 2
    elif crop_screen and screen_h < min_screen_h and min_screen_h <= max_screen_h:
        # Widen/heighten crop to give the screen at least min_screen_h without stretch.
        want_sh = min(orig_h, int(round(sw * min_screen_h / float(out_w))))
        want_sh -= want_sh % 2
        if want_sh > sh:
            sy = max(0, min(int(round(sy + sh / 2.0 - want_sh / 2.0)), orig_h - want_sh))
            sy -= sy % 2
            sh = want_sh
            screen_h = int(round(out_w * sh / float(sw)))
            screen_h -= screen_h % 2

    screen_h = max(2, min(screen_h, out_h - cam_h - 2))
    screen_h -= screen_h % 2

    leftover = max(0, out_h - cam_h - screen_h)
    want_band_ratio = (DEFAULT_CAPTION_BAND_RATIO
                       if caption_band_ratio is None else float(caption_band_ratio))
    want_band_h = max(0, int(round(out_h * want_band_ratio)))
    want_band_h -= want_band_h % 2
    # Reserve the central caption band from the leftover vertical space so it
    # never shrinks the screen or camera, while leaving room for top/bottom safe margins.
    band_h = min(want_band_h, leftover)
    band_h -= band_h % 2

    outer_filler = max(0, leftover - band_h)
    # Bias outer filler so top clears status bar (~35% of outer filler) and
    # bottom clears platform caption/account chrome (~65% of outer filler).
    cam_y = int(round(outer_filler * 0.35))
    cam_y -= cam_y % 2
    screen_y = cam_y + cam_h + band_h
    screen_y -= screen_y % 2
    if screen_y + screen_h > out_h:
        screen_y = max(cam_y + cam_h, out_h - screen_h)
        screen_y -= screen_y % 2

    seam_top = cam_y + cam_h
    seam_bottom = screen_y
    seam_centre = (seam_top + seam_bottom) / 2.0 / float(out_h)

    return {
        "cam_src": (x, y, w, h),
        "cam_dst": (0, cam_y, out_w, cam_h),
        "screen_src": (sx, sy, sw, sh),
        "screen_dst": (0, screen_y, out_w, screen_h),
        "crop_screen": crop_screen,
        "caption_band_px": (seam_top, seam_bottom),
        "caption_band_y": (seam_top, seam_bottom),
        "seam_y_fraction": round(seam_centre, 4),
        "content_band": (
            round(cam_y / float(out_h), 3),
            round((screen_y + screen_h) / float(out_h), 3),
        ),
    }


def inset_filtergraph(orig_w, orig_h, out_w, out_h, box, camera_ratio=None,
                      screen_box=None, caption_band_ratio=None):
    """Streamer camera on top, central caption band, game/screen below in 9:16.

    The screen keeps its whole width by default (or the selected ``screen_box``
    region when specified), so a game HUD, trailer detail or desktop puts what
    matters in full view without horizontal cropping. The camera inset is placed
    at the top at its own aspect ratio, with a central band between camera and
    screen where burned subtitles sit without covering either the face or the
    game/screen content.
    """
    geom = cam_screen_geometry(
        orig_w, orig_h, out_w, out_h, box,
        camera_ratio=camera_ratio,
        screen_box=screen_box,
        caption_band_ratio=caption_band_ratio,
    )
    x, y, w, h = geom["cam_src"]
    _cx, cam_y, _cw, cam_h = geom["cam_dst"]
    sx, sy, sw, sh = geom["screen_src"]
    _sx, screen_y, _sw, screen_h = geom["screen_dst"]

    if geom["crop_screen"]:
        screen_chain = f"crop=w={sw}:h={sh}:x={sx}:y={sy},scale={out_w}:{screen_h}"
    else:
        screen_chain = f"scale={out_w}:{screen_h}"

    return (
        f"[0:v]split=3[bga][sa][ca];"
        # Blurred backdrop so the leftover strips and central caption band are
        # not black bars. Scaled by HEIGHT: scaling a 16:9 source to 1080 wide
        # gives 608 tall, and there is no 1920-tall crop to take out of that.
        f"[bga]{blurred_backdrop(out_w, out_h, 14)}[bg];"
        f"[sa]{screen_chain}[screen];"
        f"[ca]crop=w={w}:h={h}:x={x}:y={y},scale={out_w}:{cam_h}[cam];"
        f"[bg][cam]overlay=x=0:y={cam_y}[withcam];"
        f"[withcam][screen]overlay=x=0:y={screen_y},"
        f"setsar=1[v]"
    )


cam_screen_filtergraph = inset_filtergraph
