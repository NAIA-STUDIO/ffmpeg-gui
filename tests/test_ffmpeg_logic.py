"""
Tests unitarios para las funciones puras de logic/ffmpeg_logic.py.

A diferencia de tests/test_cli_bridge.cjs (que ejercita el pipeline completo
con ffmpeg real), estos tests cubren la lógica que no depende de invocar
ffmpeg/ffprobe: parseo, saneado de nombres, validaciones y el algoritmo de
emparejado por resolución (con get_video_resolution/get_video_duration
mockeados). Solo requieren la stdlib.

Ejecutar con:
    python -m unittest discover -s tests -p "test_ffmpeg_logic.py" -v
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from logic import ffmpeg_logic as logic


class ParseTimeToSecondsTests(unittest.TestCase):
    def test_plain_seconds(self):
        self.assertEqual(logic.parse_time_to_seconds("10"), 10.0)
        self.assertEqual(logic.parse_time_to_seconds("10.5"), 10.5)

    def test_numeric_input(self):
        self.assertEqual(logic.parse_time_to_seconds(10), 10.0)
        self.assertEqual(logic.parse_time_to_seconds(10.5), 10.5)

    def test_mm_ss(self):
        self.assertEqual(logic.parse_time_to_seconds("1:30"), 90.0)

    def test_hh_mm_ss(self):
        self.assertEqual(logic.parse_time_to_seconds("01:02:03"), 3723.0)

    def test_strips_whitespace_in_parts(self):
        self.assertEqual(logic.parse_time_to_seconds(" 1 : 30 "), 90.0)

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            logic.parse_time_to_seconds("no-es-un-numero")


class SanitizeFilenamePartTests(unittest.TestCase):
    def test_replaces_invalid_chars(self):
        self.assertEqual(logic.sanitize_filename_part("1080x1920"), "1080x1920")
        self.assertEqual(logic.sanitize_filename_part("a/b:c*d?"), "a_b_c_d")

    def test_strips_leading_trailing_underscores(self):
        self.assertEqual(logic.sanitize_filename_part("  hola  "), "hola")

    def test_non_string_input(self):
        self.assertEqual(logic.sanitize_filename_part(123), "123")


class BuildAutoMergeOutputNameTests(unittest.TestCase):
    def test_default_variant_first_pair(self):
        name = logic.build_auto_merge_output_name(
            {"resolution": "1080x1920", "variant": "default", "pair_index": 1}
        )
        self.assertEqual(name, "merge_1080x1920")

    def test_sin_logo_variant(self):
        name = logic.build_auto_merge_output_name(
            {"resolution": "1080x1920", "variant": "sin_logo", "pair_index": 1}
        )
        self.assertEqual(name, "merge_1080x1920_sin_logo")

    def test_pair_index_suffix(self):
        name = logic.build_auto_merge_output_name(
            {"resolution": "1080x1920", "variant": "default", "pair_index": 2}
        )
        self.assertEqual(name, "merge_1080x1920_2")


class IsVideoFileTests(unittest.TestCase):
    def test_true_for_known_extensions(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            path = f.name
        try:
            self.assertTrue(logic.is_video_file(path))
        finally:
            os.remove(path)

    def test_false_for_missing_file(self):
        self.assertFalse(logic.is_video_file(os.path.join(tempfile.gettempdir(), "no-existe.mp4")))

    def test_false_for_wrong_extension(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.assertFalse(logic.is_video_file(path))
        finally:
            os.remove(path)


class GetVideoVariantTests(unittest.TestCase):
    def test_detects_sin_logo_markers(self):
        self.assertEqual(logic.get_video_variant("clip sin logo.mp4"), "sin_logo")
        self.assertEqual(logic.get_video_variant("clip_sin_logo.mp4"), "sin_logo")
        self.assertEqual(logic.get_video_variant("clip-sin-logo.mp4"), "sin_logo")

    def test_default_variant(self):
        self.assertEqual(logic.get_video_variant("clip.mp4"), "default")


class ValidateMergeInputsTests(unittest.TestCase):
    def test_rejects_fewer_than_two(self):
        ok, message = logic.validate_merge_inputs(["solo_uno.mp4"])
        self.assertFalse(ok)
        self.assertIn("al menos 2", message)

    def test_rejects_missing_file(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            existing = f.name
        try:
            ok, message = logic.validate_merge_inputs([existing, "no-existe.mp4"])
            self.assertFalse(ok)
            self.assertIn("No existe", message)
        finally:
            os.remove(existing)

    def test_accepts_two_existing_files(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f2:
            paths = [f1.name, f2.name]
        try:
            ok, message = logic.validate_merge_inputs(paths)
            self.assertTrue(ok)
            self.assertEqual(message, "")
        finally:
            for p in paths:
                os.remove(p)


class PairVideosByResolutionTests(unittest.TestCase):
    """
    get_video_resolution() invoca ffprobe; se mockea para no depender de
    tener ffmpeg instalado ni de generar vídeos reales.
    """

    def _make_videos(self, folder, names):
        paths = []
        for name in names:
            path = os.path.join(folder, name)
            with open(path, "wb"):
                pass
            paths.append(path)
        return paths

    def test_pairs_by_matching_resolution(self):
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            self._make_videos(dir1, ["a.mp4", "b.mp4"])
            self._make_videos(dir2, ["c.mp4", "d.mp4"])

            def fake_resolution(path):
                name = os.path.basename(path)
                return {"a.mp4": "1080x1920", "b.mp4": "1080x1080",
                        "c.mp4": "1080x1920", "d.mp4": "1080x1080"}[name]

            with patch.object(logic, "get_video_resolution", side_effect=fake_resolution):
                pairs, ignored_1, ignored_2, warnings = logic.pair_videos_by_resolution(dir1, dir2)

            self.assertEqual(len(pairs), 2)
            self.assertEqual(ignored_1, [])
            self.assertEqual(ignored_2, [])
            self.assertEqual(warnings, [])
            resolutions = sorted(p["resolution"] for p in pairs)
            self.assertEqual(resolutions, ["1080x1080", "1080x1920"])

    def test_sin_logo_variant_kept_separate(self):
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            self._make_videos(dir1, ["a.mp4", "a sin logo.mp4"])
            self._make_videos(dir2, ["b.mp4", "b sin logo.mp4"])

            with patch.object(logic, "get_video_resolution", return_value="1080x1920"):
                pairs, ignored_1, ignored_2, warnings = logic.pair_videos_by_resolution(dir1, dir2)

            self.assertEqual(len(pairs), 2)
            variants = sorted(p["variant"] for p in pairs)
            self.assertEqual(variants, ["default", "sin_logo"])

    def test_unmatched_videos_are_ignored_with_warning(self):
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            self._make_videos(dir1, ["a.mp4", "b.mp4"])
            self._make_videos(dir2, ["c.mp4"])

            with patch.object(logic, "get_video_resolution", return_value="1080x1920"):
                pairs, ignored_1, ignored_2, warnings = logic.pair_videos_by_resolution(dir1, dir2)

            self.assertEqual(len(pairs), 1)
            self.assertEqual(len(ignored_1), 1)
            self.assertEqual(ignored_2, [])
            self.assertEqual(len(warnings), 1)

    def test_unreadable_resolution_is_ignored(self):
        with tempfile.TemporaryDirectory() as dir1, tempfile.TemporaryDirectory() as dir2:
            self._make_videos(dir1, ["a.mp4"])
            self._make_videos(dir2, ["b.mp4"])

            with patch.object(logic, "get_video_resolution", return_value=None):
                pairs, ignored_1, ignored_2, warnings = logic.pair_videos_by_resolution(dir1, dir2)

            self.assertEqual(pairs, [])
            self.assertEqual(len(ignored_1), 1)
            self.assertEqual(len(ignored_2), 1)


def _info(stream_types=("video", "audio"), width=1080, height=1920, frame_rate="30/1", audio=True, duration=10.0):
    return {
        "stream_types": list(stream_types),
        "video": {"codec": "h264", "width": width, "height": height, "pix_fmt": "yuv420p",
                  "frame_rate": frame_rate, "fps": 30.0, "time_base": "1/30000"},
        "audio": {"codec": "aac", "sample_rate": "48000", "channels": 2} if audio else None,
        "duration": duration,
    }


class CheckFastMergeCompatibilityTests(unittest.TestCase):
    def test_identical_videos_are_compatible(self):
        ok, reason = logic.check_fast_merge_compatibility([_info(), _info(duration=3)])
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_missing_audio_or_different_track_order_is_incompatible(self):
        # Caso real: vídeo sin audio + vídeo con el audio como primera pista
        infos = [_info(stream_types=["video"], audio=False), _info(stream_types=["audio", "video"])]
        ok, _ = logic.check_fast_merge_compatibility(infos)
        self.assertFalse(ok)

    def test_different_resolution_is_incompatible(self):
        ok, _ = logic.check_fast_merge_compatibility([_info(), _info(width=1080, height=1080)])
        self.assertFalse(ok)


class MergeVideosCommandTests(unittest.TestCase):
    def _merge(self, infos, mode):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(len(infos)):
                path = os.path.join(tmp, f"v{i}.mp4")
                open(path, "w").close()
                paths.append(path)
            with patch.object(logic, "probe_video", side_effect=infos):
                command, output_file, concat_file, error, details = logic.merge_videos_command(
                    paths, mode=mode, output_dir=tmp
                )
            if concat_file and os.path.exists(concat_file):
                os.remove(concat_file)
            return command, error, details

    def test_fast_mode_uses_stream_copy_when_compatible(self):
        command, error, details = self._merge([_info(), _info()], "fast")
        self.assertEqual(error, "")
        self.assertEqual(details["mode"], "fast")
        self.assertIn("copy", command)

    def test_fast_mode_falls_back_to_compatible_when_tracks_differ(self):
        infos = [_info(stream_types=["video"], audio=False, duration=162.8),
                 _info(stream_types=["audio", "video"], duration=17.1)]
        command, error, details = self._merge(infos, "fast")
        self.assertEqual(details["mode"], "compatible")
        self.assertTrue(details["notice"])
        filter_complex = command[command.index("-filter_complex") + 1]
        # Silencio para el clip sin audio y concat de los dos segmentos con audio
        self.assertIn("anullsrc", filter_complex)
        self.assertIn("atrim=duration=162.8", filter_complex)
        self.assertIn("concat=n=2:v=1:a=1", filter_complex)
        self.assertEqual(details["total_frames"], int((162.8 + 17.1) * 30))

    def test_compatible_without_any_audio_outputs_video_only(self):
        infos = [_info(stream_types=["video"], audio=False), _info(stream_types=["video"], audio=False, width=640, height=360)]
        command, error, details = self._merge(infos, "compatible")
        filter_complex = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=1:a=0", filter_complex)
        self.assertIn("scale=1080:1920", filter_complex)
        self.assertNotIn("-c:a", command)

    def test_file_without_video_is_rejected(self):
        info = _info()
        info["video"] = None
        command, error, _ = self._merge([info, _info()], "compatible")
        self.assertEqual(command, [])
        self.assertIn("no tiene pista de vídeo", error)


if __name__ == "__main__":
    unittest.main()
