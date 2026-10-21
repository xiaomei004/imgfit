"""测试用合成图片。

不依赖任何外部素材：随机噪声（最坏情况）、文字版式（文档类）、
渐变+斑块（照片类），三种图覆盖了压缩算法的主要分支。
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw


def noise(size: Tuple[int, int] = (1200, 900), seed: int = 0) -> Image.Image:
    """纯随机噪声：几乎不可压缩，用来压测预算搜索的下限。"""
    rng = random.Random(seed)
    return Image.frombytes("RGB", size, rng.randbytes(size[0] * size[1] * 3))


def document(size: Tuple[int, int] = (900, 1200), seed: int = 1) -> Image.Image:
    """白底 + 成排小黑块，模拟成绩单/证书这种"文字密集"版式。"""
    im = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(im)
    rng = random.Random(seed)
    y = 40
    while y < size[1] - 40:
        x = 50
        while x < size[0] - 50:
            if rng.random() < 0.8:
                width = rng.randint(18, 80)
                draw.rectangle([x, y, x + width, y + 11], fill=(25, 25, 30))
                x += width + rng.randint(6, 18)
            else:
                x += rng.randint(12, 30)
        y += rng.randint(24, 34)
    return im


def photo(size: Tuple[int, int] = (1000, 700)) -> Image.Image:
    """渐变 + 斑块，模拟照片类素材（比噪声好压，比文字版式差）。"""
    im = Image.new("RGB", size)
    draw = ImageDraw.Draw(im)
    width, height = size
    for y in range(height):
        ratio = y / max(1, height - 1)
        draw.line([(0, y), (width, y)], fill=(int(40 + 180 * ratio), int(90 + 120 * ratio), 180))
    rng = random.Random(7)
    for _ in range(60):
        x = rng.randint(0, width)
        y = rng.randint(0, height)
        r = rng.randint(10, 60)
        draw.ellipse([x, y, x + r, y + r], fill=(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
    return im


def rgba(size: Tuple[int, int] = (400, 300), alpha: int = 0) -> Image.Image:
    """带透明通道的图（转 JPEG 时必须先拍平，否则透明会变黑）。"""
    im = Image.new("RGBA", size, (10, 120, 200, alpha))
    return im


def save(image: Image.Image, path: Path, **kwargs) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, **kwargs)
    return path
