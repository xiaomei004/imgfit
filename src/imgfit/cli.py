"""命令行界面。

    imgfit 照片文件夹                    # 压到默认 200KB，输出到 压缩/
    imgfit a.jpg b.jpg -t 500kb -o out   # 指定预算与输出目录
    imgfit 材料 -t 200kb --report r.csv  # 附带 CSV 报告
    imgfit --gui                         # 打开图形界面
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .engine import ACTION_COMPRESSED, ACTION_COPIED, ACTION_FAILED, MODE_QUALITY, MODE_RESOLUTION
from .pipeline import (
    BatchOptions,
    BudgetError,
    default_output_dir,
    plan_batch,
    run_batch,
    write_report,
)
from .size import SizeError, format_size, parse_size

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------- 文本排版


def _width(text: str) -> int:
    """终端显示宽度（中文占两格）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = max(0, width - _width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def _say(message: str = "") -> None:
    """把中文安全地写到控制台（老终端编码不支持的字符降级为 ?）。"""
    try:
        print(message)
    except UnicodeEncodeError:  # pragma: no cover - 极老终端
        enc = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
        print(message.encode(enc, "replace").decode(enc, "replace"))


def _parse_color(text: str):
    text = text.strip().lower()
    named = {"white": (255, 255, 255), "black": (0, 0, 0), "gray": (128, 128, 128), "grey": (128, 128, 128)}
    if text in named:
        return named[text]
    if text.startswith("#") and len(text) == 7:
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            pass
    raise SizeError(f"无法识别的颜色: {text!r}（示例：white / black / #F5F5F5）")


def _auto_headroom(target: int) -> int:
    """默认预留 5KB 余量。

    200KB 有两种口径：200*1024=204800 与 200*1000=200000。网申系统的校验代码
    各不相同，留出 5KB 后两种口径都能过；小目标（<100KB）按 5% 等比缩小。
    """
    return max(1024, min(5 * 1024, target // 20))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imgfit",
        description="本地离线批量图片压缩：把每张图片精确压进指定大小预算。",
        epilog=(
            "示例:\n"
            "  imgfit 材料/                        压到 200KB，输出到 材料/压缩/\n"
            "  imgfit *.jpg -t 500kb -o out/       指定预算与输出目录\n"
            "  imgfit 材料/ --total 2mb            总预算 2MB，自动分配给每张\n"
            "  imgfit 材料/ -n                     干运行，先看计划不动文件\n"
            "  imgfit --gui                        打开图形界面\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*", help="图片文件或文件夹（可多个）")
    parser.add_argument("-o", "--output", metavar="DIR", help="输出目录（默认：输入目录下的 压缩/）")
    parser.add_argument(
        "-t",
        "--target",
        metavar="SIZE",
        default="200kb",
        help="单张大小预算，如 200kb / 1.5mb / 204800（默认 200kb）",
    )
    parser.add_argument(
        "--total",
        metavar="SIZE",
        help="总大小预算，如 2mb；启用后忽略 -t，按'装水'算法分配给每张图",
    )
    parser.add_argument(
        "--headroom",
        metavar="SIZE",
        help="预留余量（默认自动：200kb 目标留 5kb，确保严格小于上限）",
    )
    parser.add_argument(
        "--mode",
        choices=(MODE_RESOLUTION, MODE_QUALITY),
        default=MODE_RESOLUTION,
        help="resolution=分辨率优先（文档/证书，默认）；quality=质量优先（照片）",
    )
    parser.add_argument("--min-quality", type=int, default=40, help="质量下限（默认 40）")
    parser.add_argument("--max-quality", type=int, default=92, help="质量上限（默认 92）")
    parser.add_argument("--target-quality", type=int, default=75, help="quality 模式的保底质量（默认 75）")
    parser.add_argument("--max-dim", type=int, default=2200, help="长边上限像素（默认 2200）")
    parser.add_argument("--floor-dim", type=int, default=800, help="长边下限像素（默认 800）")
    parser.add_argument("--dim-step", type=int, default=50, help="分辨率搜索步长（默认 50）")
    parser.add_argument(
        "--background",
        metavar="COLOR",
        default="white",
        help="透明背景填充色，转 JPEG 时透明会变黑（默认 white）",
    )
    parser.add_argument("--keep-metadata", action="store_true", help="保留 EXIF/ICC（默认全部清除）")
    parser.add_argument(
        "--no-copy",
        action="store_true",
        help="即使原图已达标也重新编码（默认：已达标则按字节复制，避免二次有损）",
    )
    parser.add_argument("-f", "--overwrite", action="store_true", help="覆盖同名产物（默认自动加 (1) 后缀）")
    parser.add_argument("--no-recursive", action="store_true", help="不递归子文件夹")
    parser.add_argument("-j", "--jobs", type=int, default=1, help="并行进程数（默认 1；大图多时可用 2~4）")
    parser.add_argument("-n", "--dry-run", action="store_true", help="只打印计划，不写任何文件")
    parser.add_argument("--report", metavar="PATH", help="把结果写成 .csv 或 .json")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出最终结论")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出更多细节")
    parser.add_argument("--gui", action="store_true", help="打开图形界面")
    parser.add_argument("--version", action="version", version=f"imgfit {__version__}")
    return parser


def _print_plan(items, output_dir: Path, budget_note: str) -> None:
    _say(f"输出目录 {output_dir}")
    _say(budget_note)
    _say()
    name_width = min(32, max([_width(i.source.name) for i in items] + [8]))
    # 只有出现重名改名（同名文件 / 上级目录前缀）时才多显示一列，平时不占地方
    renamed = [i for i in items if i.target and i.target.name != i.source.name]
    target_width = min(30, max([_width(i.target.name) for i in renamed] + [6])) if renamed else 0
    header = "  " + _pad("文件名", name_width) + _pad("原大小", 11, "right") + "  " + _pad("预算", 11, "right")
    if renamed:
        header += "  " + _pad("输出名", target_width)
    _say(header + "  动作")
    for item in items:
        action = "原样复制" if item.action == "copy" else "重新编码"
        line = (
            "  "
            + _pad(item.source.name, name_width)
            + _pad(format_size(item.original_bytes), 11, "right")
            + "  "
            + _pad(format_size(item.budget), 11, "right")
        )
        if renamed:
            renamed_target = item.target.name if item.target.name != item.source.name else ""
            line += "  " + _pad(renamed_target, target_width)
        _say(line + "  " + action)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        from .gui import main as gui_main

        return gui_main() or EXIT_OK

    if not args.inputs:
        parser.print_help()
        _say()
        _say("提示：没有指定任何输入。可以执行 `imgfit --gui` 打开图形界面。")
        return EXIT_USAGE

    try:
        target = parse_size(args.target)
        total = parse_size(args.total) if args.total else None
        headroom = parse_size(args.headroom) if args.headroom else (
            _auto_headroom(target) if total is None else 0
        )
        background = _parse_color(args.background)
    except SizeError as exc:
        parser.error(str(exc))
        return EXIT_USAGE  # pragma: no cover - parser.error 已退出

    budget = max(1, target - headroom) if total is None else target

    output_dir = Path(args.output) if args.output else default_output_dir(args.inputs)
    options = BatchOptions(
        output_dir=output_dir,
        budget=budget,
        total_budget=total,
        mode=args.mode,
        overwrite=args.overwrite,
        recursive=not args.no_recursive,
        jobs=max(1, args.jobs),
        copy_if_fits=not args.no_copy,
        keep_metadata=args.keep_metadata,
        background=background,
        min_quality=args.min_quality,
        max_quality=args.max_quality,
        target_quality=args.target_quality,
        max_dim=args.max_dim,
        floor_dim=args.floor_dim,
        dim_step=args.dim_step,
    )

    if total is not None:
        note = f"总预算 {format_size(total)}（按每张原图大小自动分配）"
    elif headroom:
        note = f"单张预算 {format_size(target)}，实际编码上限 {format_size(budget)}（预留 {format_size(headroom)} 余量）"
    else:
        note = f"单张预算 {format_size(budget)}"

    try:
        items = plan_batch(args.inputs, options)
    except BudgetError as exc:
        _say(f"错误：{exc}")
        return EXIT_USAGE

    if not items:
        _say("没有找到可处理的图片（支持 jpg/jpeg/png/bmp/webp/tif）。")
        return EXIT_PARTIAL

    if args.dry_run:
        _print_plan(items, output_dir, note)
        total_in = sum(item.original_bytes for item in items)
        _say()
        _say(
            f"计划：{len(items)} 张，共 {format_size(total_in)}；"
            f"{sum(1 for i in items if i.action == 'compress')} 张需重新编码，"
            f"{sum(1 for i in items if i.action == 'copy')} 张原样复制。"
        )
        return EXIT_OK

    total_files = len(items)
    if not args.quiet:
        _say(f"imgfit {__version__} · {note}")
        _say(f"输出目录 {output_dir}")
        _say()

    errors: List[str] = []

    def on_progress(done: int, total_count: int, result) -> None:
        if args.quiet:
            return
        name = result.source.name
        if result.action == ACTION_FAILED:
            _say(f"  [{done}/{total_count}] {_pad(name, 30)} 失败：{result.detail}")
            return
        way = "复制" if result.action == ACTION_COPIED else f"q{result.quality}"
        size_text = result.output_size
        dims = f"{size_text[0]}x{size_text[1]}" if size_text else "-"
        _say(
            f"  [{done}/{total_count}] {_pad(name, 30)} "
            f"{_pad(format_size(result.original_bytes), 10, 'right')} → "
            f"{_pad(format_size(result.output_bytes), 10, 'right')}  "
            f"{_pad(dims, 11)}  {way}"
        )

    try:
        report = run_batch(args.inputs, options, progress=on_progress)
    except BudgetError as exc:
        _say(f"错误：{exc}")
        return EXIT_USAGE

    for result in report.failed:
        errors.append(f"{result.source.name}: {result.detail}")

    ok_count = len(report.results) - len(report.failed)
    _say()
    if ok_count:
        ratio = report.total_output_bytes / report.total_original_bytes if report.total_original_bytes else 0
        _say(
            f"完成：{len(report.results)} 个文件 · 重新编码 {len(report.compressed)} · "
            f"原样复制 {len(report.copied)} · 失败 {len(report.failed)} · 耗时 {report.elapsed:.1f}s"
        )
        _say(
            f"总大小 {format_size(report.total_original_bytes)} → "
            f"{format_size(report.total_output_bytes)}（{ratio:.1%}）"
        )
        if report.over_budget:
            _say("警告：以下文件超过预算，请调大预算或报告 bug：")
            for result in report.over_budget:
                _say(f"  {result.source.name} → {format_size(result.output_bytes)}")
    else:
        _say(f"全部失败（{len(report.failed)} 个），请检查输入文件。")

    if errors:
        _say()
        _say("失败原因：")
        for line in errors:
            _say(f"  - {line}")

    _say(f"输出目录：{report.output_dir}")

    if args.report:
        try:
            written = write_report(report, args.report)
            if not args.quiet:
                _say(f"报告已写入：{written}")
        except OSError as exc:
            _say(f"警告：报告写入失败：{exc}")

    if report.failed:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
