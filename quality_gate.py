"""Visual and editorial quality gate for reframed vertical clips.

Implements the P0-P4 quality checks and the 4-way layout comparison protocol
described in ``docs/layout-camera-e-tela.md`` and ``docs/estudo-edicao-gta6.md``:

1. **Aspect-ratio integrity per layer**: container dimensions (1080x1920) and
   ``SAR=1`` do not detect deformation already baked into a cropped/scaled
   layer. ``check_layer_aspect_ratios`` parses the FFmpeg filtergraph into
   source -> destination layer rectangles and verifies every non-blurred
   foreground layer preserves its source aspect ratio.
2. **Introduced vs. source panel duplication**: ``check_panel_duplication``
   flags overlapping source crops (e.g. when a 2-way split on a 3-person
   podcast mosaic duplicates the middle participant in both halves) and
   distinguishes them from duplicate panels already present in the source.
3. **Border face cuts and participant exclusion**: ``check_border_face_cuts``
   checks whether any participant face is sliced by a crop boundary or left
   out of all visible panels.
4. **Screen elements (QR codes, corner banners/logos)**:
   ``detect_screen_elements`` identifies QR codes and static corner overlays
   and marks them ``preserve_and_review`` rather than blindly cropping them
   out and risking loss of essential context or faces.
5. **Caption & hook safe areas**: ``check_caption_and_hook_safe_area`` verifies
   that the active subtitle band (central seam or bottom) and hook stay clear
   of faces, HUD and platform UI margins.
6. **Mandatory human review gate**: ``evaluate_clip_render`` aggregates visual
   and editorial checks. A high ``predicted_score`` (e.g. 84) or valid
   container resolution never auto-approves a clip for publication: clips start
   in ``review_status="pending_review"`` until explicitly approved by a human.
7. **Candidate layout comparison**: ``compare_layout_candidates`` compares
   (A) fixed centre crop, (B) full frame with blurred padding, (C) legacy auto,
   and (D) the proposed layout-aware composition on the same scene metrics.
"""
import gpu_render
import hook_placement
from clip_selection import evaluate_editorial_quality


def _iou(box_a, box_b):
    """Intersection over union of two [x, y, w, h] rectangles."""
    ax, ay, aw, ah = [float(v) for v in box_a[:4]]
    bx, by, bw, bh = [float(v) for v in box_b[:4]]
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def foreground_layers(graph, orig_w, orig_h, out_w=1080, out_h=1920):
    """Return non-blurred foreground layers [{'src': [x,y,w,h], 'dst': [x,y,w,h]}]
    parsed from ``graph``."""
    layers = gpu_render.layers_from_graph(graph, orig_w, orig_h, out_w, out_h)
    return [layer for layer in layers if not layer.get("blur")]


def check_layer_aspect_ratios(graph, orig_w, orig_h, out_w=1080, out_h=1920,
                              max_error=0.02):
    """Verify that every non-blurred layer in ``graph`` preserves its source
    aspect ratio when scaled into the output canvas.

    A container check (1080x1920, SAR=1) misses deformation baked into a
    stretched layer. This check catches any discrepancy between
    ``src_w / src_h`` and ``dst_w / dst_h``.
    """
    fg = foreground_layers(graph, orig_w, orig_h, out_w, out_h)
    deformed = []
    max_observed = 0.0
    for idx, layer in enumerate(fg):
        sx, sy, sw, sh = layer["src"]
        dx, dy, dw, dh = layer["dst"]
        if sh <= 0 or dh <= 0:
            continue
        src_aspect = float(sw) / float(sh)
        dst_aspect = float(dw) / float(dh)
        rel_err = abs(src_aspect - dst_aspect) / max(src_aspect, 1e-6)
        max_observed = max(max_observed, rel_err)
        if rel_err > max_error:
            deformed.append({
                "layer_index": idx,
                "src": layer["src"],
                "dst": layer["dst"],
                "src_aspect": round(src_aspect, 4),
                "dst_aspect": round(dst_aspect, 4),
                "relative_error": round(rel_err, 4),
            })
    return {
        "ok": len(deformed) == 0,
        "max_relative_error": round(max_observed, 4),
        "deformed_layers": deformed,
    }


def check_panel_duplication(layers, frame=None, max_iou=0.08,
                            max_similarity=0.96):
    """Detect whether a multi-panel composition duplicates a person/region.

    Distinguishes two cases:
      - ``introduced_duplication``: two foreground panels crop overlapping
        regions of the source frame (e.g. a 2-way split whose top and bottom
        windows both include the middle speaker), EXCEPT when one layer is
        intentionally the full uncropped screen and the other is the enlarged
        corner webcam inset (Camera + Screen layout).
      - ``source_duplication``: non-overlapping source regions that contain
        near-identical visual content in ``frame`` (duplication already in the
        source video; should be preserved and flagged for manual review rather
        than blindly cropped).
    """
    fg = [l for l in (layers or []) if not l.get("blur")]
    introduced = []
    source_dup = []

    for i, la in enumerate(fg):
        sa = la["src"]
        area_a = float(sa[2]) * float(sa[3])
        for j in range(i + 1, len(fg)):
            lb = fg[j]
            sb = lb["src"]
            area_b = float(sb[2]) * float(sb[3])
            # In Camera + Screen (INSET), one layer is the small corner webcam
            # (< 25% of the other layer's area) and the other is the full
            # screen. That containment is intentional, not a split duplication.
            ratio = min(area_a, area_b) / max(area_a, area_b, 1.0)
            if ratio < 0.25:
                continue
            iou = _iou(sa, sb)
            # Also check if either crop swallows the horizontal centre of the other
            ca_x = sa[0] + sa[2] / 2.0
            cb_x = sb[0] + sb[2] / 2.0
            swallows_centre = (
                (sa[0] <= cb_x <= sa[0] + sa[2] and sb[0] <= ca_x <= sb[0] + sb[2])
            )
            if iou > max_iou or swallows_centre:
                introduced.append({
                    "layers": (i, j),
                    "iou": round(iou, 4),
                    "src_a": sa,
                    "src_b": sb,
                })

    if (not introduced and frame is not None
            and hasattr(frame, "shape") and len(frame.shape) >= 2 and len(fg) >= 2):
        import cv2
        import numpy as np
        fh, fw = frame.shape[:2]
        patches = []
        for idx, layer in enumerate(fg):
            sx, sy, sw, sh = [int(round(v)) for v in layer["src"]]
            crop = frame[max(0, sy):min(fh, sy + sh), max(0, sx):min(fw, sx + sw)]
            if crop.size == 0:
                continue
            small = cv2.resize(crop, (32, 32)).astype(float)
            norm = float(np.linalg.norm(small))
            patches.append((idx, small / norm if norm > 0 else small))
        for k, (ia, pa) in enumerate(patches):
            for ib, pb in patches[k + 1:]:
                sim = float(np.sum(pa * pb))
                if sim >= max_similarity:
                    source_dup.append({"layers": (ia, ib), "similarity": round(sim, 4)})

    return {
        "ok": len(introduced) == 0,
        "introduced_duplication": introduced,
        "source_duplication": source_dup,
        "needs_review": bool(introduced or source_dup),
    }


def check_border_face_cuts(face_boxes, visible_src_boxes, orig_w, orig_h,
                           min_face_width_ratio=0.045):
    """Check whether any participant face in ``face_boxes`` ([x, y, w, h] in
    source pixels) is sliced by a crop boundary or excluded from all visible
    source crops ``visible_src_boxes``.
    """
    cut_faces = []
    excluded_faces = []
    duplicated_faces = []

    for f_idx, fbox in enumerate(face_boxes or []):
        if not fbox or len(fbox) < 4:
            continue
        fx, fy, fw, fh = [float(v) for v in fbox[:4]]
        if fw < orig_w * min_face_width_ratio or fw <= 0 or fh <= 0:
            continue
        face_area = fw * fh
        full_in_count = 0
        partial_in_crops = []

        for c_idx, cbox in enumerate(visible_src_boxes or []):
            cx, cy, cw, ch = [float(v) for v in cbox[:4]]
            # Skip full-frame screen layers when checking if a small webcam or
            # participant panel cut a face, unless it is the only layer.
            if len(visible_src_boxes) > 1 and cw >= orig_w * 0.98 and ch >= orig_h * 0.98:
                continue
            ix = max(0.0, min(fx + fw, cx + cw) - max(fx, cx))
            iy = max(0.0, min(fy + fh, cy + ch) - max(fy, cy))
            covered = (ix * iy) / face_area
            if covered >= 0.92:
                full_in_count += 1
            elif covered > 0.0:
                partial_in_crops.append((c_idx, round(covered, 3)))

        if full_in_count == 0 and partial_in_crops:
            cut_faces.append({
                "face_index": f_idx,
                "box": [round(fx, 1), round(fy, 1), round(fw, 1), round(fh, 1)],
                "partial_crops": partial_in_crops,
            })
        elif full_in_count == 0 and not partial_in_crops and visible_src_boxes:
            # Check against all boxes including full-frame if needed
            any_full = False
            for cx, cy, cw, ch in visible_src_boxes:
                ix = max(0.0, min(fx + fw, cx + cw) - max(fx, cx))
                iy = max(0.0, min(fy + fh, cy + ch) - max(fy, cy))
                if (ix * iy) / face_area >= 0.92:
                    any_full = True
                    break
            if not any_full:
                excluded_faces.append({
                    "face_index": f_idx,
                    "box": [round(fx, 1), round(fy, 1), round(fw, 1), round(fh, 1)],
                })
        elif full_in_count >= 2:
            duplicated_faces.append({
                "face_index": f_idx,
                "box": [round(fx, 1), round(fy, 1), round(fw, 1), round(fh, 1)],
                "panel_count": full_in_count,
            })

    return {
        "ok": not cut_faces and not excluded_faces and not duplicated_faces,
        "cut_faces": cut_faces,
        "excluded_faces": excluded_faces,
        "duplicated_faces": duplicated_faces,
    }


def detect_screen_elements(frame):
    """Identify QR codes and static corner banners/logos in ``frame``.

    Never assumes an element is disposable: returns
    ``action="preserve_and_review"`` so the pipeline never crops over a face or
    destroys sponsor/context elements without human confirmation.
    """
    elements = []
    if frame is None or not hasattr(frame, "shape") or len(frame.shape) < 2:
        return elements

    try:
        import cv2
        import numpy as np

        h, w = frame.shape[:2]
        detector = cv2.QRCodeDetector()
        ok, points = detector.detect(frame)
        if ok and points is not None and len(points) > 0:
            pts = np.array(points).reshape(-1, 2)
            x0, y0 = int(np.min(pts[:, 0])), int(np.min(pts[:, 1]))
            x1, y1 = int(np.max(pts[:, 0])), int(np.max(pts[:, 1]))
            elements.append({
                "kind": "qr_code",
                "box": [max(0, x0), max(0, y0), max(2, x1 - x0), max(2, y1 - y0)],
                "action": "preserve_and_review",
                "essential_unknown": True,
            })
        else:
            # Fallback high-contrast finder-pattern check in corners for
            # synthetic/low-res QR codes where QRCodeDetector may not lock.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame
            cw, ch = int(w * 0.22), int(h * 0.28)
            corners = [
                ("top_left", 0, 0, cw, ch),
                ("top_right", w - cw, 0, cw, ch),
                ("bottom_left", 0, h - ch, cw, ch),
                ("bottom_right", w - cw, h - ch, cw, ch),
            ]
            for name, cx, cy, rw, rh in corners:
                patch = gray[cy:cy + rh, cx:cx + rw]
                if patch.size == 0:
                    continue
                edges = cv2.Canny(patch, 80, 200)
                edge_density = float(np.count_nonzero(edges)) / float(patch.size)
                if edge_density > 0.04 and float(np.std(patch)) > 45.0:
                    elements.append({
                        "kind": "corner_graphic_or_qr",
                        "corner": name,
                        "box": [cx, cy, rw, rh],
                        "action": "preserve_and_review",
                        "essential_unknown": True,
                    })
    except Exception:
        pass
    return elements


def check_caption_and_hook_safe_area(layout, seam_y_fraction=None,
                                     face_boxes_out=None, hook_band=None):
    """Verify that the caption band and optional hook band stay inside platform
    safe margins and do not cover faces in the rendered 9:16 output.

    ``face_boxes_out``: list of (x0, y0, x1, y1) in output frame fractions.
    """
    layout_lower = str(layout or "track").lower()
    if layout_lower in ("split", "inset", "cam_screen"):
        if seam_y_fraction is not None:
            cap_band = (max(0.0, seam_y_fraction - 0.06),
                        min(1.0, seam_y_fraction + 0.06))
        else:
            cap_band = hook_placement.SEAM_CAPTIONS
    else:
        cap_band = hook_placement.BOTTOM_CAPTIONS

    issues = []
    if cap_band[0] < hook_placement.TOP_MARGIN or cap_band[1] > 0.90:
        issues.append("caption_band_outside_platform_safe_area")

    for f in face_boxes_out or []:
        if len(f) < 4:
            continue
        _x0, fy0, _x1, fy1 = f[:4]
        overlap = min(cap_band[1], fy1) - max(cap_band[0], fy0)
        if overlap > 0.02:
            issues.append("caption_overlaps_face")
            break

    if hook_band and len(hook_band) == 2:
        hy0, hy1 = float(hook_band[0]), float(hook_band[1])
        if hy0 < hook_placement.TOP_MARGIN - 1e-3 or hy1 > hook_placement.BOTTOM_UI + 1e-3:
            issues.append("hook_outside_platform_safe_area")
        if min(hy1, cap_band[1]) - max(hy0, cap_band[0]) > 0.01:
            issues.append("hook_overlaps_captions")
        for f in face_boxes_out or []:
            if len(f) < 4:
                continue
            hb = hook_placement.head_box(f[:4])
            if min(hy1, hb[3]) - max(hy0, hb[1]) > 0.02:
                issues.append("hook_overlaps_face")
                break

    return {
        "ok": len(issues) == 0,
        "caption_band": cap_band,
        "issues": issues,
    }


def evaluate_clip_render(clip=None, layout_ranges_list=None, graphs=None,
                         orig_w=1920, orig_h=1080, out_w=1080, out_h=1920,
                         scene_faces=None, transcript=None, source_title=None,
                         sampled_frames=None):
    """Run P0-P3 visual, structural and editorial checks on a rendered clip.

    Container resolution (1080x1920) and ``predicted_score`` never substitute
    for visual/editorial verification: ``needs_review`` is always True until a
    human approves the preview via ``/api/clip/review``.
    """
    flagged = []
    aspect_reports = []
    duplication_reports = []
    face_cut_reports = []

    for idx, graph in enumerate(graphs or []):
        try:
            ar = check_layer_aspect_ratios(graph, orig_w, orig_h, out_w, out_h)
        except Exception as e:
            ar = {"ok": False, "error": str(e), "deformed_layers": []}
        aspect_reports.append(ar)
        if not ar.get("ok"):
            flagged.append("aspect_deformation")

        try:
            fg = foreground_layers(graph, orig_w, orig_h, out_w, out_h)
        except Exception:
            fg = []
        frame = (sampled_frames[idx]
                 if sampled_frames and idx < len(sampled_frames) else None)
        dup = check_panel_duplication(fg, frame=frame)
        duplication_reports.append(dup)
        if dup.get("introduced_duplication"):
            flagged.append("introduced_panel_duplication")
        if dup.get("source_duplication"):
            flagged.append("source_panel_duplication")

        faces = (scene_faces[idx]
                 if scene_faces and idx < len(scene_faces) else None)
        if faces and fg:
            src_boxes = [l["src"] for l in fg]
            fc = check_border_face_cuts(faces, src_boxes, orig_w, orig_h)
            face_cut_reports.append(fc)
            if fc.get("cut_faces"):
                flagged.append("face_cut_at_border")
            if fc.get("duplicated_faces"):
                flagged.append("face_duplicated_across_panels")

    screen_elements = []
    for frame in sampled_frames or []:
        screen_elements.extend(detect_screen_elements(frame))
    if screen_elements:
        flagged.append("screen_elements_detected_for_review")

    editorial = evaluate_editorial_quality(
        clip or {}, transcript=transcript, source_title=source_title
    )
    for w in editorial.get("warnings") or []:
        flagged.append(w)

    confidences = [
        float(r["confidence"])
        for r in (layout_ranges_list or [])
        if isinstance(r, dict) and r.get("confidence") is not None
    ]
    mean_conf = (round(sum(confidences) / len(confidences), 3)
                 if confidences else 0.8)
    if mean_conf < 0.55:
        flagged.append("low_layout_confidence")

    unique_flags = list(dict.fromkeys(flagged))
    passed_visual = not any(
        f in unique_flags
        for f in ("aspect_deformation", "introduced_panel_duplication",
                  "face_cut_at_border", "face_duplicated_across_panels")
    )
    passed_editorial = not editorial.get("needs_editorial_review")

    existing_gate = (clip or {}).get("quality_gate") if isinstance(clip, dict) else None
    review_status = (
        existing_gate.get("review_status")
        if isinstance(existing_gate, dict) and existing_gate.get("review_status") in ("approved", "rejected")
        else "pending_review"
    )

    return {
        "passed_technical": out_w == 1080 and out_h == 1920,
        "passed_visual": passed_visual,
        "passed_editorial": passed_editorial,
        "auto_approved": False,
        "needs_review": review_status != "approved",
        "review_status": review_status,
        "confidence": mean_conf,
        "flagged_issues": unique_flags,
        "editorial": editorial,
        "screen_elements": screen_elements,
        "aspect_reports": aspect_reports,
        "duplication_reports": duplication_reports,
        "face_cut_reports": face_cut_reports,
    }


def compare_layout_candidates(orig_w, orig_h, out_w=1080, out_h=1920,
                              face_boxes=None, inset_box=None,
                              proposed_graph=None):
    """Compare the 4 candidate vertical reframing modes on the same shot:
      - ``A_centre_crop``: fixed 9:16 centre crop
      - ``B_full_padding``: full horizontal frame with blurred vertical padding
      - ``C_current_auto``: legacy auto framing (0.42 GENERAL or single TRACK)
      - ``D_proposed_layout``: the new layout-aware composition (Camera+Screen,
        border-safe GENERAL, non-overlapping SPLIT, or 2x2 PANEL)
    """
    import camera_inset
    import panel_layout
    import reframe_v2
    import split_layout

    face_boxes = face_boxes or []
    crop_h = orig_h - (orig_h % 2)
    crop_w = int(round(crop_h * (out_w / float(out_h))))
    crop_w = min(orig_w, crop_w - (crop_w % 2))
    centre_x = (orig_w - crop_w) // 2
    centre_x -= centre_x % 2

    graph_a = (
        f"[0:v]crop=w={crop_w}:h={crop_h}:x={centre_x}:y=0,"
        f"scale={out_w}:{out_h},setsar=1[v]"
    )
    full_h = reframe_v2.full_width_content_height(orig_w, orig_h, out_w)
    graph_b = reframe_v2.general_filtergraph(
        out_w, out_h, content_h=full_h, orig_w=orig_w, orig_h=orig_h
    )
    graph_c = reframe_v2.general_filtergraph(
        out_w, out_h, orig_w=orig_w, orig_h=orig_h
    )

    if proposed_graph is None:
        if inset_box is not None:
            proposed_graph = camera_inset.cam_screen_filtergraph(
                orig_w, orig_h, out_w, out_h, inset_box
            )
        elif len(face_boxes) in (3, 4):
            centres = [split_layout._centre(b) for b in sorted(face_boxes, key=lambda b: b[0])]
            proposed_graph = panel_layout.panel_filtergraph(
                orig_w, orig_h, out_w, out_h, centres
            )
        elif len(face_boxes) == 2:
            ordered = sorted(face_boxes, key=lambda b: b[0])
            proposed_graph = split_layout.split_filtergraph(
                orig_w, orig_h, out_w, out_h,
                split_layout._centre(ordered[0]),
                split_layout._centre(ordered[1]),
            )
        else:
            proposed_graph = reframe_v2.general_filtergraph(
                out_w, out_h, orig_w=orig_w, orig_h=orig_h, face_boxes=face_boxes
            )

    candidates = {
        "A_centre_crop": graph_a,
        "B_full_padding": graph_b,
        "C_current_auto": graph_c,
        "D_proposed_layout": proposed_graph,
    }

    results = {}
    for name, graph in candidates.items():
        ar = check_layer_aspect_ratios(graph, orig_w, orig_h, out_w, out_h)
        fg = foreground_layers(graph, orig_w, orig_h, out_w, out_h)
        dup = check_panel_duplication(fg)
        fc = check_border_face_cuts(
            face_boxes, [l["src"] for l in fg], orig_w, orig_h
        )
        # Measure how well faces and screen context are preserved without cuts
        cuts = len(fc["cut_faces"])
        excluded = len(fc["excluded_faces"])
        dups = len(dup["introduced_duplication"]) + len(fc["duplicated_faces"])
        deformed = len(ar["deformed_layers"])
        # Bonus when an inset webcam is given its own dedicated top panel
        has_dedicated_cam = (
            inset_box is not None and len(fg) >= 2
            and fg[0]["dst"][1] < fg[1]["dst"][1]
        )
        score = (
            100.0
            - 35.0 * deformed
            - 30.0 * dups
            - 25.0 * cuts
            - 20.0 * excluded
            + (12.0 if has_dedicated_cam else 0.0)
        )
        results[name] = {
            "aspect_ok": ar["ok"],
            "max_aspect_error": ar["max_relative_error"],
            "introduced_duplications": dups,
            "faces_cut": cuts,
            "faces_excluded": excluded,
            "foreground_layers": len(fg),
            "quality_score": round(max(0.0, score), 1),
        }

    best = max(results.items(), key=lambda kv: kv[1]["quality_score"])[0]
    return {
        "candidates": results,
        "recommended": best,
    }
