#!/usr/bin/env python
"""把 coverage.xml 变成一张 SVG 徽章（只用标准库）。

为什么自己写而不用 genbadge/codecov：
* 不想为了一个徽章给 CI 多装一堆依赖（genbadge 会带 lxml）
* 不想把覆盖率数字托管给第三方服务，也不想为它授权一个 GitHub App

用法::

    python tools/coverage_badge.py --xml coverage.xml --out coverage.svg
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PALETTE = (
    (90, "#4c1"),        # 绿
    (80, "#97ca00"),     # 黄绿
    (70, "#dfb317"),     # 黄
    (60, "#fe7d37"),     # 橙
    (0, "#e05d44"),      # 红
)

# 11px Verdana 的平均字符宽度，够贴近真实渲染，不必引入字体测量
CHAR_WIDTH = 6.5
PADDING = 10


def read_line_rate(xml_path: Path) -> float:
    """从 coverage.xml 读总行覆盖率（0.0 ~ 1.0）。"""
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    rate = root.get("line-rate")
    if rate is None:  # pragma: no cover - 非 coverage.py 生成的 xml
        raise SystemExit(f"{xml_path} 里没有 line-rate 属性，确认它是 coverage xml")
    return max(0.0, min(1.0, float(rate)))


def pick_color(percent: int) -> str:
    for threshold, color in PALETTE:
        if percent >= threshold:
            return color
    return PALETTE[-1][1]


def make_badge(percent: int, label: str = "coverage") -> str:
    """生成 shields.io 风格的扁平徽章。"""
    value = f"{percent}%"
    label_width = round(len(label) * CHAR_WIDTH) + PADDING
    value_width = round(len(value) * CHAR_WIDTH) + PADDING
    total_width = label_width + value_width
    color = pick_color(percent)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="20" role="img" aria-label="{label}: {value}">
    <title>{label}: {value}</title>
    <linearGradient id="s" x2="0" y2="100%">
        <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
        <stop offset="1" stop-opacity=".1"/>
    </linearGradient>
    <clipPath id="r"><rect width="{total_width}" height="20" rx="3" fill="#fff"/></clipPath>
    <g clip-path="url(#r)">
        <rect width="{label_width}" height="20" fill="#555"/>
        <rect x="{label_width}" width="{value_width}" height="20" fill="{color}"/>
        <rect width="{total_width}" height="20" fill="url(#s)"/>
    </g>
    <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
        <text x="{label_width / 2:.1f}" y="14" fill="#010101" fill-opacity=".3">{label}</text>
        <text x="{label_width / 2:.1f}" y="13">{label}</text>
        <text x="{label_width + value_width / 2:.1f}" y="14" fill="#010101" fill-opacity=".3">{value}</text>
        <text x="{label_width + value_width / 2:.1f}" y="13">{value}</text>
    </g>
</svg>
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="由 coverage.xml 生成 SVG 覆盖率徽章")
    parser.add_argument("--xml", default="coverage.xml", help="coverage.py 生成的 xml 路径")
    parser.add_argument("--out", default="coverage.svg", help="输出的 svg 路径")
    parser.add_argument("--label", default="coverage", help="徽章左侧文字")
    parser.add_argument("--fail-under", type=float, default=None, help="低于该百分比则返回非零退出码")
    args = parser.parse_args(argv)

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"找不到 {xml_path}", file=sys.stderr)
        return 2

    rate = read_line_rate(xml_path)
    percent = int(rate * 100 + 0.5)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(make_badge(percent, args.label), encoding="utf-8")
    print(f"覆盖率 {percent}% → {out}")
    if args.fail_under is not None and percent < args.fail_under:
        print(f"覆盖率 {percent}% 低于门槛 {args.fail_under}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
