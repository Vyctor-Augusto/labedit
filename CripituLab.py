"""Primeiro corte de reacts para DaVinci Resolve. Python 3, sem pacotes extras."""

import argparse
from array import array
from bisect import bisect_left
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
from statistics import median
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit


PROJECT_DIR = Path(__file__).resolve().parent
RESOLVE_SNAPSHOTS = PROJECT_DIR / ".local" / "resolve-timeline"
CALIBRATIONS = PROJECT_DIR / ".local" / "CripituLab-calibracoes.json"
FACE_MODEL = PROJECT_DIR / "models" / "yunet-dynamic.onnx"


@dataclass
class Media:
    path: Path
    duration: float
    width: int
    height: int
    fps: Fraction
    channels: int
    audio_index: int
    audio_count: int


def tool(name):
    bundled = Path(os.environ.get("LOCALAPPDATA", ".")) / "AutoSubs" / f"{name}.exe"
    installed = Path.home() / "AppData" / "Local" / "AutoSubs" / f"{name}.exe"
    for candidate in (shutil.which(name), bundled, installed):
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise ValueError(f"{name} não encontrado. Instale FFmpeg ou AutoSubs.")


def run(name, *args, binary=False):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if name == "ffmpeg":
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    result = subprocess.run(
        [tool(name), *map(str, args)], capture_output=True,
        encoding=None if binary else "utf-8", errors=None if binary else "replace",
        creationflags=flags,
    )
    if result.returncode:
        error = result.stderr.decode("utf-8", "replace") if binary else result.stderr
        raise ValueError(error.strip().splitlines()[-1] if error.strip() else name)
    return result


def probe(path, audio_index):
    path = Path(path).resolve(strict=True)
    data = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", path).stdout)
    video = next((s for s in data["streams"]
                  if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if not video:
        raise ValueError(f"Arquivo sem vídeo: {path.name}")
    audio = [s for s in data["streams"] if s["codec_type"] == "audio"]
    if audio_index < 0 or (audio_index >= len(audio) and (audio or audio_index)):
        raise ValueError(f"Faixa de áudio inexistente em {path.name}. A primeira é 0.")
    width, height = video["width"], video["height"]
    rotation = next((s.get("rotation", 0) for s in video.get("side_data_list", [])
                     if "rotation" in s), 0)
    if abs(rotation) % 180 == 90:
        width, height = height, width
    fps = Fraction(video["avg_frame_rate"] if video["avg_frame_rate"] != "0/0" else video["r_frame_rate"])
    duration = float(video.get("duration") or data["format"]["duration"])
    channels = audio[audio_index]["channels"] if audio else 0
    return Media(path, duration, width, height, fps, channels, audio_index, len(audio))


SYNC_RATE = 4000
SYNC_HOP = 80
SYNC_LIMIT = 5


def sync_audio(media, track):
    data = run("ffmpeg", "-v", "error", "-i", media.path, "-map", f"0:a:{track}",
               "-ac", "1", "-ar", SYNC_RATE, "-f", "s16le", "-", binary=True).stdout
    return array("h", data)


def envelope(samples):
    return [math.log1p(sum(x * x for x in samples[i:i + SYNC_HOP]) / SYNC_HOP)
            for i in range(0, len(samples) - SYNC_HOP + 1, SYNC_HOP)]


def correlation(a, b, centered=True):
    n = len(a)
    if n < 2 or n != len(b):
        return 0
    ab = sum(x * y for x, y in zip(a, b))
    aa = sum(x * x for x in a)
    bb = sum(y * y for y in b)
    if centered:
        sa, sb = sum(a), sum(b)
        ab -= sa * sb / n
        aa -= sa * sa / n
        bb -= sb * sb / n
    return ab / math.sqrt(aa * bb) if aa > 1e-8 and bb > 1e-8 else 0


def audio_sync(camera, screen, desktop_track=None):
    if not screen.audio_count:
        raise ValueError("A captura OBS está sem áudio para sincronizar.")
    with ThreadPoolExecutor(max_workers=4) as pool:
        mic_job = pool.submit(sync_audio, camera, camera.audio_index)
        screen_jobs = [pool.submit(sync_audio, screen, i) for i in range(screen.audio_count)]
        mic = mic_job.result()
        tracks = [job.result() for job in screen_jobs]
    mic_env = envelope(mic)
    track_envs = [envelope(track) for track in tracks]
    overlap = min(len(mic_env), *(len(env) for env in track_envs))
    hz = SYNC_RATE // SYNC_HOP
    if overlap < hz * 3:
        raise ValueError("Gravações curtas demais para sincronizar pelo áudio.")
    max_lag = min(SYNC_LIMIT * hz, overlap // 4)
    stride = max(1, overlap // 3500)
    indices = range(max_lag, overlap - max_lag, stride)
    anchor = [mic_env[i] for i in indices]
    peaks = [
        max((correlation(anchor, [env[i + lag] for i in indices]), lag)
            for lag in range(-max_lag, max_lag + 1))
        for env in track_envs
    ]
    score, coarse, mic_track = max((score, lag, i) for i, (score, lag) in enumerate(peaks))
    if score < .55:
        raise ValueError("Áudio em comum insuficiente para sincronizar. Use a captura com "
                         "microfone compartilhado ou informe o deslocamento manual.")
    if desktop_track is None:
        candidates = [i for i, env in enumerate(track_envs)
                      if i != mic_track and peaks[i][0] < .65 and max(env, default=0) > 5]
        if not candidates:
            raise ValueError("Faixa isolada do vídeo reagido não identificada. Escolha a faixa manualmente.")
        desktop_track = min(candidates, key=lambda i: peaks[i][0])

    # Refine by waveform in loud mic windows spread across the recording.
    compare = tracks[mic_track]
    base = coarse * SYNC_HOP
    radius = SYNC_HOP * 2
    room = abs(base) + radius + SYNC_RATE
    length = min(len(mic), len(compare))
    offsets = []
    for part in range(3):
        start = max(room, part * length // 3)
        stop = min(length - room, (part + 1) * length // 3)
        windows = range(start, stop, SYNC_RATE // 2)
        if not windows:
            continue
        peak = max(windows, key=lambda p: sum(x * x for x in mic[p:p + 1200]))
        sample = mic[peak:peak + 1200]
        confidence, lag = max(
            (correlation(sample, compare[peak + d:peak + d + len(sample)], centered=False), d)
            for d in range(base - radius, base + radius + 1)
        )
        if confidence > .65:
            offsets.append(lag / SYNC_RATE)
    if len(offsets) >= 2 and max(offsets) - min(offsets) > .05:
        raise ValueError("O deslocamento mudou ao longo do vídeo. Confira se ambas as gravações são contínuas.")
    lag = median(offsets) if offsets else coarse / hz
    return -lag, desktop_track, mic_track, score


def merge(intervals, gap=0):
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def active(media, db, pause):
    if not media.channels:
        return []
    log = run("ffmpeg", "-hide_banner", "-nostats", "-i", media.path,
              "-map", f"0:a:{media.audio_index}", "-af",
              f"silencedetect=n={db}dB:d={pause}", "-f", "null", "-").stderr
    intervals = []
    start = 0.0
    silent = False
    for kind, value in re.findall(r"silence_(start|end):\s*(-?[\d.]+)", log):
        point = min(media.duration, max(0, float(value)))
        if kind == "start":
            intervals.append((start, point))
            silent = True
        else:
            start = point
            silent = False
    if not silent:
        intervals.append((start, media.duration))
    return merge(intervals)


def media_signature(path):
    with path.open("rb") as source:
        size = path.stat().st_size
        head = source.read(65536)
        source.seek(max(0, size - 65536))
        return hashlib.sha256(str(size).encode() + head + source.read(65536)).hexdigest()


def calibrated_delay(path):
    if not CALIBRATIONS.is_file():
        return 0
    values = json.loads(CALIBRATIONS.read_text(encoding="utf-8"))
    return float(values.get(media_signature(path), 0))


def intersects(intervals, left, right):
    return any(start < right and end > left for start, end in intervals)


def plan(camera, reference, mic_seconds, ref_seconds, args, fps):
    def frames(seconds):
        return round(seconds * fps)

    length = math.floor(camera.duration * fps)
    offset = frames(args.offset)
    ref_end = offset + (math.floor(reference.duration * fps) if reference else 0)
    lo, hi = max(0, offset), min(length, ref_end)
    if reference and lo >= hi:
        raise ValueError("A câmera e a referência não se sobrepõem. Confira o ajuste de sincronismo.")
    mic = merge((frames(a), frames(b)) for a, b in mic_seconds)
    ref = merge((frames(a) + offset, frames(b) + offset) for a, b in ref_seconds)
    if reference and not reference.channels:
        ref = [(lo, hi)]
    margin = frames(args.margin)
    # Keep any material with usable audio. A reference with no audio is kept visually.
    activity = merge((max(0, a), min(length, b)) for a, b in mic + ref)
    # Measure the silence before adding safety handles. Padding each side
    # first would silently add two margins to the user's minimum pause.
    joined = merge(activity, gap=max(0, frames(args.pause) - 1))
    smoothed = merge((max(0, a - margin), min(length, b + margin)) for a, b in joined)
    if args.keep_pauses or not smoothed:
        smoothed = [(0, length)]

    # Enter a close only for a sustained intervention while reference audio is quiet.
    close = []
    if reference:
        # A short breath can be trimmed without changing the framing of a
        # sustained comment on either side of it.
        mic_runs = merge(mic, gap=frames(.65))
        cuts = sorted({lo, hi, *(point for pair in ref for point in pair if lo < point < hi)})
        for a, b in zip(cuts, cuts[1:]):
            if b - a < frames(args.close) or intersects(ref, a, b):
                continue
            for x, y in mic_runs:
                start, end = max(a, x - margin), min(b, y + margin)
                if end - start >= frames(args.close):
                    close.append((start, end))
        close = merge(close)

    result = []
    cursor = 0
    for a, b in smoothed:
        cuts = sorted({a, b, *(p for p in (lo, hi) if a < p < b),
                       *(p for pair in close for p in pair if a < p < b)})
        for start, end in zip(cuts, cuts[1:]):
            split = bool(reference and lo <= start < hi and not intersects(close, start, end))
            result.append((start, end, cursor, split))
            cursor += end - start
    return result, offset


def track_face(media):
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise ValueError("Execute Instalar no Resolve.cmd para instalar o OpenCV e o NumPy.") from error
    model = FACE_MODEL
    if not model.is_file():
        raise ValueError("O modelo YuNet do rastreamento está indisponível.")
    width, height, sample_fps = 320, 192, 4
    fit = min(width / media.width, height / media.height)
    image_width = max(2, 2 * round(media.width * fit / 2))
    image_height = max(2, 2 * round(media.height * fit / 2))
    pad_x, pad_y = (width - image_width) // 2, (height - image_height) // 2
    cv2.setNumThreads(1)
    try:
        detector = cv2.FaceDetectorYN.create(str(model), "", (width, height), .72, .3, 20)
    except cv2.error as error:
        raise ValueError(f"O modelo YuNet não abriu: {error}") from error
    process = subprocess.Popen(
        [tool("ffmpeg"), "-v", "error", "-threads", "2", "-filter_threads", "1", "-i", str(media.path),
         "-an", "-vf", (f"fps={sample_fps}:start_time=0,scale={image_width}:{image_height},"
                        f"pad={width}:{height}:{pad_x}:{pad_y}"),
         "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                       | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)),
    )
    detections = []
    frame_size = width * height * 3
    center = (.5, .5)
    try:
        index = 0
        while frame := process.stdout.read(frame_size):
            if len(frame) != frame_size:
                break
            pixels = np.frombuffer(frame, np.uint8).reshape(height, width, 3)
            _, boxes = detector.detect(pixels)
            if boxes is not None:
                candidates = [((x + w / 2 - pad_x) / image_width,
                               (y + h / 2 - pad_y) / image_height, w * h)
                              for x, y, w, h, *_ in boxes if w >= 18 and h >= 18]
                if candidates:
                    x, y, _ = max(candidates, key=lambda box: box[2] /
                                  (1 + 18 * ((box[0] - center[0])**2 + (box[1] - center[1])**2)))
                    center = (x, y)
                    detections.append((index / sample_fps, float(x), float(y)))
            index += 1
        if process.wait():
            raise ValueError("O FFmpeg não conseguiu ler os quadros da webcam.")
    except cv2.error as error:
        raise ValueError(f"O YuNet não conseguiu rastrear a webcam: {error}") from error
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()
    # A short temporal median suppresses single-frame jumps. Gaps hold the last face.
    result = []
    for index, (t, x, y) in enumerate(detections):
        window = [sample for sample in detections[max(0, index-2):index+3] if abs(sample[0] - t) <= .5]
        result.append((t, median(point[1] for point in window), median(point[2] for point in window)))
    return result


def close_motion(camera, start, end, fps, close_number, faces):
    duration = end - start
    step = max(1, round(fps / 2))
    frames = sorted({start, end - 1, *range(start, end, step)})
    height, width = camera.height, camera.width
    fit_height = height * min(1080 / width, 1920 / height)
    fill = max(1080 / width, 1920 / height)
    times = [point[0] for point in faces]
    profile = close_number % 3
    if duration < fps * 2 and profile == 2:
        profile = 1
    points = []
    for frame in frames:
        t = float(frame / fps)
        i = bisect_left(times, t)
        if not times:
            x, y = .5, .5
        elif i == 0 or i == len(times) or times[i] - times[i-1] > 1:
            _, x, y = faces[0 if i == 0 else i-1]
        else:
            left, right = faces[i-1], faces[i]
            mix = (t - left[0]) / (right[0] - left[0])
            x = left[1] + (right[1] - left[1]) * mix
            y = left[2] + (right[2] - left[2]) * mix
        progress = (frame - start) / max(1, duration - 1)
        style = (1.035, 1.095, 1.045 + .10 * progress)[profile]
        scale = fill * style
        max_x = (width * scale - 1080) / 2
        max_y = (height * scale - 1920) / 2
        pan = min(max_x, max(-max_x, (.5 - x) * width * scale))
        tilt = min(max_y, max(-max_y, (y - .5) * height * scale))
        points.append((frame, scale * height / fit_height, pan * 100 / fit_height, tilt * 100 / fit_height))
    return points


def render_closes(camera, segments, fps, faces, output):
    folder = output.parent / f"{output.stem} - mídia" / "Closes"
    closes = [(a, b) for a, b, _, split in segments if not split]
    if not closes:
        return {}
    folder.mkdir(parents=True, exist_ok=True)
    fit_height = camera.height * min(1080 / camera.width, 1920 / camera.height)

    def render(index, start, end):
        output_path = folder / f"Close-{index + 1:02}-{start}-{end}.mov"
        motion = close_motion(camera, start, end, fps, index, faces)
        corners = []
        for frame, zoom, pan, tilt in motion:
            height = fit_height * zoom
            width = height * camera.width / camera.height
            cx = .5 - pan * fit_height / (100 * width)
            cy = .5 + tilt * fit_height / (100 * height)
            half_w, half_h = 540 / width, 960 / height
            corners.append((frame - start, 1080 * (cx - half_w), 1080 * (cx + half_w),
                            1920 * (cy - half_h), 1920 * (cy + half_h)))

        def linear(column):
            terms = [f"{corners[0][column]:.5f}"]
            for a, b in zip(corners, corners[1:]):
                delta = b[column] - a[column]
                if abs(delta) > .00001:
                    terms.append(f"({delta:.5f})*clip((on-1-{a[0]})/{b[0]-a[0]},0,1)")
            return "'(" + "+".join(terms) + ")'"

        left, right, top, bottom = (linear(i) for i in range(1, 5))
        frames = end - start
        filters = (f"fps={fps}:start_time=0,tpad=stop_mode=clone:stop_duration=0.1,"
                   f"trim=end_frame={frames},scale=1080:1920:flags=bicubic,setsar=1,"
                   f"perspective=sense=source:eval=frame:x0={left}:x1={right}:x2={left}:x3={right}:"
                   f"y0={top}:y1={top}:y2={bottom}:y3={bottom}")
        graph = output_path.with_suffix(".filter")
        graph.write_text(filters, encoding="ascii")
        seconds = float((end - start) / fps)
        try:
            run("ffmpeg", "-v", "error", "-y", "-threads", "2", "-filter_threads", "2",
                "-ss", f"{float(start/fps):.9f}", "-i", camera.path, "-map", "0:v:0",
                "-map", "0:a:0", "-af", f"apad,atrim=duration={seconds:.9f}",
                "-t", f"{seconds:.9f}",
                "-filter_script:v", graph, "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "18", "-bf", "0", "-g", max(1, round(fps / 2)), "-threads:v", "2",
                "-c:a", "pcm_s16le", output_path)
            media = probe(output_path, 0)
            if abs(media.duration - seconds) >= .9 / float(fps):
                raise ValueError(f"O close {index+1} ficou com duração diferente da prevista.")
            return (start, end), media
        finally:
            graph.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(render, i, a, b) for i, (a, b) in enumerate(closes)]
        return dict(job.result() for job in jobs)


def fcpxml(camera, reference, segments, offset, fps, title, obs_composite=False, ref_gain=-8,
           close_media=None):
    def time(frames):
        return rational(Fraction(frames) / fps)

    root = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r1", name="FFVideoFormatRateUndefined",
                  width="1080", height="1920", frameDuration=time(1))
    close_media = close_media or {}
    close_ids = {bounds: f"r{i * 2 + 1}" for i, bounds in enumerate(close_media, 3)}
    for i, media in enumerate((camera, reference, *close_media.values()), 1):
        if media is None:
            continue
        fmt = f"r{i * 2}"
        ET.SubElement(resources, "format", id=fmt, name="FFVideoFormatRateUndefined",
                      width=str(media.width), height=str(media.height),
                      frameDuration=rational(1 / media.fps))
        asset = ET.SubElement(resources, "asset", id=f"r{i * 2 + 1}", name=media.path.stem,
                              start="0s", duration=rational(Fraction(str(media.duration))),
                              hasVideo="1", format=fmt)
        if media.channels:
            asset.attrib.update(hasAudio="1", audioSources="1", audioChannels=str(media.channels))
        ET.SubElement(asset, "media-rep", kind="original-media", src=media.path.as_uri())

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="CripituLab")
    project = ET.SubElement(event, "project", name=title)
    total = sum(b - a for a, b, _, _ in segments)
    sequence = ET.SubElement(project, "sequence", format="r1", duration=time(total),
                             tcStart="0s", tcFormat="NDF", audioLayout="stereo", audioRate="48k")
    spine = ET.SubElement(sequence, "spine")
    for start, end, record, split in segments:
        baked = close_media.get((start, end)) if not split else None
        if baked:
            ET.SubElement(spine, "asset-clip", name=baked.path.stem, ref=close_ids[(start, end)],
                          offset=time(record), start="0s", duration=time(end - start))
            continue
        clip = ET.SubElement(spine, "asset-clip", name=camera.path.stem, ref="r3",
                             offset=time(record), start=time(start), duration=time(end - start))
        if split and obs_composite:
            clip.set("srcEnable", "audio")
        else:
            fit = min(1080 / camera.width, 1920 / camera.height)
            if split:
                zoom = max(1080 / camera.width, 768 / camera.height)
                y = (-192 - camera.height * zoom / 2) / (camera.height * fit) * 100
            else:
                zoom = max(1080 / camera.width, 1920 / camera.height)
                y = 0
            transform(clip, zoom / fit, y)
        if split:
            # Connected offsets use the parent clip's source clock.
            child = ET.SubElement(clip, "asset-clip", name=reference.path.stem, ref="r5",
                                  lane="1", offset=time(start), start=time(start - offset),
                                  duration=time(end - start))
            fit = min(1080 / reference.width, 1920 / reference.height)
            if not obs_composite:
                zoom = min(1080 / reference.width, 1152 / reference.height)
                transform(child, zoom / fit, 384 / (reference.height * fit) * 100)
            if reference.channels:
                ET.SubElement(child, "adjust-volume", amount=f"{ref_gain:g}dB")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode")


def transform(clip, scale, position):
    ET.SubElement(clip, "adjust-transform", position=f"0 {position:.7f}",
                  scale=f"{scale:.7f} {scale:.7f}")


def rational(value):
    return f"{value.numerator}/{value.denominator}s" if value.denominator != 1 else f"{value.numerator}s"


def subtitles(path, segments, fps, voice_delay=0):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = data.get("originalSegments") or data.get("segments") or []
    words = []
    for entry in entries:
        for word in entry.get("words", []):
            label = (word.get("word") or word.get("text") or "").strip()
            if label:
                words.append((float(word["start"]) + voice_delay, float(word["end"]) + voice_delay, label))
    if not words:
        raise ValueError("O JSON do AutoSubs precisa conter tempos por palavra do microfone bruto.")

    def stamp(ms):
        hours, remainder = divmod(ms, 3600000)
        minutes, remainder = divmod(remainder, 60000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

    blocks = []
    for begin, end, label in sorted(words):
        for a, b, record, _ in segments:
            start = max(begin, a / fps)
            finish = min(end, b / fps)
            if finish - start < .045:
                continue
            shift = (record - a) / fps
            t0, t1 = round((start + shift) * 1000), round((finish + shift) * 1000)
            blocks.append(f"{len(blocks) + 1}\n{stamp(t0)} --> {stamp(t1)}\n{label}\n")
    if not blocks:
        raise ValueError("As palavras do JSON não coincidem com os trechos da câmera.")
    return "\n".join(blocks)


def prepare_edit_media(media, destination, optimized, voice_delay=0):
    command = ["-v", "error", "-n", "-threads", "2", "-i", media.path, "-map", "0:v:0"]
    if media.channels:
        command += ["-map", f"0:a:{media.audio_index}", "-c:a", "pcm_s16le", "-ar", "48000"]
        if voice_delay:
            samples = round(abs(voice_delay) * 48000)
            shift = (f"adelay={samples}S:all=1" if voice_delay > 0 else
                     f"atrim=start_sample={samples},asetpts=PTS-STARTPTS")
            command += ["-af", f"aresample=48000,{shift},apad,atrim=duration={media.duration:.8f}"]
    if optimized:
        # Intra-frame media keeps seeks at every cut inexpensive.
        frame_count = round(media.duration * media.fps)
        command += ["-c:v", "dnxhd", "-profile:v", "dnxhr_sq", "-pix_fmt", "yuv422p",
                    "-vf", "tpad=stop_mode=clone:stop_duration=0.1", "-frames:v", str(frame_count),
                    "-r", str(media.fps), "-fps_mode", "cfr", "-threads:v", "2"]
    else:
        command += ["-c:v", "copy"]
    run("ffmpeg", *command, destination)
    result = probe(destination, 0)
    if abs(result.duration - media.duration) > 1 / float(media.fps) + .001:
        raise ValueError(f"A mídia preparada perdeu a duração original: {media.path.name}")
    return result


def write_cut(args, review=False):
    args = argparse.Namespace(**vars(args))
    known_obs_pair = (args.reference and Path(args.camera).parent.name.upper() == "WEBCAM"
                      and Path(args.reference).parent.name.upper() == "TELACOMPLETA")
    args.obs_composite = bool(args.obs_composite or known_obs_pair)
    args.mic_db = args.mic_db if args.mic_db is not None else (-44 if args.obs_composite else -35)
    args.ref_db = args.ref_db if args.ref_db is not None else (-55 if args.obs_composite else -42)
    args.ref_gain = args.ref_gain if args.ref_gain is not None else (0 if args.obs_composite else -8)
    args.voice_delay_ms = getattr(args, "voice_delay_ms", None)
    voice_is_auto = args.voice_delay_ms is None
    optimize = bool(getattr(args, "optimize_media", False))
    sync_note = "manual ou início comum"
    if not all(math.isfinite(value) for value in (args.offset, args.pause, args.margin, args.close, args.ref_gain)
               if value is not None):
        raise ValueError("Use valores numéricos finitos.")
    if not -90 < args.mic_db < 0 or not -90 < args.ref_db < 0:
        raise ValueError("Os limiares do áudio precisam ficar entre -90 e 0 dB.")
    if args.pause < .1 or args.margin < 0 or args.close < .2:
        raise ValueError("Valores inválidos para pausa, margem ou duração de close.")
    with ThreadPoolExecutor(max_workers=2) as pool:
        camera_job = pool.submit(probe, args.camera, args.mic_track)
        ref_job = pool.submit(probe, args.reference, args.ref_track or 0) if args.reference else None
        camera = camera_job.result()
        reference = ref_job.result() if ref_job else None
        if not camera.channels:
            raise ValueError("A gravação da câmera precisa conter o áudio do microfone.")
        if voice_is_auto:
            args.voice_delay_ms = calibrated_delay(camera.path)
        if not math.isfinite(args.voice_delay_ms) or abs(args.voice_delay_ms) > 1000:
            raise ValueError("A correção do microfone deve ficar entre −1000 e +1000 ms.")
        voice_delay = args.voice_delay_ms / 1000
        if reference and args.obs_composite and (args.offset is None or args.ref_track is None):
            measured, track, shared, confidence = audio_sync(camera, reference, args.ref_track)
            if args.offset is None:
                args.offset = measured
                sync_note = f"automático, microfone compartilhado na faixa {shared}, correlação {confidence:.3f}"
            if args.ref_track is None:
                args.ref_track = track
                reference = probe(args.reference, track)
        args.offset = args.offset if args.offset is not None else 0.0
        args.ref_track = args.ref_track if args.ref_track is not None else 0
        detection_pause = min(.22, args.pause)
        mic_job = pool.submit(active, camera, args.mic_db, detection_pause)
        ref_job = pool.submit(active, reference, args.ref_db, detection_pause) if reference else None
        mic = mic_job.result()
        ref = ref_job.result() if ref_job else []
    if not mic:
        raise ValueError("Nenhuma fala detectada. Confira a faixa do microfone ou use um limiar mais baixo.")
    mic = merge((max(0, start + voice_delay), min(camera.duration, end + voice_delay))
                for start, end in mic)
    fps = Fraction(args.fps) if args.fps else camera.fps
    if fps <= 0 or fps > 120:
        raise ValueError("FPS precisa estar entre 0 e 120.")
    segments, offset = plan(camera, reference, mic, ref, args, fps)
    if not segments:
        raise ValueError("A mídia é curta demais para gerar uma timeline.")
    out = Path(args.output).resolve()
    if out.suffix.lower() != ".fcpxml":
        raise ValueError("O arquivo de saída precisa terminar em .fcpxml.")
    companion = out.with_suffix(".txt")
    captions = out.with_suffix(".srt")
    if out.exists() or companion.exists() or (args.captions and captions.exists()):
        raise ValueError("Já existe uma saída com esse nome. Escolha outro para manter as duas versões.")
    faces = track_face(camera) if getattr(args, "track_face", True) else None
    total = sum(b - a for a, b, _, _ in segments)
    close_count = sum(not split for _, _, _, split in segments)
    report = [f"CripituLab — {out.stem}", f"Câmera: {camera.path}",
              f"Referência: {reference.path if reference else 'sem referência'}",
              f"Faixa da referência: {args.ref_track} | Tela dividida do OBS: {args.obs_composite}",
              f"Sincronismo: {args.offset:+.3f}s ({sync_note}) | FPS: {fps} | mic analisado: {args.mic_track}",
              f"Correção do microfone: {args.voice_delay_ms:+g} ms"
              f" ({'calibração do arquivo' if voice_is_auto and voice_delay else 'manual' if not voice_is_auto else 'sem calibração'})"
              f" | Mídia otimizada (DNxHR SQ): {optimize}",
              f"Bruto: {camera.duration:.2f}s | Timeline: {total / fps:.2f}s",
              f"Tempo removido: {max(0, camera.duration - total/fps):.2f}s | Pausa mínima: {args.pause:.2f}s",
              f"Rastreamento de rosto: {len(faces)} amostras" if faces is not None else "Rastreamento: desligado",
              f"{len(segments)} trechos | {close_count} em câmera cheia", "",
              "Timeline (s) | Câmera original (s) | Enquadramento"]
    for a, b, record, split in segments:
        report.append(f"{record / fps:8.2f} | {a / fps:8.2f}–{b / fps:8.2f} | "
                      + ("Tela dividida" if split else "Câmera cheia"))
    srt = subtitles(args.captions, segments, fps, voice_delay) if args.captions else None
    out.parent.mkdir(parents=True, exist_ok=True)
    prepare_camera = optimize or voice_delay or camera.audio_count > 1
    prepare_reference = reference and (optimize or reference.audio_count > 1)
    if prepare_camera or prepare_reference:
        media_dir = out.parent / f"{out.stem} - mídia"
        media_dir.mkdir()
        camera_copy = media_dir / f"{out.stem} - Webcam.mov"
        reference_copy = media_dir / f"{out.stem} - OBS.mov"
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                cam_job = (pool.submit(prepare_edit_media, camera, camera_copy, optimize, voice_delay)
                           if prepare_camera else None)
                ref_job = (pool.submit(prepare_edit_media, reference, reference_copy, optimize)
                           if prepare_reference else None)
                if cam_job:
                    camera = cam_job.result()
                if ref_job:
                    reference = ref_job.result()
        except (OSError, ValueError, KeyError):
            camera_copy.unlink(missing_ok=True)
            reference_copy.unlink(missing_ok=True)
            media_dir.rmdir()
            raise
    baked = render_closes(camera, segments, fps, faces, out) if faces is not None and not review else None
    xml = fcpxml(camera, reference, segments, offset, fps, out.stem,
                 obs_composite=args.obs_composite, ref_gain=args.ref_gain, close_media=baked)
    with out.open("x", encoding="utf-8") as file:
        file.write(xml)
    with companion.open("x", encoding="utf-8") as file:
        file.write("\n".join(report) + "\n")
    if srt:
        with captions.open("x", encoding="utf-8") as file:
            file.write(srt)
    summary = (f"{len(segments)} trechos, {close_count} em câmera cheia, {total / fps:.1f}s. "
               f"Sincronismo: {args.offset:+.3f}s. Voz: {args.voice_delay_ms:+g} ms.")
    if review:
        return summary, (out, camera, reference, segments, offset, fps, args, faces)
    return f"{out}\n{summary}"


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("camera", help="Gravação com câmera e microfone")
    cli.add_argument("--reference", help="Gravação do vídeo reagido sincronizada com a câmera")
    cli.add_argument("--offset", type=float, help="Segundo da câmera em que a referência começa. OBS: automático")
    cli.add_argument("--fps", help="Por padrão, usa a taxa da webcam")
    cli.add_argument("--mic-track", type=int, default=0, help="Faixa usada para detectar fala, começando em 0")
    cli.add_argument("--mic-db", type=float)
    cli.add_argument("--ref-db", type=float)
    cli.add_argument("--ref-track", type=int, help="Faixa da referência, contando a partir de 0. OBS: automática")
    cli.add_argument("--ref-gain", type=float, help="Ajuste de volume da referência em dB")
    cli.add_argument("--voice-delay-ms", type=float,
                     help="Correção da voz: positivo atrasa a voz; vazio usa calibração conhecida")
    cli.add_argument("--optimize-media", action=argparse.BooleanOptionalAction, default=False,
                     help="Gera mídia DNxHR para edição (opcional)")
    cli.add_argument("--track-face", action=argparse.BooleanOptionalAction, default=True,
                     help="Centraliza o rosto e varia o zoom nos closes")
    cli.add_argument("--obs-composite", action="store_true", help="A captura já contém a tela dividida do OBS")
    cli.add_argument("--pause", type=float, default=.45)
    cli.add_argument("--margin", type=float, default=.09)
    cli.add_argument("--close", type=float, default=2.5)
    cli.add_argument("--keep-pauses", action="store_true")
    cli.add_argument("--captions", help="JSON AutoSubs por palavra, do microfone antes dos cortes")
    cli.add_argument("-o", "--output", required=True)
    return cli


def resolve_sources(snapshot):
    root = ET.parse(snapshot).getroot()
    sequence = root.find("sequence")
    if sequence is None:
        sequence = root.find(".//sequence")
    if sequence is None:
        raise ValueError("O XML do Resolve não contém uma timeline.")
    paths = {file.get("id"): file.findtext("pathurl") for file in root.iter("file")
             if file.get("id") and file.findtext("pathurl")}
    sources = {}
    for number, track in enumerate(sequence.findall("./media/video/track"), 1):
        for item in track.findall("clipitem"):
            file = item.find("file")
            if file is None:
                continue
            raw = file.findtext("pathurl") or paths.get(file.get("id"))
            if not raw:
                continue
            uri = urlsplit(raw)
            if uri.scheme == "file":
                raw = unquote(uri.path)
                if uri.netloc.lower() not in ("", "localhost"):
                    raw = f"//{uri.netloc}{raw}"
                elif re.match(r"^/[A-Za-z]:/", raw):
                    raw = raw[1:]
            path = Path(raw)
            if path.is_file():
                sources.setdefault(path, set()).add(number)
    if not sources:
        raise ValueError("Nenhum arquivo de vídeo disponível na timeline ativa.")
    return sequence.findtext("name", "Timeline"), sources


def original_pair(sources):
    pairs = set()
    for path in sources:
        folder = path.parent
        if folder.name == "Closes":
            folder = folder.parent
        if not folder.name.endswith(" - mídia"):
            continue
        report = folder.with_name(folder.name.removesuffix(" - mídia") + ".txt")
        if not report.is_file():
            continue
        lines = report.read_text(encoding="utf-8").splitlines()
        if not lines or not lines[0].startswith("CripituLab — "):
            continue
        camera = next((line.partition(": ")[2] for line in lines if line.startswith("Câmera: ")), "")
        source = next((line.partition(": ")[2] for line in lines if line.startswith("Referência: ")), "")
        if camera and source and Path(camera).is_file() and Path(source).is_file():
            pairs.add((Path(camera), Path(source)))
    return next(iter(pairs)) if len(pairs) == 1 else None


def resolve_command(mailbox, action, path=""):
    ticket = uuid.uuid4().hex
    request = Path(f"{mailbox}.request")
    temporary = request.with_suffix(f".{ticket}.tmp")
    temporary.write_text(f"{ticket}\n{action}\n{path}\n", encoding="utf-8", newline="\n")
    temporary.replace(request)
    reply = Path(f"{mailbox}.{ticket}")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if reply.exists():
            kind, _, message = reply.read_text(encoding="utf-8", errors="replace").partition("\n")
            if message:
                if kind == "ok":
                    return message
                raise ValueError(message)
        if Path(f"{mailbox}.closed").exists():
            raise ValueError("A conexão encerrou. Abra o CripituLab novamente pelo menu do Resolve.")
        time.sleep(.15)
    raise ValueError("O Resolve não confirmou o envio. Confira o Media Pool antes de tentar de novo.")


def gui(snapshot=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("CripituLab • Primeiro corte")
    root.configure(background="#14202a")
    panel = ttk.Frame(root, padding=18)
    panel.pack(side="left", fill="both", expand=True)
    panel.columnconfigure(1, weight=1)
    review = ttk.Frame(root, padding=18)
    review.pack(side="right", fill="both", expand=True)
    ttk.Label(review, text="Revisão do primeiro corte", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    strip = tk.Canvas(review, background="#14202a", height=30, width=355, highlightthickness=0)
    strip.pack(fill="x", pady=(10, 6))
    table = ttk.Treeview(review, columns=("time", "duration", "mode"), show="headings", height=12,
                         selectmode="browse")
    for key, label, width in (("time", "Início", 75), ("duration", "Duração", 70),
                              ("mode", "Enquadramento", 180)):
        table.heading(key, text=label)
        table.column(key, width=width, anchor="w")
    table.pack(fill="both", expand=True)
    actions = ttk.Frame(review)
    actions.pack(fill="x", pady=8)
    draft = None
    omitted = set()
    overrides = {}
    mailbox = Path(f"{snapshot}.bridge") if snapshot else None
    sent = False
    values = {}
    fields = [
        ("camera", "Câmera + microfone", ""),
        ("reference", "Vídeo reagido gravado junto", ""),
        ("output", "Salvar timeline", ""),
        ("captions", "JSON do AutoSubs (opcional)", ""),
        ("offset", "Sincronismo (vazio = automático no OBS)", ""),
        ("fps", "FPS (vazio = usa a webcam)", ""),
        ("mic_db", "Limiar do microfone (dB)", ""),
        ("ref_db", "Limiar do vídeo reagido (dB)", ""),
        ("pause", "Silêncio mínimo para cortar (s)", "0.45"),
        ("close", "Fala mínima para close (s)", "2.5"),
        ("ref_track", "Faixa da referência (vazio = automática no OBS)", ""),
        ("ref_gain", "Ajuste de volume da referência (dB)", ""),
        ("voice_delay_ms", "Correção da voz (ms, vazio = calibração)", ""),
    ]

    def choose(key):
        path = (filedialog.asksaveasfilename(defaultextension=".fcpxml",
                                            filetypes=[("Timeline Resolve", "*.fcpxml")])
                if key == "output" else filedialog.askopenfilename(filetypes=(
                    [("Transcrição AutoSubs", "*.json")] if key == "captions" else
                    [("Vídeos", "*.mp4 *.mov *.mkv *.m4v"), ("Todos", "*.*")])))
        if path:
            values[key].set(path)

    for row, (key, label, default) in enumerate(fields):
        values[key] = tk.StringVar(value=default)
        ttk.Label(panel, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(panel, textvariable=values[key], width=48).grid(row=row, column=1, sticky="ew")
        if row < 4:
            ttk.Button(panel, text="Escolher…", command=lambda k=key: choose(k)).grid(
                row=row, column=2, padx=(8, 0))
    keep = tk.BooleanVar()
    composite = tk.BooleanVar()
    optimized = tk.BooleanVar(value=False)
    ttk.Checkbutton(panel, text="A captura já tem a tela dividida do OBS",
                    variable=composite).grid(row=13, column=1, sticky="w")
    ttk.Checkbutton(panel, text="Manter pausas", variable=keep).grid(row=14, column=1, sticky="w")
    ttk.Checkbutton(panel, text="Otimizar mídia para reprodução",
                    variable=optimized).grid(row=15, column=1, sticky="w")
    status = tk.StringVar(value="A referência precisa acompanhar as pausas da gravação.")
    ttk.Label(panel, textvariable=status, wraplength=580).grid(row=17, column=0, columnspan=3, sticky="w")
    workers = ThreadPoolExecutor(max_workers=1)

    def display_time(seconds):
        minutes, remainder = divmod(seconds, 60)
        return f"{int(minutes):02}:{remainder:04.1f}"

    def refresh_review():
        strip.delete("all")
        table.delete(*table.get_children())
        if not draft:
            return
        _, _, _, segments, _, fps, _, _ = draft
        length = sum(b - a for a, b, _, _ in segments)
        width = max(355, strip.winfo_width())
        cursor = 0
        for i, (a, b, record, split) in enumerate(segments):
            mode = overrides.get(i, split)
            muted = i in omitted
            table.insert("", "end", iid=str(i), values=(
                "—" if muted else display_time(cursor / fps), f"{(b-a)/fps:.1f} s",
                "Omitido" if muted else ("Tela dividida" if mode else "Close"),
            ))
            color = "#485766" if muted else ("#43b7ad" if mode else "#efa75f")
            rectangle = strip.create_rectangle(width*record/length, 4, width*(record+b-a)/length,
                                                26, fill=color, outline="#14202a")
            strip.tag_bind(rectangle, "<Button-1>", lambda _, index=i: select(index))
            if not muted:
                cursor += b - a

    def select(index):
        table.selection_set(str(index))
        table.see(str(index))

    def revise(change_mode):
        if not draft or not table.selection() or sent:
            return
        index = int(table.selection()[0])
        _, _, reference, segments, offset, fps, _, _ = draft
        a, b, _, split = segments[index]
        if change_mode:
            if not reference or a < offset or b > offset + math.floor(reference.duration * fps):
                status.set("A captura do OBS não cobre esse trecho da webcam.")
                return
            overrides[index] = not overrides.get(index, split)
        elif index in omitted:
            omitted.remove(index)
        else:
            omitted.add(index)
        refresh_review()
        select(index)

    def connected():
        return (mailbox is not None and Path(f"{mailbox}.ready").exists()
                and not Path(f"{mailbox}.closed").exists())

    def send():
        if not draft or sent:
            return
        if not connected():
            status.set("Abra pelo menu Workspace → Scripts → CripituLab para enviar ao Resolve.")
            return
        out, camera, reference, segments, offset, fps, args, faces = draft
        revised = []
        cursor = 0
        for i, (a, b, _, split) in enumerate(segments):
            if i not in omitted:
                revised.append((a, b, cursor, overrides.get(i, split)))
                cursor += b-a
        if not revised:
            status.set("Mantenha pelo menos um trecho para enviar ao Resolve.")
            return
        def deliver():
            if revised != segments or faces is not None:
                baked = render_closes(camera, revised, fps, faces, out) if faces is not None else None
                xml = fcpxml(camera, reference, revised, offset, fps, out.stem,
                             obs_composite=args.obs_composite, ref_gain=args.ref_gain, close_media=baked)
                temp = out.with_suffix(".review.tmp")
                temp.write_text(xml, encoding="utf-8")
                temp.replace(out)
                report_path = out.with_suffix(".txt")
                header = report_path.read_text(encoding="utf-8").split("\nTimeline (s)", 1)[0]
                header = header.split("\nRevisão:", 1)[0].rstrip()
                rows = [f"{record/fps:8.2f} | {a/fps:8.2f}–{b/fps:8.2f} | "
                        + ("Tela dividida" if split else "Câmera cheia")
                        for a, b, record, split in revised]
                report_path.write_text(
                    f"{header}\nRevisão: {len(revised)} trechos, {cursor/fps:.2f}s.\n\n"
                    + "Timeline (s) | Câmera original (s) | Enquadramento\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")
                if args.captions:
                    out.with_suffix(".srt").write_text(
                        subtitles(args.captions, revised, fps, args.voice_delay_ms / 1000), encoding="utf-8")
            return resolve_command(mailbox, "send", out)

        def delivered(name):
            nonlocal sent
            sent = True
            send_button.state(["disabled"])
            status.set(f"Timeline enviada ao Resolve · {name}")
            root.after(450, close_window)

        perform(deliver, "Preparando os closes e enviando a montagem ao Resolve…", delivered)

    ttk.Button(actions, text="Alternar close / dividida", command=lambda: revise(True)).pack(side="left")
    ttk.Button(actions, text="Omitir / restaurar", command=lambda: revise(False)).pack(side="left", padx=6)
    send_button = ttk.Button(review, text="Enviar ao Resolve", command=send)
    send_button.pack(fill="x", pady=(2, 0))
    send_button.state(["disabled"])

    def load_resolve(path=None):
        nonlocal mailbox
        if path is None and connected():
            perform(lambda: resolve_command(mailbox, "read"), "Lendo a timeline ativa…",
                    lambda result: load_resolve(Path(result)))
            return
        try:
            if path is None:
                path = max((p for p in RESOLVE_SNAPSHOTS.glob("timeline-*.xml")
                            if re.fullmatch(r"timeline-\d+-\d+\.xml", p.name)),
                           key=lambda item: item.stat().st_mtime, default=None)
            if path is None:
                raise ValueError("No Resolve, use Workspace → Scripts → CripituLab primeiro.")
            title, sources = resolve_sources(path)
            candidate = Path(f"{path}.bridge")
            if Path(f"{candidate}.ready").exists() and not Path(f"{candidate}.closed").exists():
                mailbox = candidate
        except (OSError, ValueError, ET.ParseError) as error:
            messagebox.showerror("CripituLab", str(error))
            return
        files = list(sources)
        recovered = original_pair(sources)
        if recovered:
            apply_sources(*recovered, title)
            status.set(f"Brutos de {title} recuperados. Pronto para gerar uma nova montagem.")
            return
        webcams = [p for p in files if p.parent.name.upper() == "WEBCAM"]
        captures = [p for p in files if p.parent.name.upper() == "TELACOMPLETA"]
        if len(webcams) == len(captures) == 1:
            apply_sources(webcams[0], captures[0], title)
            return

        dialog = tk.Toplevel(root)
        dialog.title("Escolher clipes da timeline")
        dialog.transient(root)
        dialog.grab_set()
        labels = [
            f"{', '.join(f'V{n}' for n in sorted(sources[p]))} | {p.parent.name} | {p.name}"
            for p in files
        ]
        selections = []
        for row, label in enumerate(("Câmera + microfone", "Vídeo reagido")):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=10, pady=8, sticky="w")
            options = labels + (["Sem referência"] if row else [])
            combo = ttk.Combobox(dialog, values=options, state="readonly", width=60)
            combo.grid(row=row, column=1, padx=10, pady=8)
            combo.current(min(row, len(options) - 1))
            selections.append(combo)

        def confirm_sources():
            cam, ref = [selection.current() for selection in selections]
            if cam < 0 or ref < 0 or cam == ref:
                messagebox.showerror("CripituLab", "Escolha clipes diferentes para cada função.", parent=dialog)
                return
            apply_sources(files[cam], files[ref] if ref < len(files) else None, title)
            dialog.destroy()

        ttk.Button(dialog, text="Usar clipes", command=confirm_sources).grid(row=2, column=1, pady=12)

    def apply_sources(camera, reference, title):
        values["camera"].set(str(camera))
        values["reference"].set(str(reference) if reference else "")
        if not values["output"].get():
            label = f"{camera.stem} - CripituLab"
            prefix = re.sub(r'[<>:"/\\|?*]', "_", label).strip()
            folder = PROJECT_DIR / "renders"
            destination = folder / f"{prefix}.fcpxml"
            version = 2
            while destination.exists():
                destination = folder / f"{prefix} {version}.fcpxml"
                version += 1
            values["output"].set(str(destination))
        status.set(f"Clipes carregados de {title}. Pronto para gerar a timeline.")

    def start():
        data = {key: variable.get().strip() for key, variable in values.items()}
        if not data["camera"] or not data["output"]:
            messagebox.showerror("CripituLab", "Escolha a câmera e o destino da timeline.")
            return
        try:
            for key in ("offset", "mic_db", "ref_db", "pause", "close", "ref_gain", "voice_delay_ms"):
                data[key] = float(data[key].replace(",", ".")) if data[key] else None
                if data[key] is not None and not math.isfinite(data[key]):
                    raise ValueError()
            data["pause"] = data["pause"] if data["pause"] is not None else .45
            data["close"] = data["close"] if data["close"] is not None else 2.5
            data["ref_track"] = int(data["ref_track"]) if data["ref_track"] else None
        except ValueError:
            messagebox.showerror("CripituLab", "Confira os campos numéricos.")
            return
        args = argparse.Namespace(**data, mic_track=0, margin=.09, keep_pauses=keep.get(),
                                  obs_composite=composite.get(), optimize_media=optimized.get())
        def generated(result):
            nonlocal draft, sent
            label, draft = result
            sent = False
            omitted.clear()
            overrides.clear()
            refresh_review()
            if connected():
                send_button.state(["!disabled"])
            status.set(label)

        perform(lambda: write_cut(args, review=True), "Sincronizando e preparando a mídia de edição…", generated)

    def perform(work, description, completed):
        job = workers.submit(work)
        button.state(["disabled"])
        send_button.state(["disabled"])
        read_button.state(["disabled"])
        status.set(description)

        def check():
            if not job.done():
                root.after(200, check)
                return
            button.state(["!disabled"])
            read_button.state(["!disabled"])
            if draft and connected() and not sent:
                send_button.state(["!disabled"])
            try:
                completed(job.result())
            except (ValueError, OSError, KeyError, ZeroDivisionError) as error:
                status.set(str(error))
                messagebox.showerror("CripituLab", str(error))

        check()

    button = ttk.Button(panel, text="Gerar timeline", command=start)
    button.grid(row=16, column=1, sticky="w", pady=12)
    read_button = ttk.Button(panel, text="Ler timeline do Resolve", command=load_resolve)
    read_button.grid(row=16, column=0, sticky="w", pady=12)
    def close_window():
        if connected():
            Path(f"{mailbox}.stop").touch()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", close_window)
    if snapshot:
        root.after(0, lambda: load_resolve(snapshot))
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        gui()
    elif len(sys.argv) == 3 and sys.argv[1] == "--resolve-timeline":
        gui(Path(sys.argv[2]))
    else:
        try:
            print(write_cut(parser().parse_args()))
        except (ValueError, OSError, KeyError, ZeroDivisionError) as error:
            raise SystemExit(f"CripituLab: {error}") from error
