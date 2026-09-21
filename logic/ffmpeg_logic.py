# logic/ffmpeg_logic.py
"""
Módulo que contiene funciones para construir comandos FFmpeg para diversas operaciones:
- Convertir una secuencia de imágenes en un video.
- Agregar audio a un video.
- Cortar un video.
- Recortar un video.
- Unir varios videos.
- Emparejar automáticamente videos por resolución entre dos carpetas.
"""

import os
import re
import sys
import json
import time
import tempfile
import subprocess


VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov"}
SIN_LOGO_MARKERS = ("sin logo", "sin_logo", "sin-logo")


def get_unique_filename(file_path):
    """
    Si el archivo 'file_path' ya existe, genera un nombre único añadiendo
    timestamp en milisegundos. Retorna el nombre de archivo único.
    """
    if not os.path.exists(file_path):
        return file_path

    base, ext = os.path.splitext(file_path)
    timestamp = int(time.time() * 1000)
    new_file_path = f"{base}_{timestamp}{ext}"

    counter = 1
    while os.path.exists(new_file_path):
        new_file_path = f"{base}_{timestamp}_{counter}{ext}"
        counter += 1

    return new_file_path


def detect_image_prefix(folder_path):
    files = sorted([f for f in os.listdir(folder_path) if f.lower() != "thumbs.db"])

    if not files:
        print("[DEBUG] No se encontraron archivos en la carpeta.")
        return None, 0, False, None

    for f in files:
        match = re.match(r"(.+_)(\d{2,})", f)
        if match:
            prefix = match.group(1)
            start_number = int(match.group(2))
            width = len(match.group(2))
            print(
                "[DEBUG] Prefijo detectado en",
                f,
                ":",
                prefix,
                "con ancho:",
                width,
                "y start_number:",
                start_number,
            )
            return prefix, width, True, start_number
        else:
            print("[DEBUG] No se encontró coincidencia en el archivo:", f)

    return None, 0, False, None


def get_audio_duration(audio_path):
    """
    Devuelve la duración en segundos del audio usando ffprobe.
    """
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        audio_path
    ]
    try:
        output = subprocess.check_output(cmd, universal_newlines=True)
        return float(output.strip())
    except Exception as e:
        print("Error obteniendo duración del audio:", e)
        return 0.0


def get_video_duration(video_path):
    """
    Devuelve la duración en segundos del vídeo usando ffprobe.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        output = subprocess.check_output(cmd, universal_newlines=True)
        return float(output.strip())
    except Exception as e:
        print("Error obteniendo duración del vídeo:", e)
        return 0.0


def _subprocess_flags():
    """
    En Windows evita que se abra una consola al lanzar ffprobe desde el .exe.
    """
    return 0x08000000 if sys.platform.startswith("win") else 0


def _parse_frame_rate(rate):
    """
    Convierte '30000/1001' o '30' en float. Devuelve 0.0 si no es válido.
    """
    try:
        if "/" in str(rate):
            num, den = str(rate).split("/", 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(rate)
    except (TypeError, ValueError):
        return 0.0


def probe_video(video_path):
    """
    Obtiene con ffprobe la información necesaria para unir vídeos.

    Retorna un dict con:
        stream_types: tipos de pista en orden, p.ej. ['video', 'audio']
        video: dict con codec, width, height, pix_fmt, fps, time_base (o None)
        audio: dict con codec, sample_rate, channels (o None)
        duration: duración en segundos
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,"
        "avg_frame_rate,r_frame_rate,time_base,sample_rate,channels,duration",
        "-of", "json",
        video_path
    ]
    output = subprocess.check_output(
        cmd, universal_newlines=True, encoding="utf-8", creationflags=_subprocess_flags()
    )
    data = json.loads(output)
    streams = data.get("streams", [])

    video = None
    audio = None
    video_duration = None
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and video is None:
            # Se usa la tasa nominal (r_frame_rate) como fracción para no acumular desfase
            # con tasas como 30000/1001; si es anómala se recurre a la media.
            frame_rate = stream.get("r_frame_rate")
            fps = _parse_frame_rate(frame_rate)
            if not 0 < fps <= 120:
                fps = _parse_frame_rate(stream.get("avg_frame_rate"))
                frame_rate = str(round(fps, 3)) if fps > 0 else "30"
            video = {
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "pix_fmt": stream.get("pix_fmt"),
                "frame_rate": frame_rate,
                "fps": fps or 30.0,
                "time_base": stream.get("time_base"),
            }
            video_duration = stream.get("duration")
        elif codec_type == "audio" and audio is None:
            audio = {
                "codec": stream.get("codec_name"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
            }

    try:
        duration = float(data.get("format", {}).get("duration") or video_duration or 0)
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "stream_types": [s.get("codec_type") for s in streams if s.get("codec_type") in ("video", "audio")],
        "video": video,
        "audio": audio,
        "duration": duration,
    }


def check_fast_merge_compatibility(infos):
    """
    Comprueba si una lista de vídeos (resultado de probe_video) se puede unir
    sin recodificar con el concat demuxer. Para ello todos deben tener exactamente
    las mismas pistas, en el mismo orden y con los mismos parámetros.

    Retorna (True, "") o (False, motivo).
    """
    reference = infos[0]
    for info in infos[1:]:
        if info["stream_types"] != reference["stream_types"]:
            return False, "los vídeos no tienen las mismas pistas (audio/vídeo) o están en distinto orden"
        if info["video"] != reference["video"]:
            return False, "los vídeos tienen distinto códec, resolución, formato de píxel o fps"
        if info["audio"] != reference["audio"]:
            return False, "los vídeos tienen distinto formato de audio"
    return True, ""


def _even(value):
    value = int(value or 0)
    return value - (value % 2)


def build_compatible_merge_command(video_paths, infos, output_file, preset="slow", crf="19"):
    """
    Une N vídeos recodificando con el filtro concat, que funciona aunque los vídeos
    tengan distinta resolución, fps, formato de píxel, orden de pistas o les falte audio:
    - Todos se escalan (sin deformar, con bandas negras si hace falta) a la resolución
      y fps del primer vídeo.
    - Si algún vídeo tiene audio, a los que no lo tengan se les añade silencio de su
      misma duración. Si ninguno tiene audio, la salida no lleva audio.
    """
    reference = infos[0]["video"]
    width = _even(reference["width"])
    height = _even(reference["height"])
    fps = reference["frame_rate"]

    with_audio = any(info["audio"] for info in infos)

    command = ["ffmpeg", "-y"]
    for path in video_paths:
        command.extend(["-i", path])

    filters = []
    concat_inputs = ""
    for i, info in enumerate(infos):
        filters.append(
            f"[{i}:v:0]scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        concat_inputs += f"[v{i}]"

        if with_audio:
            if info["audio"]:
                source = f"[{i}:a:0]"
            else:
                # Silencio con la duración del clip para que el audio no se desincronice
                source = f"anullsrc=r=48000:cl=stereo,atrim=duration={max(info['duration'], 0.1)},"
            filters.append(
                f"{source}aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS[a{i}]"
            )
            concat_inputs += f"[a{i}]"

    audio_flag = 1 if with_audio else 0
    output_labels = "[v][a]" if with_audio else "[v]"
    filters.append(f"{concat_inputs}concat=n={len(infos)}:v=1:a={audio_flag}{output_labels}")

    command.extend(["-filter_complex", ";".join(filters), "-map", "[v]"])
    if with_audio:
        command.extend(["-map", "[a]"])

    command.extend([
        "-c:v", "libx264",
        "-preset", str(preset),
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ])
    if with_audio:
        command.extend(["-c:a", "aac", "-b:a", "192k"])
    command.extend(["-movflags", "+faststart", output_file])

    return command


def get_video_resolution(video_path):
    """
    Devuelve la resolución del vídeo como string 'ANCHOxALTO', por ejemplo '1080x1920'.
    Si falla, devuelve None.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        video_path
    ]
    try:
        output = subprocess.check_output(cmd, universal_newlines=True).strip()
        if not output or "x" not in output:
            return None
        return output
    except Exception as e:
        print("Error obteniendo resolución del vídeo:", e)
        return None


def is_video_file(file_path):
    """
    Devuelve True si la ruta parece corresponder a un vídeo soportado.
    """
    if not os.path.isfile(file_path):
        return False
    return os.path.splitext(file_path)[1].lower() in VIDEO_EXTENSIONS


def get_video_variant(video_path):
    """
    Devuelve la variante lógica del vídeo para emparejado:
    - 'sin_logo' si el nombre contiene indicadores como 'sin logo'
    - 'default' en cualquier otro caso
    """
    name = os.path.basename(video_path).lower()
    if any(marker in name for marker in SIN_LOGO_MARKERS):
        return "sin_logo"
    return "default"


def sanitize_filename_part(text):
    """
    Limpia texto para usarlo como parte segura de un nombre de archivo.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(text))
    return cleaned.strip("_")


def build_auto_merge_output_name(pair_info):
    """
    Construye un nombre de salida para una pareja emparejada automáticamente.
    """
    resolution = sanitize_filename_part(pair_info.get("resolution", "unknown"))
    variant = pair_info.get("variant", "default")
    pair_index = int(pair_info.get("pair_index", 1))

    name = f"merge_{resolution}"
    if variant == "sin_logo":
        name += "_sin_logo"
    if pair_index > 1:
        name += f"_{pair_index}"

    return name


def scan_video_folder_for_matching(folder_path):
    """
    Escanea una carpeta y agrupa vídeos por:
    (resolución, variante)

    Retorna:
        (mapping, ignored)
    donde:
        mapping[(resolution, variant)] = [ruta1, ruta2, ...]
        ignored = vídeos que no se pudieron interpretar
    """
    mapping = {}
    ignored = []

    if not folder_path or not os.path.isdir(folder_path):
        return mapping, ignored

    for entry in sorted(os.listdir(folder_path)):
        full_path = os.path.join(folder_path, entry)

        if not is_video_file(full_path):
            continue

        resolution = get_video_resolution(full_path)
        if not resolution:
            ignored.append(full_path)
            continue

        variant = get_video_variant(full_path)
        key = (resolution, variant)
        mapping.setdefault(key, []).append(full_path)

    return mapping, ignored


def pair_videos_by_resolution(folder_1, folder_2):
    """
    Empareja automáticamente vídeos entre dos carpetas por:
    - resolución
    - variante ('default' o 'sin_logo')

    Si en una carpeta hay más vídeos que en la otra para una misma clave,
    empareja por orden alfabético y deja el resto como ignorados.

    Retorna:
        pairs, ignored_1, ignored_2, warnings

    Cada elemento de pairs es un dict:
    {
        "resolution": "1080x1080",
        "variant": "default" | "sin_logo",
        "video_1": "...",
        "video_2": "...",
        "pair_index": 1
    }
    """
    map_1, ignored_1 = scan_video_folder_for_matching(folder_1)
    map_2, ignored_2 = scan_video_folder_for_matching(folder_2)

    all_keys = sorted(set(map_1.keys()) | set(map_2.keys()))

    pairs = []
    warnings = []

    for key in all_keys:
        resolution, variant = key
        list_1 = sorted(map_1.get(key, []))
        list_2 = sorted(map_2.get(key, []))

        if list_1 and list_2:
            pair_count = min(len(list_1), len(list_2))

            for idx in range(pair_count):
                pairs.append({
                    "resolution": resolution,
                    "variant": variant,
                    "video_1": list_1[idx],
                    "video_2": list_2[idx],
                    "pair_index": idx + 1
                })

            if len(list_1) > pair_count:
                ignored_1.extend(list_1[pair_count:])
                warnings.append(
                    f"Sobran {len(list_1) - pair_count} vídeo(s) en carpeta 1 para {resolution} / {variant}"
                )

            if len(list_2) > pair_count:
                ignored_2.extend(list_2[pair_count:])
                warnings.append(
                    f"Sobran {len(list_2) - pair_count} vídeo(s) en carpeta 2 para {resolution} / {variant}"
                )

        else:
            if list_1:
                ignored_1.extend(list_1)
            if list_2:
                ignored_2.extend(list_2)

    return pairs, ignored_1, ignored_2, warnings


def convert_images_to_video_command(folder_path, fps, audio_path=None, user_format="mp4 (H.264 8-bit)",
                                    crf="19", fade_in_duration=1, fade_out_duration=1, pix_fmt=None,
                                    prioritize_audio=False):
    """
    Construye un comando FFmpeg para convertir una secuencia de imágenes en un video.
    """
    prefix, width, found, start_number = detect_image_prefix(folder_path)
    if not found:
        return [], ""

    valid_extensions = ('.png', '.jpg', '.jpeg')
    files = sorted(os.listdir(folder_path))
    images = [f for f in files if f.lower().endswith(valid_extensions) and f.startswith(prefix)]
    num_images = len(images)
    if num_images == 0:
        return [], ""

    ext = os.path.splitext(images[0])[1]

    try:
        fps_val = float(fps)
    except ValueError:
        fps_val = 30.0
    video_duration = num_images / fps_val if fps_val > 0 else 0

    if user_format.lower().startswith("mp4"):
        extension = "mp4"
    elif user_format.lower() == "avi":
        extension = "avi"
    elif user_format.lower() == "mkv":
        extension = "mkv"
    elif user_format.lower() == "mov":
        extension = "mov"
    else:
        extension = "mp4"

    output_file = os.path.join(folder_path, f"{prefix}video.{extension}")
    output_file = get_unique_filename(output_file)

    image_pattern = os.path.join(folder_path, f"{prefix}%0{width}d{ext}")
    command = [
        "ffmpeg",
        "-y",
        "-start_number", str(start_number),
        "-framerate", str(fps),
        "-i", image_pattern
    ]

    if audio_path:
        command.extend(["-i", audio_path])

    if user_format == "mp4 (H.265 8-bit)":
        default_fmt = "yuv420p"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx265", "-pix_fmt", chosen_fmt, "-crf", crf])
    elif user_format == "mp4 (H.265 10-bit)":
        default_fmt = "yuv420p10le"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx265", "-pix_fmt", chosen_fmt, "-crf", crf])
    elif user_format == "mp4 (H.265 16-bit)":
        default_fmt = "yuv420p16le"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx265", "-pix_fmt", chosen_fmt, "-crf", crf])
    elif user_format == "mp4 (H.264 10-bit)":
        default_fmt = "yuv420p10le"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx264", "-pix_fmt", chosen_fmt, "-crf", crf])
    elif user_format == "mp4 (H.264 16-bit)":
        default_fmt = "yuv420p16le"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx264", "-pix_fmt", chosen_fmt, "-crf", crf])
    elif user_format == "mp4 (H.264 8-bit)" or user_format.lower().startswith("mp4"):
        default_fmt = "yuv420p"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx264", "-pix_fmt", chosen_fmt, "-crf", crf])
    else:
        default_fmt = "yuv420p"
        chosen_fmt = pix_fmt if pix_fmt else default_fmt
        command.extend(["-c:v", "libx264", "-pix_fmt", chosen_fmt, "-crf", crf])

    vf_filters = []
    if (fade_in_duration > 0 or fade_out_duration > 0) and video_duration > (fade_in_duration + fade_out_duration):
        fade_filter = (
            f"fade=t=in:st=0:d={fade_in_duration},"
            f"fade=t=out:st={video_duration - fade_out_duration}:d={fade_out_duration}"
        )
        vf_filters.append(fade_filter)

    if audio_path and prioritize_audio:
        audio_duration = get_audio_duration(audio_path)
        padding = audio_duration - video_duration
        if padding > 0:
            tpad_filter = f"tpad=stop_mode=add:stop_duration={padding}"
            vf_filters.append(tpad_filter)

    if vf_filters:
        command.extend(["-vf", ",".join(vf_filters)])

    if audio_path:
        if not prioritize_audio:
            command.extend(["-c:a", "aac", "-b:a", "192k", "-shortest"])
        else:
            command.extend(["-c:a", "aac", "-b:a", "192k"])

    command.append(output_file)
    print(" ".join(command))
    return command, output_file


def add_audio_to_video_command(video_path, audio_path, output_format="mp4"):
    """
    Construye un comando FFmpeg para agregar audio a un video sin sonido.
    """
    output_file = video_path.rsplit('.', 1)[0] + "_CON_AUDIO." + output_format
    output_file = get_unique_filename(output_file)
    command = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        output_file
    ]
    return command, output_file


def cut_video_command(video_path, start_time, duration=None, end_time=None,
                      output_format="mp4", cut_mode="time",
                      fade_in_duration=0, fade_out_duration=0):
    """
    Corta un vídeo con calidad máxima y permite añadir fundido a negro
    al inicio y/o al final del fragmento resultante.
    """
    base = os.path.splitext(video_path)[0]
    output_file = f"{base}_cut.{output_format}"
    output_file = get_unique_filename(output_file)

    command = ["ffmpeg", "-y", "-i", video_path]

    clip_duration = None
    start_seconds = parse_time_to_seconds(start_time)

    if end_time:
        end_seconds = parse_time_to_seconds(end_time)
        real_duration = max(0.0, end_seconds - start_seconds)
        clip_duration = real_duration
        command.extend(["-ss", str(start_seconds), "-t", str(real_duration)])
    elif duration:
        dur_seconds = parse_time_to_seconds(duration)
        clip_duration = dur_seconds
        command.extend(["-ss", str(start_seconds), "-t", str(dur_seconds)])
    else:
        command.extend(["-ss", str(start_seconds)])

    vf_filters = []

    if fade_in_duration > 0 or fade_out_duration > 0:
        vf_filters.append("setpts=PTS-STARTPTS")

    if fade_in_duration > 0:
        vf_filters.append(f"fade=t=in:st=0:d={fade_in_duration}")

    if fade_out_duration > 0 and clip_duration and clip_duration > fade_out_duration:
        fade_out_start = max(0, clip_duration - fade_out_duration)
        vf_filters.append(f"fade=t=out:st={fade_out_start}:d={fade_out_duration}")

    if vf_filters:
        command.extend(["-vf", ",".join(vf_filters)])

    video_codec_args = [
        "-c:v", "libx264",
        "-preset", "veryslow",
        "-crf", "0",
        "-pix_fmt", "yuv420p",
    ]

    audio_codec_args = [
        "-c:a", "copy",
    ]

    command.extend(
        video_codec_args
        + audio_codec_args
        + [
            "-movflags", "+faststart",
            "-map_metadata", "0",
            "-map", "0",
            output_file,
        ]
    )

    print(" ".join(command))
    return command, output_file


def parse_time_to_seconds(time_str):
    """
    Convierte una cadena de tiempo en segundos.
    Acepta:
      - "ss"
      - "ss.sss"
      - "mm:ss"
      - "hh:mm:ss"
    """
    if isinstance(time_str, (int, float)):
        return float(time_str)

    s = str(time_str).strip()

    if ":" in s:
        parts = s.split(":")
        parts = [p.strip() for p in parts]

        seconds_total = 0.0
        power = 0
        for p in reversed(parts):
            value = float(p)
            seconds_total += value * (60 ** power)
            power += 1
        return seconds_total

    return float(s)


def limit_kps_command(input_file, video_bitrate="57M", maxrate="60M", output_format="mp4"):
    """
    Construye un comando FFmpeg para limitar los kps (bitrate de video).
    """
    base = os.path.splitext(input_file)[0]
    output_file = f"{base}_limited.{output_format}"
    output_file = get_unique_filename(output_file)

    command = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-c:v", "libx264",
        "-b:v", video_bitrate,
        "-maxrate", maxrate,
        output_file
    ]
    print(" ".join(command))
    return command, output_file


def scale_video_command(input_file, scale_width, scale_height, preset="slow", crf="18", output_format="mp4"):
    """
    Construye un comando FFmpeg para reescalar un video sin recortar.
    """
    base = os.path.splitext(input_file)[0]
    output_file = f"{base}_scaled.{output_format}"
    output_file = get_unique_filename(output_file)

    command = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-vf", f"scale={scale_width}:{scale_height}",
        "-preset", preset,
        "-crf", crf,
        output_file
    ]

    print(" ".join(command))
    return command, output_file


def crop_video_command(input_file, crop_top=0, crop_bottom=0, crop_left=0, crop_right=0,
                       fade_in_duration=0, fade_out_duration=0, output_format="mp4"):
    """
    Construye un comando FFmpeg para recortar un video y opcionalmente añadir
    fundido a negro al inicio y/o al final.
    """
    base = os.path.splitext(input_file)[0]
    output_file = f"{base}_cropped.{output_format}"
    output_file = get_unique_filename(output_file)

    crop_filter = f"crop=iw-{crop_left + crop_right}:ih-{crop_top + crop_bottom}:{crop_left}:{crop_top}"
    vf_filters = [crop_filter]

    video_duration = get_video_duration(input_file)

    if fade_in_duration > 0:
        vf_filters.append(f"fade=t=in:st=0:d={fade_in_duration}")

    if fade_out_duration > 0 and video_duration > fade_out_duration:
        fade_out_start = max(0, video_duration - fade_out_duration)
        vf_filters.append(f"fade=t=out:st={fade_out_start}:d={fade_out_duration}")

    command = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-vf", ",".join(vf_filters),
        output_file
    ]

    print(" ".join(command))
    return command, output_file


def remove_audio_command(video_path, output_format="mp4"):
    """
    Construye un comando FFmpeg para quitar el audio de un video.
    """
    base = os.path.splitext(video_path)[0]
    output_file = f"{base}_SIN_AUDIO.{output_format}"
    output_file = get_unique_filename(output_file)
    command = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-c:v", "copy",
        "-an",
        output_file
    ]
    print(" ".join(command))
    return command, output_file


def replace_audio_command(video_path, new_audio_path, output_format="mp4"):
    """
    Construye un comando FFmpeg para sustituir la pista de audio de un video por una nueva.
    """
    base = os.path.splitext(video_path)[0]
    output_file = f"{base}_REPLACE_AUDIO.{output_format}"
    output_file = get_unique_filename(output_file)
    command = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-i", new_audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        output_file
    ]
    print(" ".join(command))
    return command, output_file


def validate_merge_inputs(video_paths):
    """
    Valida que la lista de vídeos para unir tenga al menos 2 elementos
    y que todos existan.
    """
    if not video_paths or len(video_paths) < 2:
        return False, "Selecciona al menos 2 vídeos."

    for path in video_paths:
        if not os.path.isfile(path):
            return False, f"No existe el archivo: {path}"

    return True, ""


def build_concat_file(video_paths):
    """
    Genera un archivo temporal de concat para FFmpeg.
    """
    fd, concat_file = tempfile.mkstemp(prefix="ffmpeg_concat_", suffix=".txt", text=True)
    os.close(fd)

    with open(concat_file, "w", encoding="utf-8") as f:
        for video_path in video_paths:
            normalized = os.path.abspath(video_path).replace("\\", "/")
            escaped = normalized.replace("'", r"'\''")
            f.write(f"file '{escaped}'\n")

    return concat_file


def merge_videos_command(video_paths, mode="fast", output_name=None, preset="slow",
                         crf="19", output_format="mp4", output_dir=None):
    """
    Construye un comando FFmpeg para unir múltiples vídeos.

    Parámetros:
        video_paths: lista ordenada de vídeos a unir.
        mode: 'fast' o 'compatible'
        output_name: nombre de salida sin extensión
        preset: preset de codificación
        crf: calidad de codificación
        output_format: formato de salida
        output_dir: directorio de salida opcional. Si es None, usa la carpeta del primer vídeo.

    El modo 'fast' (sin recodificar) sólo es fiable si todos los vídeos tienen
    exactamente las mismas pistas y parámetros. Si no es así, se usa automáticamente
    el modo compatible, porque el concat demuxer mezclaría las pistas y el resultado
    saldría recortado o corrupto.

    Retorna:
        (command, output_file, concat_file, error_message, details)
        details = {"mode": modo usado, "notice": aviso para el usuario, "total_frames": estimación}
    """
    details = {"mode": mode, "notice": "", "total_frames": 0}

    is_valid, error_message = validate_merge_inputs(video_paths)
    if not is_valid:
        return [], "", "", error_message, details

    if mode not in {"fast", "compatible"}:
        return [], "", "", f"Modo de unión no válido: {mode}", details

    infos = []
    for path in video_paths:
        try:
            info = probe_video(path)
        except Exception as e:
            return [], "", "", f"No se pudo analizar el vídeo {os.path.basename(path)}: {e}", details
        if not info["video"]:
            return [], "", "", f"El archivo no tiene pista de vídeo: {os.path.basename(path)}", details
        infos.append(info)

    if mode == "fast":
        compatible, reason = check_fast_merge_compatibility(infos)
        if not compatible:
            mode = "compatible"
            details["mode"] = mode
            details["notice"] = f"Se ha recodificado porque {reason}."

    first_video = os.path.abspath(video_paths[0])
    base_dir = output_dir if output_dir else os.path.dirname(first_video)

    try:
        os.makedirs(base_dir, exist_ok=True)
    except Exception as e:
        return [], "", "", f"No se pudo crear el directorio de salida: {e}", details

    if output_name and str(output_name).strip():
        filename = f"{str(output_name).strip()}.{output_format}"
    else:
        suffix = "merged_fast" if mode == "fast" else "merged_compatible"
        filename = f"{suffix}.{output_format}"

    output_file = os.path.join(base_dir, filename)
    output_file = get_unique_filename(output_file)

    details["total_frames"] = int(sum(info["duration"] for info in infos) * infos[0]["video"]["fps"])

    if mode == "fast":
        concat_file = build_concat_file(video_paths)
        command = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_file,
            "-map", "0",
            "-c", "copy",
            "-movflags", "+faststart",
            output_file
        ]
    else:
        concat_file = ""
        command = build_compatible_merge_command(video_paths, infos, output_file, preset=preset, crf=crf)

    print(" ".join(command))
    return command, output_file, concat_file, "", details
