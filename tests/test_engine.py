"""核心引擎：预算是否真的达标、版面是否保真、边界是否安全。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from PIL import Image, ImageOps  # noqa: E402

from helpers import document, noise, photo, rgba, save  # noqa: E402
from imgfit.engine import (  # noqa: E402
    ACTION_COMPRESSED,
    ACTION_COPIED,
    ACTION_FAILED,
    MODE_QUALITY,
    MODE_RESOLUTION,
    FitConfig,
    FitError,
    compress_file,
    fit,
    prepare,
    unique_target,
)

FAST = dict(max_dim=1200, floor_dim=400, dim_step=100)


def cfg(**kwargs) -> FitConfig:
    base = dict(budget=120 * 1024, **FAST)
    base.update(kwargs)
    return FitConfig(**base)


class EngineTestCase(unittest.TestCase):
    """带临时图片写入能力的基类（退出时自动清理）。"""

    def write_image(self, image: Image.Image, **kwargs) -> Path:
        suffix = kwargs.pop("suffix", ".jpg")
        handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        handle.close()
        path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        image.save(path, **kwargs)
        return path


class FitTests(EngineTestCase):
    def test_result_is_within_budget(self):
        image = noise((1200, 900))
        result = fit(image, cfg())
        self.assertLessEqual(len(result.data), cfg().budget)

    def test_aspect_ratio_preserved(self):
        image = noise((1200, 900))
        result = fit(image, cfg())
        self.assertAlmostEqual(result.size[0] / result.size[1], 1200 / 900, places=2)

    def test_output_is_decodable_jpeg(self):
        result = fit(noise((900, 600)), cfg())
        with Image.open(io.BytesIO(result.data)) as decoded:
            decoded.load()
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.mode, "RGB")

    def test_resolution_mode_keeps_more_pixels_than_quality_mode(self):
        """同一预算下，分辨率优先模式必须给出更大的画面。"""
        image = document((1000, 1400))
        res_cfg = cfg(mode=MODE_RESOLUTION, budget=90 * 1024)
        qual_cfg = cfg(mode=MODE_QUALITY, budget=90 * 1024, target_quality=80)
        resolution_result = fit(image, res_cfg)
        quality_result = fit(image, qual_cfg)
        self.assertGreaterEqual(
            max(resolution_result.size), max(quality_result.size)
        )

    def test_quality_mode_hits_target_quality(self):
        image = photo((900, 600))
        result = fit(image, cfg(mode=MODE_QUALITY, budget=150 * 1024, target_quality=78))
        self.assertEqual(result.quality, 78)
        self.assertLessEqual(len(result.data), 150 * 1024)

    def test_never_upscales(self):
        image = photo((300, 200))
        result = fit(image, cfg(floor_dim=100, hard_min_dim=50))
        self.assertEqual(result.size, (300, 200))

    def test_budget_too_small_raises_with_hint(self):
        with self.assertRaises(FitError) as ctx:
            fit(noise((800, 600)), FitConfig(budget=200, max_dim=1200, floor_dim=400, hard_min_dim=320))
        self.assertIn("过小", str(ctx.exception))

    def test_metadata_stripped_by_default(self):
        image = Image.new("RGB", (400, 300), "white")
        exif = Image.Exif()
        exif[271] = "TestCamera"
        prepared = prepare(self.write_image(image, exif=exif))
        result = fit(prepared, cfg(budget=60 * 1024))
        with Image.open(io.BytesIO(result.data)) as decoded:
            self.assertFalse(decoded.getexif().get(271))

    def test_keep_metadata_option(self):
        image = Image.new("RGB", (400, 300), "white")
        exif = Image.Exif()
        exif[271] = "TestCamera"
        path = self.write_image(image, exif=exif)
        prepared = prepare(path)
        result = fit(prepared, cfg(budget=60 * 1024, keep_metadata=True))
        with Image.open(io.BytesIO(result.data)) as decoded:
            self.assertEqual(decoded.getexif().get(271), "TestCamera")


class PrepareTests(EngineTestCase):
    def test_exif_orientation_applied(self):
        image = Image.new("RGB", (100, 200), "white")
        exif = Image.Exif()
        exif[274] = 6  # 需要顺时针旋转 90 度
        prepared = prepare(self.write_image(image, exif=exif))
        self.assertEqual(prepared.image.size, (200, 100))

    def test_transparent_flattened_to_background(self):
        prepared = prepare(self.write_image(rgba((120, 90), alpha=0), suffix=".png"))
        self.assertEqual(prepared.image.mode, "RGB")
        self.assertEqual(prepared.image.getpixel((5, 5)), (255, 255, 255))

    def test_custom_background(self):
        prepared = prepare(
            self.write_image(rgba((120, 90), alpha=0), suffix=".png"),
            FitConfig(background=(10, 20, 30)),
        )
        self.assertEqual(prepared.image.getpixel((5, 5)), (10, 20, 30))


class CompressFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src_dir = self.root / "src"
        self.out_dir = self.root / "out"

    def test_small_file_copied_byte_for_byte(self):
        source = save(document((600, 800)), self.src_dir / "small.jpg", quality=60)
        original = source.read_bytes()
        result = compress_file(source, self.out_dir, cfg(budget=200 * 1024))
        self.assertEqual(result.action, ACTION_COPIED)
        self.assertEqual(result.target.read_bytes(), original)
        self.assertEqual(result.quality, None)

    def test_small_file_reencoded_when_copy_disabled(self):
        source = save(document((600, 800)), self.src_dir / "small.jpg", quality=60)
        result = compress_file(source, self.out_dir, cfg(budget=200 * 1024, copy_if_fits=False))
        self.assertEqual(result.action, ACTION_COMPRESSED)
        self.assertLessEqual(result.output_bytes, 200 * 1024)

    def test_large_file_compressed_and_verified(self):
        source = save(noise((1600, 1200)), self.src_dir / "big.jpg", quality=95)
        result = compress_file(source, self.out_dir, cfg(budget=100 * 1024))
        self.assertEqual(result.action, ACTION_COMPRESSED)
        self.assertLessEqual(result.output_bytes, 100 * 1024)
        self.assertTrue(result.target.exists())
        with Image.open(result.target) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, result.output_size)

    def test_source_is_never_overwritten(self):
        source = save(noise((1200, 900)), self.src_dir / "same.jpg", quality=95)
        before = source.read_bytes()
        result = compress_file(source, self.src_dir, cfg(budget=80 * 1024), overwrite=True)
        self.assertNotEqual(result.target, source)
        self.assertEqual(source.read_bytes(), before)

    def test_existing_target_not_clobbered(self):
        source = save(noise((1000, 800)), self.src_dir / "x.jpg", quality=95)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "x.jpg").write_bytes(b"keep me")
        result = compress_file(source, self.out_dir, cfg(budget=80 * 1024))
        self.assertEqual((self.out_dir / "x.jpg").read_bytes(), b"keep me")
        self.assertEqual(result.target.name, "x(1).jpg")

    def test_broken_input_reports_failure_without_raising(self):
        broken = self.src_dir / "broken.jpg"
        broken.parent.mkdir(parents=True, exist_ok=True)
        # 体积超过预算，才会走"重新编码"分支并暴露解码失败
        broken.write_bytes(b"not an image" * 50_000)
        result = compress_file(broken, self.out_dir, cfg())
        self.assertFalse(result.ok)
        self.assertEqual(result.action, ACTION_FAILED)
        self.assertTrue(result.detail)

    def test_unique_target_numbering(self):
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "a.jpg").write_bytes(b"x")
        (self.out_dir / "a(1).jpg").write_bytes(b"x")
        self.assertEqual(unique_target(self.out_dir, "a", ".jpg").name, "a(2).jpg")


class FloorTests(EngineTestCase):
    """质量下限的默认值按模式区分（0.2.0 修正）。"""

    def test_default_floor_depends_on_mode(self):
        self.assertEqual(FitConfig(mode=MODE_RESOLUTION).quality_floor, 10)
        self.assertEqual(FitConfig(mode=MODE_QUALITY).quality_floor, 40)
        self.assertEqual(FitConfig(mode=MODE_RESOLUTION, min_quality=25).quality_floor, 25)

    def test_document_keeps_native_resolution_when_budget_allows(self):
        """密实文字图 + 够用的预算：应该保留原分辨率，而不是守 q40 去缩尺寸。"""
        image = document((900, 1200))
        result = fit(image, FitConfig(budget=200 * 1024, max_dim=2000, floor_dim=400))
        self.assertEqual(result.size, (900, 1200))
        self.assertLessEqual(len(result.data), 200 * 1024)

    def test_candidate_sides_strictly_decreasing(self):
        from imgfit.engine import _candidate_sides

        main, low = _candidate_sides(FitConfig(), 4000)
        self.assertEqual(main, sorted(set(main), reverse=True))
        self.assertGreaterEqual(len(main), 10)
        self.assertTrue(all(s >= 800 for s in main))
        self.assertTrue(all(s < 800 for s in low))


if __name__ == "__main__":
    unittest.main()
