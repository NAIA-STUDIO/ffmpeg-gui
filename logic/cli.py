# logic/cli.py
"""
Puente headless (sin PyQt) entre el frontend Electron y ffmpeg_logic.py.

Protocolo:
  - Se lee una linea de JSON por stdin con la peticion:
        {"operation": "<nombre>", "params": {...}}
  - Mientras se ejecuta, un hilo en segundo plano sigue leyendo stdin
    esperando una linea {"cancel": true} para cancelar el proceso ffmpeg.
  - Se escriben lineas de JSON por stdout (una por linea, con flush):
        {"type": "progress", "percent": int}
        {"type": "pair_progress", "percent": int, "pairIndex": int, "pairsTotal": int, "label": str}
        {"type": "pair_done", "pairIndex": int, "pairsTotal": int, "success": bool,
         "output": str|null, "error": str|null, "label": str}
        {"type": "result", "success": bool, "output": str|null, "error": str|null,
         "cancelled": bool, "pairs": [...] (solo merge_auto)}
"""

import sys
import os
import re
import json
import threading
import subprocess

# El frontend (Electron/Node) escribe la peticion JSON en stdin codificada como
# UTF-8. En Windows, sys.stdin/stdout usan por defecto la codificacion local
# (cp1252), lo que corrompe caracteres no ASCII de las rutas (p.ej. la "N" de
# CAMPANAS se convertia en "A'"), provocando errores "no se encuentra la ruta".
# Forzamos UTF-8 en ambos streams para leer y escribir sin perdidas.
for _stream in (sys.stdin, sys.stdout):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import ffmpeg_logic as logic

CREATE_NO_WINDOW = 0x08000000 if sys.platform.startswith("win") else 0

FRAME_RE = re.compile(r"frame=\s*(\d+)")


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def get_fps(video_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        output = subprocess.check_output(cmd, universal_newlines=True).strip()
        if "/" in output:
            num, den = output.split("/")
            den = float(den)
            return float(num) / den if den else 0.0
        return float(output)
    except Exception:
        return 0.0


def estimate_total_frames(video_path, fallback=100):
    duration = logic.get_video_duration(video_path)
    fps = get_fps(video_path)
    total = int(duration * fps)
    return total if total > 0 else fallback


class CancelWatcher:
    """Escucha stdin en un hilo aparte esperando una peticion de cancelacion."""

    def __init__(self):
        self.cancelled = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    def attach(self, proc):
        with self._lock:
            self._proc = proc
            if self.cancelled.is_set():
                self._kill()

    def _kill(self):
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass

    def start(self):
        thread = threading.Thread(target=self._listen, daemon=True)
        thread.start()

    def _listen(self):
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("cancel"):
                self.cancelled.set()
                with self._lock:
                    self._kill()
                return


def run_ffmpeg(command, output_file, total_frames, watcher, progress_prefix=None):
    """
    Ejecuta un comando ffmpeg, emitiendo progreso, y devuelve (success, error_message).
    progress_prefix, si se indica, es un dict con claves extra a incluir en los eventos
    de progreso (pairIndex, pairsTotal, label) para operaciones por lotes.
    """
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        shell=False,
        creationflags=CREATE_NO_WINDOW,
    )
    watcher.attach(proc)

    while True:
        if watcher.cancelled.is_set():
            break
        line = proc.stderr.readline()
        if not line:
            break
        match = FRAME_RE.search(line)
        if match and total_frames > 0:
            percent = min(100, int(int(match.group(1)) / total_frames * 100))
            payload = {"type": "progress", "percent": percent}
            if progress_prefix:
                payload = {"type": "pair_progress", "percent": percent, **progress_prefix}
            emit(payload)

    proc.wait()
    cancelled = watcher.cancelled.is_set()
    success = (proc.returncode == 0) and not cancelled

    if cancelled and output_file and os.path.exists(output_file):
        try:
            os.remove(output_file)
        except Exception:
            pass

    if success:
        return True, ""
    if cancelled:
        return False, "Cancelado"
    error_output = proc.stderr.read()
    return False, error_output or "Error en FFmpeg."


def op_img2vid(params, watcher):
    folder = params["folder"]
    command, output_file = logic.convert_images_to_video_command(
        folder,
        params.get("fps", "30"),
        params.get("audioPath") or None,
        params.get("format", "mp4 (H.264 8-bit)"),
        params.get("crf", "19"),
        float(params.get("fadeIn", 0) or 0),
        float(params.get("fadeOut", 0) or 0),
        params.get("pixFmt") or None,
        prioritize_audio=bool(params.get("prioritizeAudio", False)),
    )
    if not command:
        return {"success": False, "error": "No se detecto un patron de nombres valido en las imagenes."}

    valid_extensions = (".png", ".jpg", ".jpeg")
    files = sorted(os.listdir(folder))
    total_images = len([f for f in files if f.lower().endswith(valid_extensions)])
    total_frames = total_images or 100

    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_audio_edit(params, watcher):
    mode = params.get("mode", "add")
    video = params["video"]
    output_format = params.get("format", "mp4")

    if mode == "add":
        command, output_file = logic.add_audio_to_video_command(video, params["audio"], output_format)
    elif mode == "replace":
        command, output_file = logic.replace_audio_command(video, params["audio"], output_format)
    else:
        command, output_file = logic.remove_audio_command(video, output_format)

    if not command:
        return {"success": False, "error": "Error al construir el comando FFmpeg."}

    total_frames = estimate_total_frames(video)
    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_cut_video(params, watcher):
    video = params["video"]
    cut_mode = params.get("cutMode", "time")

    if cut_mode == "frames":
        fps_value = float(params.get("fps", "30") or "30")
        start_frames = int(params.get("start", "0") or "0")
        start_time = str(start_frames / fps_value) if fps_value > 0 else "0"
        raw_duration = params.get("duration")
        duration = str(int(raw_duration) / fps_value) if raw_duration else None
        end_time = None
    else:
        start_time = params.get("start", "0") or "0"
        duration = params.get("duration") or None
        end_time = params.get("endTime") or None

    command, output_file = logic.cut_video_command(
        video,
        start_time,
        duration=duration,
        end_time=end_time,
        output_format=params.get("format", "mp4"),
        cut_mode="time",
        fade_in_duration=float(params.get("fadeIn", 0) or 0),
        fade_out_duration=float(params.get("fadeOut", 0) or 0),
    )
    if not command:
        return {"success": False, "error": "Error al construir el comando FFmpeg."}

    total_frames = estimate_total_frames(video)
    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_limit_kbps(params, watcher):
    video = params["video"]
    command, output_file = logic.limit_kps_command(
        video,
        video_bitrate=params.get("bitrate", "57M"),
        maxrate=params.get("maxrate", "60M"),
        output_format=params.get("format", "mp4"),
    )
    if not command:
        return {"success": False, "error": "Error al construir el comando FFmpeg."}

    total_frames = estimate_total_frames(video)
    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_scale_video(params, watcher):
    video = params["video"]
    command, output_file = logic.scale_video_command(
        video,
        params["width"],
        params["height"],
        params.get("preset", "slow"),
        params.get("crf", "19"),
        output_format=params.get("format", "mp4"),
    )
    if not command:
        return {"success": False, "error": "Error al construir el comando FFmpeg."}

    total_frames = estimate_total_frames(video)
    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_crop_video(params, watcher):
    video = params["video"]
    command, output_file = logic.crop_video_command(
        video,
        crop_top=int(params.get("top", 0) or 0),
        crop_bottom=int(params.get("bottom", 0) or 0),
        crop_left=int(params.get("left", 0) or 0),
        crop_right=int(params.get("right", 0) or 0),
        fade_in_duration=float(params.get("fadeIn", 0) or 0),
        fade_out_duration=float(params.get("fadeOut", 0) or 0),
        output_format=params.get("format", "mp4"),
    )
    if not command:
        return {"success": False, "error": "Error al construir el comando FFmpeg."}

    total_frames = estimate_total_frames(video)
    success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_merge_videos(params, watcher):
    videos = params["videos"]
    command, output_file, concat_file, error_message, details = logic.merge_videos_command(
        videos,
        mode=params.get("mode", "fast"),
        output_name=params.get("outputName") or None,
        preset=params.get("preset", "slow"),
        crf=params.get("crf", "19"),
        output_format=params.get("format", "mp4"),
    )
    if not command:
        return {"success": False, "error": error_message or "Error al construir el comando FFmpeg."}

    total_frames = details["total_frames"] or 100
    try:
        success, error = run_ffmpeg(command, output_file, total_frames, watcher)
    finally:
        try:
            if concat_file and os.path.exists(concat_file):
                os.remove(concat_file)
        except Exception:
            pass
    return {"success": success, "output": output_file if success else None, "error": None if success else error}


def op_merge_auto(params, watcher):
    folder1 = params["folder1"]
    folder2 = params["folder2"]
    mode = params.get("mode", "fast")
    preset = params.get("preset", "slow")
    crf = params.get("crf", "19")
    output_format = params.get("format", "mp4")

    pairs, ignored_1, ignored_2, warnings = logic.pair_videos_by_resolution(folder1, folder2)

    if not pairs:
        return {
            "success": False,
            "error": "No se encontraron videos con resolucion coincidente entre ambas carpetas.",
            "pairs": [],
        }

    output_dir = os.path.join(folder1, "merged_by_resolution")
    os.makedirs(output_dir, exist_ok=True)

    results = []
    pairs_total = len(pairs)

    for index, pair_info in enumerate(pairs):
        if watcher.cancelled.is_set():
            break

        output_name = os.path.splitext(os.path.basename(pair_info["video_1"]))[0]
        variant_suffix = " sin logo" if pair_info["variant"] == "sin_logo" else ""
        label = f"Auto {pair_info['resolution']}{variant_suffix}: {output_name}"

        command, output_file, concat_file, error_message, details = logic.merge_videos_command(
            [pair_info["video_1"], pair_info["video_2"]],
            mode=mode,
            output_name=output_name,
            preset=preset,
            crf=crf,
            output_format=output_format,
            output_dir=output_dir,
        )

        prefix = {"pairIndex": index, "pairsTotal": pairs_total, "label": label}

        if not command:
            results.append({"label": label, "success": False, "output": None, "error": error_message})
            emit({"type": "pair_done", "success": False, "output": None, "error": error_message, **prefix})
            continue

        total_frames = details["total_frames"] or 100
        try:
            success, error = run_ffmpeg(command, output_file, total_frames, watcher, progress_prefix=prefix)
        finally:
            try:
                if concat_file and os.path.exists(concat_file):
                    os.remove(concat_file)
            except Exception:
                pass

        result = {
            "label": label,
            "success": success,
            "output": output_file if success else None,
            "error": None if success else error,
        }
        results.append(result)
        emit({"type": "pair_done", **result, "pairIndex": index, "pairsTotal": pairs_total})

    return {
        "success": any(r["success"] for r in results),
        "pairs": results,
        "ignored": {"folder1": len(ignored_1), "folder2": len(ignored_2)},
        "warnings": warnings,
    }


OPERATIONS = {
    "img2vid": op_img2vid,
    "audio_edit": op_audio_edit,
    "cut_video": op_cut_video,
    "limit_kbps": op_limit_kbps,
    "scale_video": op_scale_video,
    "crop_video": op_crop_video,
    "merge_videos": op_merge_videos,
    "merge_auto": op_merge_auto,
}


def main():
    request_line = sys.stdin.readline()
    try:
        request = json.loads(request_line)
    except ValueError:
        emit({"type": "result", "success": False, "error": "Peticion JSON invalida."})
        return

    operation = request.get("operation")
    params = request.get("params", {})
    handler = OPERATIONS.get(operation)

    watcher = CancelWatcher()
    watcher.start()

    if not handler:
        emit({"type": "result", "success": False, "error": f"Operacion desconocida: {operation}"})
        return

    try:
        result = handler(params, watcher)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    result["type"] = "result"
    result.setdefault("cancelled", watcher.cancelled.is_set())
    emit(result)


if __name__ == "__main__":
    main()
