"""核心压缩引擎：把一张图片编码进指定字节预算。

搜索思路
--------
JPEG 的实际字节数取决于图像内容的局部复杂度，用公式估算通常有 20%~30% 的
误差，所以这里**不做估算**——对每个候选参数真编码一次、量真实字节数。

预算固定时有两个自由度：

* **分辨率**（长边像素数）——决定细节多少
* **编码质量**（quality）——决定压缩伪影多少

两者对"还能不能看清"的贡献完全不同：

* 文字类材料（成绩单、证书、证件扫描件）的信息量在分辨率里，质量掉到 40
  文字仍然可辨；分辨率砍一半就直接糊成一团。→ ``mode="resolution"``
* 照片类材料信息分散，质量掉到 40 会出现明显色块。→ ``mode="quality"``

实测数据见 ``docs/设计说明.md``。
"""

from __future__ import annotations

import io
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

from PIL import Image, ImageOps

__all__ = [
    "MODE_QUALITY",
    "MODE_RESOLUTION",
    "MODES",
    "FileResult",
    "FitConfig",
    "FitError",
    "FitResult",
    "Prepared",
    "compress_file",
    "encode",
    "fit",
    "prepare",
]

RGB = Tuple[int, int, int]
Size = Tuple[int, int]

MODE_RESOLUTION = "resolution"   # 分辨率优先：文档 / 扫描件 / 证书
MODE_QUALITY = "quality"         # 质量优先：照片 / 图片素材
MODES = (MODE_RESOLUTION, MODE_QUALITY)

ACTION_COMPRESSED = "compressed"
ACTION_COPIED = "copied"
ACTION_FAILED = "failed"

# Pillow 9.1+ 把重采样常量搬进了 Image.Resampling，这里做兼容
LANCZOS = getattr(Image, "Resampling", Image).LANCZOS


class FitError(Exception):
    """无法在给定预算内完成编码（预算过小或图像无法处理）。"""


@dataclass(frozen=True)
class FitConfig:
    """一次压缩的全部可调参数。

    budget          : 硬预算，单张输出不得超过（字节）
    mode            : "resolution" 分辨率优先 / "quality" 质量优先
    min_quality     : 质量下限，再低就不如放弃分辨率
    max_quality     : 质量上限（初值）
    target_quality  : quality 模式的保底质量
    max_dim         : 长边上限，超过此值对上传没有意义
    floor_dim       : 长边下限，分辨率优先模式下不允许再缩小
    hard_min_dim    : 极端情况下的绝对下限
    """

    budget: int = 200 * 1024
    mode: str = MODE_RESOLUTION
    min_quality: int = 40
    max_quality: int = 92
    target_quality: int = 75
    quality_step: int = 2
    max_dim: int = 2200
    floor_dim: int = 800
    hard_min_dim: int = 320
    dim_step: int = 50
    subsampling: int = 2
    copy_if_fits: bool = True
    keep_metadata: bool = False
    background: RGB = (255, 255, 255)

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError("预算必须为正数")
        if self.mode not in MODES:
            raise ValueError(f"未知模式: {self.mode!r}（可选 {'/'.join(MODES)}）")
        if not 1 <= self.min_quality <= self.max_quality <= 100:
            raise ValueError("质量必须满足 1 <= min_quality <= max_quality <= 100")
        if not self.min_quality <= self.target_quality <= self.max_quality:
            raise ValueError("target_quality 必须落在 [min_quality, max_quality] 区间内")
        if not 1 <= self.quality_step <= 32:
            raise ValueError("quality_step 必须在 1~32 之间")
        if not 1 <= self.hard_min_dim <= self.floor_dim <= self.max_dim:
            raise ValueError("必须满足 1 <= hard_min_dim <= floor_dim <= max_dim")
        if self.dim_step < 1:
            raise ValueError("dim_step 必须为正整数")
        if self.subsampling not in (0, 1, 2):
            raise ValueError("subsampling 只能是 0 / 1 / 2")


@dataclass(frozen=True)
class Prepared:
    """已转正方向、已拍平透明通道、可直接编码的图片。"""

    image: Image.Image
    exif: Optional[bytes] = None
    icc: Optional[bytes] = None


@dataclass(frozen=True)
class FitResult:
    """一次成功的编码结果。"""

    data: bytes
    size: Size
    quality: int
    long_side: int
    encodes: int = 0

    @property
    def nbytes(self) -> int:
        return len(self.data)


@dataclass
class FileResult:
    """单个文件的处理结果（成功或失败都返回，不抛异常）。"""

    source: Path
    target: Optional[Path]
    original_bytes: int
    output_bytes: int = 0
    original_size: Optional[Size] = None
    output_size: Optional[Size] = None
    quality: Optional[int] = None
    action: str = ACTION_FAILED
    detail: str = ""
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.action in (ACTION_COMPRESSED, ACTION_COPIED)

    @property
    def ratio(self) -> float:
        if not self.original_bytes:
            return 0.0
        return self.output_bytes / self.original_bytes

    @property
    def saved_bytes(self) -> int:
        return max(0, self.original_bytes - self.output_bytes)


# ---------------------------------------------------------------- 图像准备


def _flatten(im: Image.Image, background: RGB) -> Image.Image:
    """把带透明通道的图拍平到指定背景色（转 JPEG 时透明会变黑）。"""
    rgba = im.convert("RGBA")
    canvas = Image.new("RGB", rgba.size, tuple(background))
    canvas.paste(rgba, mask=rgba.getchannel("A"))
    return canvas


def prepare(path: Union[str, Path], cfg: Optional[FitConfig] = None) -> Prepared:
    """读图并做好编码前处理：按 EXIF 转正、透明拍平、统一成 RGB。"""
    cfg = cfg or FitConfig()
    with Image.open(path) as raw:
        exif = raw.info.get("exif")
        icc = raw.info.get("icc_profile")
        im = ImageOps.exif_transpose(raw)
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA") or (
            im.mode == "P" and "transparency" in im.info
        )
        if has_alpha:
            im = _flatten(im, cfg.background)
        elif im.mode != "RGB":
            im = im.convert("RGB")
        return Prepared(
            image=im.copy(),
            exif=bytes(exif) if exif else None,
            icc=bytes(icc) if icc else None,
        )


def encode(
    im: Image.Image,
    quality: int,
    cfg: Optional[FitConfig] = None,
    exif: Optional[bytes] = None,
    icc: Optional[bytes] = None,
) -> bytes:
    """按给定质量把图片编码成 JPEG 字节串（不做任何估算，返回真实字节）。"""
    cfg = cfg or FitConfig()
    buf = io.BytesIO()
    params: Dict[str, object] = {
        "format": "JPEG",
        "quality": int(quality),
        "optimize": True,        # 针对本图重新生成最优 Huffman 表，同质量下更小
        "progressive": True,     # 渐进式：体积略降，弱网下先出轮廓
        "subsampling": cfg.subsampling,   # 2 == 4:2:0，亮度全采样
    }
    if cfg.keep_metadata:
        if exif:
            params["exif"] = exif
        if icc:
            params["icc_profile"] = icc
    im.save(buf, **params)
    return buf.getvalue()


# ---------------------------------------------------------------- 参数搜索


def _levels(start: int, stop: int, step: int) -> List[int]:
    """生成从 start 递减到 stop（含两端）的整数列表。"""
    if start < stop:
        start, stop = stop, start
    out: List[int] = []
    cur = start
    while cur > stop:
        out.append(cur)
        cur -= step
    if not out or out[-1] != stop:
        out.append(stop)
    return out


class _Fitter:
    """带缓存的参数搜索器：同一 (分辨率, 质量) 只编码一次。"""

    def __init__(self, prepared: Prepared, cfg: FitConfig) -> None:
        self.prepared = prepared
        self.cfg = cfg
        self.encodes = 0
        self._resized: Dict[int, Image.Image] = {}
        self._encoded: Dict[Tuple[int, int], bytes] = {}

    def image_at(self, long_side: int) -> Image.Image:
        im = self.prepared.image
        native = max(im.size)
        if long_side >= native:
            return im
        cached = self._resized.get(long_side)
        if cached is not None:
            return cached
        scale = long_side / native
        width = max(1, round(im.size[0] * scale))
        height = max(1, round(im.size[1] * scale))
        resized = im.resize((width, height), LANCZOS)
        self._resized[long_side] = resized
        return resized

    def encoded(self, long_side: int, quality: int) -> bytes:
        key = (long_side, quality)
        data = self._encoded.get(key)
        if data is None:
            data = encode(
                self.image_at(long_side),
                quality,
                self.cfg,
                exif=self.prepared.exif,
                icc=self.prepared.icc,
            )
            self.encodes += 1
            self._encoded[key] = data
        return data

    def fits(self, long_side: int, quality: int) -> bool:
        return len(self.encoded(long_side, quality)) <= self.cfg.budget

    def _result(self, long_side: int, quality: int) -> FitResult:
        return FitResult(
            data=self.encoded(long_side, quality),
            size=self.image_at(long_side).size,
            quality=quality,
            long_side=long_side,
            encodes=self.encodes,
        )

    def search_resolution(self, sides: Sequence[int]) -> Optional[FitResult]:
        """分辨率优先：从大往小找，第一个"最低质量塞得下"的尺寸就采用。"""
        levels = _levels(self.cfg.max_quality, self.cfg.min_quality, self.cfg.quality_step)
        for long_side in sides:
            if not self.fits(long_side, self.cfg.min_quality):
                continue  # 连最低质量都超预算，换更小的分辨率（只花 1 次编码）
            index = self._best_quality_index(long_side, levels)
            if index is not None:
                return self._result(long_side, levels[index])
        return None

    def _best_quality_index(self, long_side: int, levels: Sequence[int]) -> Optional[int]:
        """在"质量从高到低"的候选里，二分出塞得下的最高质量（体积随质量单调递增）。"""
        low, high, best = 0, len(levels) - 1, None
        while low <= high:
            mid = (low + high) // 2
            if self.fits(long_side, levels[mid]):
                best = mid
                high = mid - 1  # 还能再高一点
            else:
                low = mid + 1
        return best

    def search_quality(self, sides_asc: Sequence[int]) -> Optional[FitResult]:
        """质量优先：守住保底质量，在该质量下取能塞下的最大分辨率。"""
        levels = _levels(self.cfg.target_quality, self.cfg.min_quality, self.cfg.quality_step)
        for quality in levels:
            long_side = self._largest_fitting_side(sides_asc, quality)
            if long_side is not None:
                return self._result(long_side, quality)
        return None

    def _largest_fitting_side(self, sides_asc: Sequence[int], quality: int) -> Optional[int]:
        """二分查找能塞下的最大长边（体积随分辨率单调递增）。"""
        low, high, best = 0, len(sides_asc) - 1, None
        while low <= high:
            mid = (low + high) // 2
            if self.fits(sides_asc[mid], quality):
                best = sides_asc[mid]
                low = mid + 1
            else:
                high = mid - 1
        return best


def _candidate_sides(cfg: FitConfig, native: int) -> Tuple[List[int], List[int]]:
    cap = min(native, cfg.max_dim)
    floor = min(cfg.floor_dim, cap)
    main: List[int] = []
    cur = cap
    while cur > floor:
        main.append(cur)
        cur -= cfg.dim_step
    main.append(floor)

    low: List[int] = []
    cur = floor - cfg.dim_step
    step = max(cfg.dim_step, 100)
    while cur >= cfg.hard_min_dim:
        low.append(cur)
        cur -= step
    return main, low


def fit(image: Union[Prepared, Image.Image], cfg: Optional[FitConfig] = None) -> FitResult:
    """在预算内找出最优的（分辨率, 质量）组合。

    失败时抛 :class:`FitError`，并说明"最小也还有多大"。
    """
    cfg = cfg or FitConfig()
    prepared = image if isinstance(image, Prepared) else Prepared(image=image)
    fitter = _Fitter(prepared, cfg)
    native = max(prepared.image.size)
    main, low = _candidate_sides(cfg, native)

    result: Optional[FitResult]
    if cfg.mode == MODE_QUALITY:
        asc = sorted(set(main + low))
        result = fitter.search_quality(asc)
        if result is None:  # 极端情况：降质量也不够，退回分辨率优先
            result = fitter.search_resolution(sorted(set(main + low), reverse=True))
    else:
        result = fitter.search_resolution(main) or fitter.search_resolution(low)

    if result is None:
        smallest = len(fitter.encoded(cfg.hard_min_dim, cfg.min_quality))
        raise FitError(
            "预算 {budget} 字节过小：即使缩到 {dim}px、质量 {q} 也还需 {need} 字节".format(
                budget=cfg.budget, dim=cfg.hard_min_dim, q=cfg.min_quality, need=smallest
            )
        )
    return result


# ---------------------------------------------------------------- 单文件流程


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:  # pragma: no cover - 极端路径异常
        return str(a) == str(b)


def unique_target(
    dst_dir: Path,
    stem: str,
    suffix: str,
    source: Optional[Path] = None,
    overwrite: bool = False,
) -> Path:
    """生成不覆盖源文件、不覆盖已有产物的目标路径。"""
    suffix = suffix if suffix.startswith(".") else "." + suffix
    candidate = Path(dst_dir) / f"{stem}{suffix}"
    index = 1
    while candidate.exists() or (source is not None and _same_path(candidate, source)):
        if not overwrite or (source is not None and _same_path(candidate, source)):
            candidate = Path(dst_dir) / f"{stem}({index}){suffix}"
            index += 1
            continue
        break
    return candidate


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _verify_output(path: Path, budget: int, expected_size: Size) -> None:
    """写完再自检：字节数达标 + 能完整解码 + 尺寸对得上。"""
    size = path.stat().st_size
    if size > budget:
        raise FitError(f"产物 {size} 字节超过预算 {budget} 字节")
    with Image.open(path) as check:
        check.load()
        if tuple(check.size) != tuple(expected_size):
            raise FitError(f"产物尺寸 {check.size} 与预期 {expected_size} 不一致")


def _probe_size(path: Path) -> Optional[Size]:
    try:
        with Image.open(path) as im:
            return tuple(im.size)  # type: ignore[return-value]
    except Exception:
        return None


def compress_file(
    source: Union[str, Path],
    dst_dir: Union[str, Path],
    cfg: Optional[FitConfig] = None,
    name: Optional[str] = None,
    overwrite: bool = False,
    target: Optional[Union[str, Path]] = None,
) -> FileResult:
    """把单个文件压进预算并写入 ``dst_dir``。

    三种结果：

    * ``copied``     —— 原本就在预算内，按字节复制，不做二次有损压缩
    * ``compressed`` —— 重新编码成功
    * ``failed``     —— 失败原因写在 ``detail`` 里（不抛异常，便于批量继续）

    ``target`` 给了就直接用这个路径，不再自己找空名字——批量并行时由
    :func:`imgfit.pipeline.plan_targets` 统一分配，避免多个 worker 抢同一个名字。
    """
    cfg = cfg or FitConfig()
    source = Path(source)
    dst_dir = Path(dst_dir)
    started = time.perf_counter()
    original_bytes = source.stat().st_size
    original_size = _probe_size(source)
    stem = name or source.stem

    def resolve(suffix: str) -> Path:
        if target is not None:
            return Path(target)
        return unique_target(dst_dir, stem, suffix, source, overwrite)

    if cfg.copy_if_fits and original_bytes <= cfg.budget:
        try:
            suffix = source.suffix.lower() or ".jpg"
            resolved = resolve(suffix)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, resolved)
            return FileResult(
                source=source,
                target=resolved,
                original_bytes=original_bytes,
                output_bytes=resolved.stat().st_size,
                original_size=original_size,
                output_size=original_size,
                quality=None,
                action=ACTION_COPIED,
                detail="原本已在预算内，按字节复制",
                elapsed=time.perf_counter() - started,
            )
        except OSError as exc:
            return FileResult(
                source=source,
                target=None,
                original_bytes=original_bytes,
                original_size=original_size,
                action=ACTION_FAILED,
                detail=f"复制失败: {exc}",
                elapsed=time.perf_counter() - started,
            )

    try:
        prepared = prepare(source, cfg)
        result = fit(prepared, cfg)
        resolved = resolve(".jpg")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(resolved, result.data)
        _verify_output(resolved, cfg.budget, result.size)
        return FileResult(
            source=source,
            target=resolved,
            original_bytes=original_bytes,
            output_bytes=len(result.data),
            original_size=original_size,
            output_size=result.size,
            quality=result.quality,
            action=ACTION_COMPRESSED,
            elapsed=time.perf_counter() - started,
        )
    except Exception as exc:  # 单张失败不应打断批量
        return FileResult(
            source=source,
            target=None,
            original_bytes=original_bytes,
            original_size=original_size,
            action=ACTION_FAILED,
            detail=f"{type(exc).__name__}: {exc}",
            elapsed=time.perf_counter() - started,
        )
