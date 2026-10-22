"""imgfit —— 本地离线批量图片压缩。

设计目标只有一句话：**把每张图片精确压进给定字节预算，同时尽量保住可读性。**

核心引擎见 :mod:`imgfit.engine`，尺寸预算解析见 :mod:`imgfit.size`，
批量编排见 :mod:`imgfit.pipeline`。
"""

from __future__ import annotations

from .engine import (
    MODE_QUALITY,
    MODE_RESOLUTION,
    FileResult,
    FitConfig,
    FitError,
    FitResult,
    Prepared,
    compress_file,
    encode,
    fit,
    prepare,
)
from .size import SizeError, format_size, parse_size

__version__ = "0.2.0"
__all__ = [
    "__version__",
    "MODE_QUALITY",
    "MODE_RESOLUTION",
    "FileResult",
    "FitConfig",
    "FitError",
    "FitResult",
    "Prepared",
    "SizeError",
    "compress_file",
    "encode",
    "fit",
    "format_size",
    "parse_size",
    "prepare",
]
