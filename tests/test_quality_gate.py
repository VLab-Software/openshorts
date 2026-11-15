"""Unit tests for P0-P4 visual & editorial quality gate and 4-way layout comparison."""
import numpy as np

import camera_inset
import panel_layout
import quality_gate
import reframe_v2
import split_layout
from clip_selection import evaluate_editorial_quality


class TestLayerAspectIntegrity:
    def test_all_builtin_layouts_preserve_layer_aspect_ratios(self):
        graphs = [
            camera_inset.cam_screen_filtergraph(1920, 1080, 1080, 1920, (24, 24, 356, 200)),
            split_layout.split_filtergraph(1920, 1080, 1080, 1920, (550, 400, 180, 180), (1370, 400, 180, 180)),
            panel_layout.panel_filtergraph(
                1920, 1080, 1080, 1920,
                [(320, 400, 180, 180), (960, 400, 180, 180), (1600, 400, 180, 180)],
            ),
            reframe_v2.general_filtergraph(1080, 1920, orig_w=1920, orig_h=1080),
        ]
        for g in graphs:
            res = quality_gate.check_layer_aspect_ratios(g, 1920, 1080, 1080, 1920)
            assert res["ok"], f"Deformation in graph: {res}"

    def test_prebaked_layer_stretch_is_caught_despite_valid_container_and_sar(self):
        # Output is 1080x1920 with setsar=1, but the camera crop (400x400, 1:1)
        # is stretched into a 1080x500 (2.16:1) band.
        stretched_graph = (
            "[0:v]split=2[bga][ca];"
            "[bga]scale=-2:1920,crop=w=1080:h=1920,gblur=sigma=12[bg];"
            "[ca]crop=w=400:h=400:x=0:y=0,scale=1080:500[cam];"
            "[bg][cam]overlay=x=0:y=100,setsar=1[v]"
        )
        res = quality_gate.check_layer_aspect_ratios(stretched_graph, 1920, 1080, 1080, 1920)
        assert not res["ok"]
        assert len(res["deformed_layers"]) == 1
        assert res["max_relative_error"] > 0.5


class TestPanelDuplicationAndFaceCuts:
    def test_overlapping_split_panels_caught_as_introduced_duplication(self):
        # Simulates the old BRKsEDU podcast failure where two wide crops both
        # cover the middle speaker at x=960.
        layers = [
            {"src": [100, 100, 1050, 864], "dst": [0, 0, 1080, 960]},
            {"src": [750, 100, 1050, 864], "dst": [0, 960, 1080, 960]},
        ]
        dup = quality_gate.check_panel_duplication(layers)
        assert not dup["ok"]
        assert len(dup["introduced_duplication"]) == 1

    def test_cam_screen_layout_is_not_flagged_as_panel_duplication(self):
        graph = camera_inset.cam_screen_filtergraph(1920, 1080, 1080, 1920, (24, 24, 356, 200))
        fg = quality_gate.foreground_layers(graph, 1920, 1080, 1080, 1920)
        dup = quality_gate.check_panel_duplication(fg)
        assert dup["ok"]

    def test_source_panel_duplication_distinguished_from_introduced(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        frame[:, :800] = [180, 90, 40]
        frame[:, 1100:1900] = [180, 90, 40]
        layers = [
            {"src": [50, 100, 600, 800], "dst": [0, 0, 540, 960]},
            {"src": [1200, 100, 600, 800], "dst": [540, 0, 540, 960]},
        ]
        dup = quality_gate.check_panel_duplication(layers, frame=frame)
        assert dup["ok"]  # Not an introduced crop overlap
        assert len(dup["source_duplication"]) == 1
        assert dup["needs_review"]

    def test_border_face_cut_caught_on_aggressive_side_crop_and_fixed_by_safe_general(self):
        faces = [
            [60, 350, 180, 180],    # left speaker near border
            [870, 350, 180, 180],   # middle speaker
            [1680, 350, 180, 180],  # right speaker near border
        ]
        # Default 0.42 GENERAL trims ~236px off left and right, slicing faces 0 and 2
        legacy_graph = reframe_v2.general_filtergraph(1080, 1920, orig_w=1920, orig_h=1080)
        legacy_fg = quality_gate.foreground_layers(legacy_graph, 1920, 1080, 1080, 1920)
        legacy_check = quality_gate.check_border_face_cuts(
            faces, [l["src"] for l in legacy_fg], 1920, 1080
        )
        assert not legacy_check["ok"]
        assert len(legacy_check["cut_faces"]) == 2

        # Safe GENERAL with face_boxes keeps all 3 faces intact
        safe_graph = reframe_v2.general_filtergraph(
            1080, 1920, orig_w=1920, orig_h=1080, face_boxes=faces
        )
        safe_fg = quality_gate.foreground_layers(safe_graph, 1920, 1080, 1080, 1920)
        safe_check = quality_gate.check_border_face_cuts(
            faces, [l["src"] for l in safe_fg], 1920, 1080
        )
        assert safe_check["ok"]
        assert safe_check["cut_faces"] == []


class TestScreenElementsAndEditorialGate:
    def test_qr_or_corner_graphic_is_preserved_and_flagged_for_review(self):
        frame = np.full((1080, 1920, 3), 30, dtype=np.uint8)
        # Draw a high-contrast checkerboard in the bottom-right corner
        for y in range(820, 1060, 12):
            for x in range(1620, 1900, 12):
                if ((x // 12) + (y // 12)) % 2 == 0:
                    frame[y:y + 12, x:x + 12] = 250
        elements = quality_gate.detect_screen_elements(frame)
        assert elements
        assert all(el["action"] == "preserve_and_review" for el in elements)

    def test_editorial_flags_unqualified_rumor_and_off_topic_tangent(self):
        transcript = {
            "segments": [{
                "start": 0.0,
                "end": 36.0,
                "text": "Eu acho que essa pré-venda de jogos em geral é um rumor sem sentido.",
            }]
        }
        clip_bad = {
            "start": 0.0,
            "end": 36.0,
            "predicted_score": 84,
            "viral_hook_text": "Preço oficial confirmado nos jogos!",
            "video_title_for_youtube_short": "Foi confirmado agora!",
        }
        ed_bad = evaluate_editorial_quality(
            clip_bad, transcript=transcript, source_title="GTA 6 Trailer Análise"
        )
        assert not ed_bad["rumor_qualified"]
        assert not ed_bad["topic_relevant"]
        assert "opinion_or_rumor_stated_as_fact" in ed_bad["warnings"]
        assert "tangent_from_source_topic" in ed_bad["warnings"]

        clip_good = {
            "start": 0.0,
            "end": 36.0,
            "predicted_score": 84,
            "viral_hook_text": "Opinião sobre rumores de pré-venda e GTA 6?",
            "video_title_for_youtube_short": "Vale fazer pré-venda no caso de GTA 6? Opinião",
        }
        ed_good = evaluate_editorial_quality(
            clip_good, transcript=transcript, source_title="GTA 6 Trailer Análise"
        )
        assert ed_good["rumor_qualified"]
        assert ed_good["topic_relevant"]
        assert not ed_good["needs_editorial_review"]

    def test_score_84_and_1080x1920_do_not_auto_approve_clip(self):
        clip = {
            "start": 0.0,
            "end": 36.0,
            "predicted_score": 84,
            "viral_hook_text": "Veja o detalhe do trailer de GTA 6.",
            "video_title_for_youtube_short": "Detalhe do trailer de GTA 6.",
        }
        graph = camera_inset.cam_screen_filtergraph(1920, 1080, 1080, 1920, (24, 24, 356, 200))
        gate = quality_gate.evaluate_clip_render(
            clip=clip, graphs=[graph], orig_w=1920, orig_h=1080, out_w=1080, out_h=1920
        )
        assert gate["passed_technical"]
        assert gate["passed_visual"]
        assert gate["auto_approved"] is False
        assert gate["needs_review"] is True
        assert gate["review_status"] == "pending_review"


class TestCompareLayoutCandidates:
    def test_proposed_cam_screen_beats_centre_crop_on_streamer_reaction(self):
        comp = quality_gate.compare_layout_candidates(
            1920, 1080, 1080, 1920,
            face_boxes=[[80, 60, 120, 120]],
            inset_box=(24, 24, 356, 200),
        )
        assert comp["recommended"] == "D_proposed_layout"
        assert (
            comp["candidates"]["D_proposed_layout"]["quality_score"]
            > comp["candidates"]["A_centre_crop"]["quality_score"]
        )

    def test_proposed_layout_beats_legacy_auto_on_three_person_podcast(self):
        faces = [
            [60, 350, 180, 180],
            [870, 350, 180, 180],
            [1680, 350, 180, 180],
        ]
        comp = quality_gate.compare_layout_candidates(
            1920, 1080, 1080, 1920, face_boxes=faces
        )
        assert comp["candidates"]["C_current_auto"]["faces_cut"] == 2
        assert comp["candidates"]["D_proposed_layout"]["faces_cut"] == 0
        assert (
            comp["candidates"]["D_proposed_layout"]["quality_score"]
            > comp["candidates"]["C_current_auto"]["quality_score"]
        )
