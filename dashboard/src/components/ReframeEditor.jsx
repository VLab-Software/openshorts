import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Loader2, Crosshair, RotateCcw, AlertCircle, Play, Pause, Columns2, Monitor } from 'lucide-react';
import { getApiUrl } from '../config';
import { apiJson } from '../lib/api';

// Manual reframing: the automatic crop is right most of the time and grossly
// wrong occasionally, and until now there was no way to say "frame it here".
//
// The unit is the scene, because a podcast cuts between a fixed close camera
// and a fixed wide one and the right crop differs per camera. Scenes the user
// does not touch stay automatic, so fixing one bad shot cannot spoil the good
// ones — only adjusted scenes are sent.
//
// Two things a still frame cannot tell you, and both are handled here:
//   - WHO is talking. Each scene plays its own range of an uncropped preview,
//     with sound, and the rectangle stays overlaid while it plays.
//   - Whether one window is even enough. A scene can be split into two stacked
//     regions, positioned independently.

const fmt = (s) => {
    const m = Math.floor(s / 60);
    const r = Math.floor(s % 60);
    return `${m}:${String(r).padStart(2, '0')}`;
};

export default function ReframeEditor({ jobId, clipIndex, clipTitle, onClose, onReframed }) {
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const [data, setData] = useState(null);
    const [overrides, setOverrides] = useState({});   // idx -> number | {top,bottom}
    const [playing, setPlaying] = useState(null);     // scene index being played
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        let alive = true;
        (async () => {
            try {
                const res = await apiJson(`/api/clip/${jobId}/${clipIndex}/scenes`);
                if (!alive) return;
                setData(res);
                // Start from what is already applied. A re-render rebuilds
                // from source with only what it receives, so opening blank
                // would quietly discard every earlier adjustment on the next
                // save.
                const salvos = res.saved_overrides || {};
                setOverrides(Object.fromEntries(
                    Object.entries(salvos).map(([k, v]) => [Number(k), v])
                ));
            } catch (e) {
                if (alive) setError(e?.message || 'Could not read the scenes of this clip.');
            } finally {
                if (alive) setLoading(false);
            }
        })();
        return () => { alive = false; };
    }, [jobId, clipIndex]);

    const half = (data?.crop_width_fraction ?? 0.5) / 2;
    const clamp = useCallback((v) => Math.min(1 - half, Math.max(half, v)), [half]);

    // What a scene shows right now: the user's value, else the backend's
    // suggestion (the biggest face in the shot).
    const valueOf = useCallback((scene) => (
        overrides[scene.index] ?? clamp(scene.suggested_center)
    ), [overrides, clamp]);

    const setSingle = useCallback((idx, fraction) => {
        setOverrides((o) => ({ ...o, [idx]: clamp(fraction) }));
    }, [clamp]);

    const setSplitHalf = useCallback((idx, which, fraction) => {
        setOverrides((o) => {
            const cur = o[idx];
            const base = (cur && typeof cur === 'object')
                ? cur
                : { top: { x: clamp(0.3), y: 0.5 }, bottom: { x: clamp(0.7), y: 0.5 } };
            const anterior = base[which] || { y: 0.5 };
            return { ...o, [idx]: {
                ...base,
                [which]: { x: clamp(fraction), y: anterior.y ?? 0.5 },
            } };
        });
    }, [clamp]);

    const toggleSplit = useCallback((idx, scene) => {
        setOverrides((o) => {
            const cur = o[idx];
            if (cur && typeof cur === 'object' && 'top' in cur) {
                // back to a single window, kept where the top half was
                return { ...o, [idx]: cur.top?.x ?? 0.5 };
            }
            const centre = typeof cur === 'number' ? cur : clamp(scene.suggested_center);
            // SPLIT halves crop vertically as well, so each carries the face
            // height the backend measured for this scene.
            const y = scene.suggested_center_y ?? 0.5;
            return { ...o, [idx]: {
                top: { x: clamp(centre - 0.2), y },
                bottom: { x: clamp(centre + 0.2), y },
            } };
        });
    }, [clamp]);

    const toggleCamScreen = useCallback((idx, scene) => {
        setOverrides((o) => {
            const cur = o[idx];
            if (cur && typeof cur === 'object' && 'camera' in cur) {
                return { ...o, [idx]: clamp(scene.suggested_center) };
            }
            const cx = scene.suggested_center ?? 0.14;
            const cy = scene.suggested_center_y ?? 0.16;
            const camW = 0.24;
            const camH = 0.24;
            const x = Math.min(1 - camW, Math.max(0, cx - camW / 2));
            const y = Math.min(1 - camH, Math.max(0, cy - camH / 2));
            return {
                ...o,
                [idx]: {
                    camera: { x, y, w: camW, h: camH },
                    screen: { x: 0, y: 0, w: 1, h: 1 },
                },
            };
        });
    }, [clamp]);

    const setCameraBox = useCallback((idx, fx, fy) => {
        setOverrides((o) => {
            const cur = o[idx];
            const cam = (cur && cur.camera) || { x: 0.02, y: 0.03, w: 0.24, h: 0.24 };
            const w = cam.w ?? 0.24;
            const h = cam.h ?? 0.24;
            const x = Math.min(1 - w, Math.max(0, fx - w / 2));
            const y = Math.min(1 - h, Math.max(0, fy - h / 2));
            return {
                ...o,
                [idx]: {
                    camera: { x, y, w, h },
                    screen: (cur && cur.screen) || { x: 0, y: 0, w: 1, h: 1 },
                },
            };
        });
    }, []);

    const resetScene = useCallback((idx) => {
        setOverrides((o) => {
            const next = { ...o };
            delete next[idx];
            return next;
        });
    }, []);

    const adjusted = Object.keys(overrides).length;

    const handleSave = async () => {
        if (!adjusted || saving) return;
        setSaving(true);
        setError(null);
        try {
            const payload = Object.fromEntries(Object.entries(overrides).map(([k, v]) => [
                String(k),
                (v && typeof v === 'object' && 'camera' in v)
                    ? {
                        camera: {
                            x: Number(v.camera.x.toFixed(4)),
                            y: Number(v.camera.y.toFixed(4)),
                            w: Number((v.camera.w ?? 0.24).toFixed(4)),
                            h: Number((v.camera.h ?? 0.24).toFixed(4)),
                        },
                        screen: {
                            x: Number((v.screen?.x ?? 0).toFixed(4)),
                            y: Number((v.screen?.y ?? 0).toFixed(4)),
                            w: Number((v.screen?.w ?? 1).toFixed(4)),
                            h: Number((v.screen?.h ?? 1).toFixed(4)),
                        },
                    }
                    : (v && typeof v === 'object')
                        ? { top: { x: Number(v.top.x.toFixed(4)), y: Number(v.top.y.toFixed(4)) },
                            bottom: { x: Number(v.bottom.x.toFixed(4)), y: Number(v.bottom.y.toFixed(4)) } }
                        : Number(v.toFixed(4)),
            ]));
            const res = await apiJson('/api/clip/reframe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ job_id: jobId, clip_index: clipIndex, crop_overrides: payload }),
            });
            if (onReframed) onReframed(clipIndex, res);
            onClose();
        } catch (e) {
            setError(e?.message || 'The re-render failed.');
        } finally {
            setSaving(false);
        }
    };

    return (
        /* Bottom sheet on a phone, centred dialog from sm — same shape as the
           shared Modal so the app has one overlay idiom, not two. */
        <div className="fixed inset-0 z-50 bg-black/80 flex items-end sm:items-center justify-center p-0 sm:p-4">
            <div className="card w-full max-w-3xl max-h-[92vh] sm:max-h-[90vh] flex flex-col rounded-b-none sm:rounded-card animate-sheet-up sm:animate-none">
                <div className="flex items-center justify-between p-4 border-b border-rule">
                    <div className="flex items-center gap-2.5 min-w-0">
                        <Crosshair size={18} className="text-brass shrink-0" />
                        <div className="min-w-0">
                            <h2 className="text-base font-medium text-ink lowercase truncate">reframing</h2>
                            {clipTitle && <p className="text-xs text-muted truncate">{clipTitle}</p>}
                        </div>
                    </div>
                    <button onClick={onClose} className="p-1.5 hover:bg-paper3 rounded-input transition-colors">
                        <X size={18} className="text-muted" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto overscroll-contain custom-scrollbar p-4 space-y-5">
                    <p className="text-xs text-muted leading-relaxed">
                        Play a scene to hear who is talking, then drag the rectangle over
                        the person you want. Each scene is one camera. Scenes you leave
                        alone keep the automatic camera.
                    </p>

                    {loading && (
                        <div className="flex items-center gap-2 text-sm text-muted py-8 justify-center">
                            <Loader2 size={18} className="animate-spin text-brass" />
                            reading the scenes…
                        </div>
                    )}

                    {error && (
                        <div className="flex items-start gap-2 text-xs text-warn border border-warn/40 rounded-input p-3">
                            <AlertCircle size={14} className="shrink-0 mt-0.5" />
                            <span>{error}</span>
                        </div>
                    )}

                    {data?.scenes?.map((scene) => (
                        <SceneRow
                            key={scene.index}
                            scene={scene}
                            value={valueOf(scene)}
                            widthFraction={data.crop_width_fraction}
                            previewUrl={data.preview_url}
                            touched={scene.index in overrides}
                            playing={playing === scene.index}
                            onPlayToggle={() => setPlaying((p) => (p === scene.index ? null : scene.index))}
                            onMoveSingle={(f) => setSingle(scene.index, f)}
                            onMoveHalf={(which, f) => setSplitHalf(scene.index, which, f)}
                            onMoveCamera={(fx, fy) => setCameraBox(scene.index, fx, fy)}
                            onToggleSplit={() => toggleSplit(scene.index, scene)}
                            onToggleCamScreen={() => toggleCamScreen(scene.index, scene)}
                            onReset={() => resetScene(scene.index)}
                        />
                    ))}
                </div>

                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 border-t border-rule">
                    <span className="text-xs text-muted">
                        {adjusted === 0
                            ? 'nothing adjusted yet'
                            : `${adjusted} scene${adjusted > 1 ? 's' : ''} reframed by hand`}
                    </span>
                    <div className="flex items-center gap-2 [&>button]:flex-1 sm:[&>button]:flex-none">
                        <button onClick={onClose} className="btn-quiet py-2 px-4 text-sm">cancel</button>
                        <button
                            onClick={handleSave}
                            disabled={!adjusted || saving}
                            className="btn-primary py-2 px-4 text-sm disabled:opacity-40"
                        >
                            {saving ? <Loader2 size={16} className="animate-spin" /> : null}
                            {saving ? 're-rendering…' : 'apply reframing'}
                        </button>
                    </div>
                </div>
                <div className="sm:hidden safe-bottom" />
            </div>
        </div>
    );
}


// One scene. While it plays, the frame is replaced by the uncropped preview
// seeked to this scene, so the rectangle can be judged against moving pictures
// and sound rather than a single still.
function SceneRow({ scene, value, widthFraction, previewUrl, touched, playing,
                    onPlayToggle, onMoveSingle, onMoveHalf, onMoveCamera,
                    onToggleSplit, onToggleCamScreen, onReset }) {
    const boxRef = useRef(null);
    const videoRef = useRef(null);
    const [dragging, setDragging] = useState(null);   // null | 'single' | 'top' | 'bottom' | 'camera'

    const isCamScreen = value && typeof value === 'object' && 'camera' in value;
    const isSplit = value && typeof value === 'object' && 'top' in value;

    // Play only this scene's slice of the shared preview.
    useEffect(() => {
        const v = videoRef.current;
        if (!v) return;
        if (!playing) { v.pause(); return; }
        v.currentTime = scene.start;
        v.play().catch(() => {});
        const stopAtEnd = () => { if (v.currentTime >= scene.end) { v.pause(); } };
        v.addEventListener('timeupdate', stopAtEnd);
        return () => v.removeEventListener('timeupdate', stopAtEnd);
    }, [playing, scene.start, scene.end]);

    const fractionFromEvent = useCallback((clientX) => {
        const el = boxRef.current;
        if (!el) return 0.5;
        const rect = el.getBoundingClientRect();
        return (clientX - rect.left) / rect.width;
    }, []);

    const pointFromEvent = useCallback((clientX, clientY) => {
        const el = boxRef.current;
        if (!el) return [0.5, 0.5];
        const rect = el.getBoundingClientRect();
        return [
            (clientX - rect.left) / rect.width,
            (clientY - rect.top) / rect.height,
        ];
    }, []);

    useEffect(() => {
        if (!dragging) return;
        const move = (e) => {
            const x = e.touches ? e.touches[0].clientX : e.clientX;
            const y = e.touches ? e.touches[0].clientY : e.clientY;
            if (dragging === 'camera') {
                const [fx, fy] = pointFromEvent(x, y);
                onMoveCamera(fx, fy);
                return;
            }
            const f = fractionFromEvent(x);
            if (dragging === 'single') onMoveSingle(f);
            else onMoveHalf(dragging, f);
        };
        const up = () => setDragging(null);
        window.addEventListener('mousemove', move);
        window.addEventListener('mouseup', up);
        window.addEventListener('touchmove', move);
        window.addEventListener('touchend', up);
        return () => {
            window.removeEventListener('mousemove', move);
            window.removeEventListener('mouseup', up);
            window.removeEventListener('touchmove', move);
            window.removeEventListener('touchend', up);
        };
    }, [dragging, fractionFromEvent, pointFromEvent, onMoveSingle, onMoveHalf, onMoveCamera]);

    const startDrag = (which) => (e) => {
        e.stopPropagation();
        setDragging(which);
        const x = e.touches ? e.touches[0].clientX : e.clientX;
        const y = e.touches ? e.touches[0].clientY : e.clientY;
        if (which === 'camera') {
            const [fx, fy] = pointFromEvent(x, y);
            onMoveCamera(fx, fy);
        } else if (which === 'single') {
            onMoveSingle(fractionFromEvent(x));
        } else {
            onMoveHalf(which, fractionFromEvent(x));
        }
    };

    const win = (centre, label, which) => {
        const leftPct = (centre - widthFraction / 2) * 100;
        return (
            <div
                key={which}
                onMouseDown={startDrag(which)}
                onTouchStart={startDrag(which)}
                className="absolute inset-y-0 border-2 border-brass cursor-ew-resize"
                style={{ left: `${leftPct}%`, width: `${widthFraction * 100}%` }}
            >
                {label && (
                    <span className="absolute top-1 left-1 text-[10px] px-1 rounded bg-brass text-paper lowercase">
                        {label}
                    </span>
                )}
            </div>
        );
    };

    return (
        <div className="space-y-1.5">
            <div className="flex items-center justify-between text-xs gap-2">
                <div className="flex items-center gap-2 min-w-0">
                    <button
                        onClick={onPlayToggle}
                        className="flex items-center gap-1 text-ink2 hover:text-brass transition-colors"
                        title="play this scene with sound"
                    >
                        {playing ? <Pause size={13} /> : <Play size={13} />}
                    </button>
                    <span className="readout text-muted truncate">
                        scene {scene.index + 1} · {fmt(scene.start)}–{fmt(scene.end)}
                    </span>
                </div>
                <div className="flex items-center gap-3 shrink-0">
                    <button
                        onClick={onToggleCamScreen}
                        className={`flex items-center gap-1 transition-colors ${
                            isCamScreen ? 'text-brass' : 'text-muted hover:text-ink2'}`}
                        title="camera on top, central captions, and screen/game below"
                    >
                        <Monitor size={12} /> cam + screen
                    </button>
                    <button
                        onClick={onToggleSplit}
                        className={`flex items-center gap-1 transition-colors ${
                            isSplit ? 'text-brass' : 'text-muted hover:text-ink2'}`}
                        title="stack two regions instead of one window"
                    >
                        <Columns2 size={12} /> split
                    </button>
                    {touched ? (
                        <button onClick={onReset} className="flex items-center gap-1 text-brass hover:underline">
                            <RotateCcw size={12} /> automatic
                        </button>
                    ) : (
                        <span className="text-muted">automatic</span>
                    )}
                </div>
            </div>

            <div
                ref={boxRef}
                className={`relative overflow-hidden rounded-input select-none border ${
                    touched ? 'border-brass' : 'border-rule'}`}
            >
                {playing && previewUrl ? (
                    <video
                        ref={videoRef}
                        src={getApiUrl(previewUrl)}
                        playsInline
                        className="w-full block"
                    />
                ) : scene.thumbnail_url ? (
                    <img
                        src={getApiUrl(scene.thumbnail_url)}
                        alt=""
                        draggable={false}
                        className="w-full block pointer-events-none"
                    />
                ) : (
                    <div className="w-full aspect-video bg-paper3" />
                )}

                {/* Everything outside the kept region is dimmed, so what survives
                    the crop is what stays bright. */}
                {!isSplit && !isCamScreen && (
                    <>
                        <div className="absolute inset-y-0 left-0 bg-black/65 pointer-events-none"
                             style={{ width: `${Math.max(0, (value - widthFraction / 2) * 100)}%` }} />
                        <div className="absolute inset-y-0 right-0 bg-black/65 pointer-events-none"
                             style={{ width: `${Math.max(0, 100 - (value + widthFraction / 2) * 100)}%` }} />
                    </>
                )}

                {isCamScreen ? (
                    <>
                        <div className="absolute inset-0 border border-brass/50 pointer-events-none">
                            <span className="absolute bottom-1 right-1 text-[10px] px-1 rounded bg-paper2/90 text-ink2 lowercase">
                                screen (bottom)
                            </span>
                        </div>
                        <div
                            onMouseDown={startDrag('camera')}
                            onTouchStart={startDrag('camera')}
                            className="absolute border-2 border-brass cursor-move bg-brass/10"
                            style={{
                                left: `${(value.camera.x ?? 0.02) * 100}%`,
                                top: `${(value.camera.y ?? 0.03) * 100}%`,
                                width: `${(value.camera.w ?? 0.24) * 100}%`,
                                height: `${(value.camera.h ?? 0.24) * 100}%`,
                            }}
                        >
                            <span className="absolute top-0.5 left-0.5 text-[10px] px-1 rounded bg-brass text-paper lowercase">
                                camera (top)
                            </span>
                        </div>
                    </>
                ) : isSplit
                    ? [win(value.top.x, 'top', 'top'), win(value.bottom.x, 'bottom', 'bottom')]
                    : win(value, null, 'single')}
            </div>

            {isCamScreen && (
                <p className="text-[11px] text-muted leading-snug">
                    Composes in 9:16 with <strong>camera</strong> on top, subtitles in a
                    central band, and <strong>screen</strong> below without stretching. Drag
                    the camera box onto the streamer&apos;s webcam.
                </p>
            )}
            {isSplit && (
                <p className="text-[11px] text-muted leading-snug">
                    Two regions stacked in the vertical frame: <strong>top</strong> above,
                    <strong> bottom</strong> below. Drag each one onto the person it should hold.
                </p>
            )}
        </div>
    );
}
