"""尺寸预算的解析与格式化。

统一约定：KB/MB 都是 **1024 进制**（与 Windows 资源管理器一致）。
但很多网申系统按 1000 进制校验，所以 CLI 默认还会再留一点余量，
见 :func:`imgfit.cli._effective_budget`。
"""

from __future__ import annotations

import re
from typing import Union

__all__ = ["SizeError", "parse_size", "format_size"]

_UNIT_FACTORS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
}

_PATTERN = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)\s*$")


class SizeError(ValueError):
    """尺寸字符串无法解析。"""


def parse_size(value: Union[str, int, float]) -> int:
    """把 ``"200kb"`` / ``"1.5MB"`` / ``"200000"`` 解析成字节数。

    >>> parse_size("200kb")
    204800
    >>> parse_size("200")
    200
    >>> parse_size(200 * 1024)
    204800
    """
    if isinstance(value, bool):  # bool 是 int 的子类，单独挡掉
        raise SizeError(f"无法识别的尺寸: {value!r}")
    if isinstance(value, (int, float)):
        size = int(round(value))
        if size <= 0:
            raise SizeError(f"尺寸必须为正数: {value!r}")
        return size

    if not isinstance(value, str):
        raise SizeError(f"无法识别的尺寸: {value!r}")

    match = _PATTERN.match(value)
    if not match:
        raise SizeError(f"无法识别的尺寸: {value!r}（示例：200kb、1.5mb、204800）")

    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit not in _UNIT_FACTORS:
        raise SizeError(f"无法识别的单位: {match.group(2)!r}（支持 b / kb / mb / gb）")

    size = int(round(number * _UNIT_FACTORS[unit]))
    if size <= 0:
        raise SizeError(f"尺寸必须为正数: {value!r}")
    return size


def format_size(size: int, precision: int = 1) -> str:
    """把字节数格式化成 ``190.2 KB`` / ``1.7 MB`` 这种便于阅读的形式。"""
    if size < 0:
        raise SizeError(f"尺寸不能为负: {size}")
    if size < 1024:
        return f"{size} B"
    for unit, factor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= factor:
            return f"{size / factor:.{precision}f} {unit}"
    return f"{size} B"
