"""批量编排：收集文件、分配预算、并发执行、生成报告。

单独拆出来是为了让 CLI 和 GUI 共用同一套逻辑——两边都不该各写一遍压缩流程。
"""

from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .engine import (
    ACTION_COMPRESSED,
    ACTION_COPIED,
    ACTION_FAILED,
    MODE_RESOLUTION,
    FileResult,
    FitConfig,
    compress_file,
)

__all__ = [
    "IMAGE_SUFFIXES",
    "ACTION_PLANNED",
    "BatchOptions",
    "BatchReport",
    "BudgetError",
    "PlanItem",
    "allocate_budgets",
    "collect_inputs",
    "default_output_dir",
    "plan_batch",
    "run_batch",
    "write_report",
]

IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".gif", ".jfif"}
)

ACTION_PLANNED = "planned"

ProgressCallback = Callable[[int, int, FileResult], None]
CancelCallback = Callable[[], bool]


class BudgetError(ValueError):
    """总预算不足以覆盖所有文件。"""


@dataclass
class BatchOptions:
    """批量任务的全部选项。"""

    output_dir: Path
    budget: Optional[int] = None          # 单张预算（字节）
    total_budget: Optional[int] = None    # 总预算（字节）；与 budget 二选一
    mode: str = MODE_RESOLUTION
    overwrite: bool = False
    recursive: bool = True
    jobs: int = 1
    copy_if_fits: bool = True
    keep_metadata: bool = False
    background: Tuple[int, int, int] = (255, 255, 255)
    min_quality: Optional[int] = None
    max_quality: int = 92
    target_quality: int = 75
    max_dim: int = 2200
    floor_dim: int = 800
    hard_min_dim: int = 320
    dim_step: int = 50
    subsampling: int = 2

    def fit_config(self, budget: int) -> FitConfig:
        return FitConfig(
            budget=budget,
            mode=self.mode,
            min_quality=self.min_quality,
            max_quality=self.max_quality,
            target_quality=self.target_quality,
            max_dim=self.max_dim,
            floor_dim=self.floor_dim,
            hard_min_dim=self.hard_min_dim,
            dim_step=self.dim_step,
            subsampling=self.subsampling,
            copy_if_fits=self.copy_if_fits,
            keep_metadata=self.keep_metadata,
            background=self.background,
        )


@dataclass
class BatchReport:
    """一次批量任务的汇总结果。"""

    results: List[FileResult] = field(default_factory=list)
    output_dir: Optional[Path] = None
    budget: Optional[int] = None
    total_budget: Optional[int] = None
    elapsed: float = 0.0

    @property
    def compressed(self) -> List[FileResult]:
        return [r for r in self.results if r.action == ACTION_COMPRESSED]

    @property
    def copied(self) -> List[FileResult]:
        return [r for r in self.results if r.action == ACTION_COPIED]

    @property
    def failed(self) -> List[FileResult]:
        return [r for r in self.results if r.action == ACTION_FAILED]

    @property
    def total_original_bytes(self) -> int:
        return sum(r.original_bytes for r in self.results)

    @property
    def total_output_bytes(self) -> int:
        return sum(r.output_bytes for r in self.results if r.ok)

    @property
    def over_budget(self) -> List[FileResult]:
        """产物意外超过预算的文件（正常应为空，用于自检）。"""
        return [r for r in self.results if r.ok and self.budget and r.output_bytes > self.budget]


@dataclass(frozen=True)
class PlanItem:
    source: Path
    original_bytes: int
    budget: int
    action: str  # "copy"（原本达标）或 "compress"
    target: Optional[Path] = None


# ---------------------------------------------------------------- 文件收集


def _iter_images(directory: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        for root, dirnames, filenames in os.walk(directory):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in sorted(filenames):
                if Path(filename).suffix.lower() in IMAGE_SUFFIXES and not filename.startswith("."):
                    yield Path(root) / filename
    else:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES and not entry.name.startswith("."):
                yield entry


def collect_inputs(
    paths: Sequence[Union[str, Path]],
    recursive: bool = True,
    exclude_dirs: Sequence[Union[str, Path]] = (),
) -> List[Path]:
    """把用户给的文件/文件夹展开成去重后的图片列表（跳过输出目录本身）。

    例外：如果"输出目录"就是某个输入目录本身（用户写了 ``-o .`` 这种），
    不做排除——否则整批文件会被静默过滤掉、一个都不处理。
    这种情况下的自我保护由 :func:`plan_targets` 负责（绝不覆盖本批次的源文件）。
    """
    input_roots = set()
    for raw in paths:
        try:
            input_roots.add(os.path.normcase(str(Path(raw).resolve())))
        except OSError:  # pragma: no cover
            continue

    excluded: List[str] = []
    for item in exclude_dirs:
        try:
            resolved = os.path.normcase(str(Path(item).resolve()))
        except OSError:  # pragma: no cover
            continue
        if resolved in input_roots:
            continue
        excluded.append(resolved)

    collected: List[Path] = []
    seen = set()

    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates = list(_iter_images(path, recursive))
        elif path.is_file():
            candidates = [path]
        else:
            continue

        for candidate in candidates:
            try:
                key = candidate.resolve()
            except OSError:  # pragma: no cover
                key = candidate
            if key in seen:
                continue
            key_text = os.path.normcase(str(key))
            if any(key_text.startswith(ex + os.sep) for ex in excluded):
                continue
            seen.add(key)
            collected.append(candidate)

    collected.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return collected


def default_output_dir(inputs: Sequence[Union[str, Path]], folder_name: str = "压缩") -> Path:
    """推断默认输出目录：一般是输入所在位置下的 ``压缩`` 文件夹。"""
    paths = [Path(p) for p in inputs]
    if not paths:
        return Path.cwd() / folder_name
    if len(paths) == 1 and paths[0].is_dir():
        return paths[0] / folder_name

    parents = []
    for path in paths:
        parents.append(str(path.parent.resolve() if path.is_file() else path.resolve()))
    try:
        common = os.path.commonpath(parents)
    except ValueError:  # Windows 跨盘符
        return Path.cwd() / folder_name
    return Path(common) / folder_name


# ---------------------------------------------------------------- 预算分配


def allocate_budgets(sizes: Sequence[int], total: int, minimum: int = 1024) -> List[int]:
    """把总预算按"装水"方式分给每个文件。

    规则：

    * 本身就不超均摊额度的文件只拿自己那份大小（反正会原样复制，不需要更多）
    * 剩下的额度继续均摊给大文件
    * 每个文件至少 ``minimum`` 字节

    这样小图不会浪费额度，大图拿到的额度更多。
    """
    n = len(sizes)
    if n == 0:
        return []
    if total < minimum * n:
        raise BudgetError(
            f"总预算 {total} 字节不足以覆盖 {n} 个文件（每个至少 {minimum} 字节）"
        )

    allocation = [0] * n
    remaining = total
    pending = set(range(n))

    while pending:
        share = remaining / len(pending)
        progressed = False
        for index in sorted(pending):
            if sizes[index] <= share:
                allocation[index] = sizes[index]
                remaining -= sizes[index]
                pending.discard(index)
                progressed = True
        if not progressed:
            share = remaining / len(pending)
            for index in pending:
                allocation[index] = int(share)
            break

    # 保证"确实需要压缩"的文件都能拿到下限；本来就很小、只需复制的文件保持原样
    for index in range(n):
        if allocation[index] < minimum < sizes[index]:
            allocation[index] = minimum

    # 取整 / 下限修正可能多出几字节，从余量最大的文件里扣回来
    overflow = sum(allocation) - total
    for index in sorted(range(n), key=lambda i: allocation[i], reverse=True):
        if overflow <= 0:
            break
        floor = min(sizes[index], minimum)
        room = allocation[index] - floor
        if room <= 0:
            continue
        take = min(room, overflow)
        allocation[index] -= take
        overflow -= take
    return allocation


# ---------------------------------------------------------------- 执行


def _canonical(path: Union[str, Path]) -> str:
    """把路径归一成可比较的字符串。

    必须两边都用同一套归一，否则在 macOS 和 Windows 上会误判成"不同文件"：

    * macOS 的临时目录 ``/var/folders/...`` 实际是 ``/private/var/...`` 的软链
    * Windows 上 ``C:\\Users\\RUNNER~1\\...`` 是长路径的 8.3 短名

    ``resolve()`` 会把它们展开成规范形式，而"拼出来的候选文件名"是原始形式，
    直接比较就会认为两者不同 —— 进而把本批次的源文件覆盖掉。
    """
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover - 极端路径异常
        return os.path.normcase(str(path))


def _reserve(
    output_dir: Path,
    stem: str,
    suffix: str,
    source: Path,
    overwrite: bool,
    used: set,
    batch_sources: set,
) -> Path:
    """在输出目录里定下一个不冲突的文件名（纯函数式，不创建文件）。

    冲突规则：

    * 本批次里已经分配过的名字不能再分配（并行时也确定）
    * 本批次的**任何源文件**都不能被覆盖，哪怕开了 ``--overwrite``
    * 磁盘上已存在且没开 overwrite → 换成 ``名字(1)``
    """
    candidate = Path(output_dir) / f"{stem}{suffix}"
    index = 1
    while True:
        key = _canonical(candidate)
        if key in batch_sources:
            taken = True
        elif key in used:
            taken = True
        elif candidate.exists() and not overwrite:
            taken = True
        else:
            taken = False
        if not taken:
            used.add(key)
            return candidate
        candidate = Path(output_dir) / f"{stem}({index}){suffix}"
        index += 1


def plan_targets(
    inputs: Sequence[Path],
    sizes: Sequence[int],
    budgets: Sequence[int],
    options: "BatchOptions",
) -> List[Path]:
    """一次性算出所有输出文件名。

    为什么要在主进程里算：并行时如果让每个 worker 自己找空名字，
    两个 worker 可能同时看到"这个名字没人用"，最终产物名就不确定了。
    统一在开始前分配好，``-j 1`` 和 ``-j 4`` 的结果才能逐字节一致。

    另外，同一次处理里出现同名文件（比如 ``A/证书.jpg`` 和 ``B/证书.jpg``）时，
    用上级目录名做前缀区分（``A_证书.jpg``），比 ``证书(1).jpg`` 更好认。
    """
    output_dir = Path(options.output_dir)
    stems: Dict[str, int] = {}
    for path in inputs:
        key = path.stem.lower()
        stems[key] = stems.get(key, 0) + 1

    batch_sources = {_canonical(path) for path in inputs}

    used: set = set()
    targets: List[Path] = []
    for source, size, budget in zip(inputs, sizes, budgets):
        keep_format = options.copy_if_fits and size <= budget
        suffix = (source.suffix.lower() or ".jpg") if keep_format else ".jpg"
        stem = source.stem
        if stems.get(stem.lower(), 0) > 1:
            parent = source.parent.name
            if parent:
                stem = f"{parent}_{stem}"
        targets.append(
            _reserve(output_dir, stem, suffix, source, options.overwrite, used, batch_sources)
        )
    return targets


def _worker(payload: Tuple[str, str, FitConfig, bool, str]) -> FileResult:
    source, dst_dir, cfg, overwrite, target = payload
    return compress_file(source, dst_dir, cfg, overwrite=overwrite, target=target)


def plan_batch(
    paths: Sequence[Union[str, Path]],
    options: BatchOptions,
) -> List[PlanItem]:
    """干运行：只判断"该复制还是该压缩"，不做任何编码。"""
    inputs = collect_inputs(paths, options.recursive, exclude_dirs=[options.output_dir])
    sizes = [path.stat().st_size for path in inputs]
    budgets = _budgets_for(sizes, options)
    targets = plan_targets(inputs, sizes, budgets, options)
    items: List[PlanItem] = []
    for path, size, budget, target in zip(inputs, sizes, budgets, targets):
        action = "copy" if (options.copy_if_fits and size <= budget) else "compress"
        items.append(
            PlanItem(source=path, original_bytes=size, budget=budget, action=action, target=target)
        )
    return items


def _budgets_for(sizes: Sequence[int], options: BatchOptions) -> List[int]:
    if options.total_budget is not None:
        minimum = max(4 * 1024, options.total_budget // (len(sizes) * 8)) if sizes else 4096
        return allocate_budgets(sizes, options.total_budget, minimum=minimum)
    budget = options.budget if options.budget is not None else 200 * 1024
    return [budget] * len(sizes)


def run_batch(
    paths: Sequence[Union[str, Path]],
    options: BatchOptions,
    progress: Optional[ProgressCallback] = None,
    should_cancel: Optional[CancelCallback] = None,
) -> BatchReport:
    """执行批量压缩。

    * 单个文件失败只记录、不打断整体
    * ``progress(done, total, result)`` 每完成一张回调一次（GUI/CLI 都用它）
    * ``should_cancel()`` 返回 True 时尽快停下，已完成的产物保留
    """
    started = time.perf_counter()
    inputs = collect_inputs(paths, options.recursive, exclude_dirs=[options.output_dir])
    report = BatchReport(
        output_dir=Path(options.output_dir),
        budget=options.budget,
        total_budget=options.total_budget,
    )
    if not inputs:
        report.elapsed = time.perf_counter() - started
        return report

    sizes = [path.stat().st_size for path in inputs]
    budgets = _budgets_for(sizes, options)
    targets = plan_targets(inputs, sizes, budgets, options)
    report.output_dir.mkdir(parents=True, exist_ok=True)

    payloads = [
        (str(path), str(options.output_dir), options.fit_config(budget), options.overwrite, str(target))
        for path, budget, target in zip(inputs, budgets, targets)
    ]
    total = len(payloads)

    if options.jobs <= 1:
        for done, payload in enumerate(payloads, start=1):
            if should_cancel and should_cancel():
                break
            result = compress_file(
                payload[0], payload[1], payload[2], overwrite=payload[3], target=payload[4]
            )
            report.results.append(result)
            if progress:
                progress(done, total, result)
    else:
        with ProcessPoolExecutor(max_workers=min(options.jobs, total)) as pool:
            futures = {pool.submit(_worker, payload): payload for payload in payloads}
            done = 0
            for future in as_completed(futures):
                if should_cancel and should_cancel():
                    for pending in futures:
                        pending.cancel()
                    break
                done += 1
                result = future.result()
                report.results.append(result)
                if progress:
                    progress(done, total, result)

    report.results.sort(key=lambda r: r.source.name.lower())
    report.elapsed = time.perf_counter() - started
    return report


# ---------------------------------------------------------------- 报告导出


REPORT_FIELDS = [
    "文件名",
    "原大小(字节)",
    "新大小(字节)",
    "原尺寸",
    "新尺寸",
    "质量",
    "处理方式",
    "输出路径",
    "耗时(秒)",
    "备注",
]


def _row(result: FileResult) -> Dict[str, object]:
    def dims(size: Optional[Tuple[int, int]]) -> str:
        return f"{size[0]}x{size[1]}" if size else ""

    action_names = {
        ACTION_COMPRESSED: "重新编码",
        ACTION_COPIED: "原样复制",
        ACTION_FAILED: "失败",
    }
    return {
        "文件名": result.source.name,
        "原大小(字节)": result.original_bytes,
        "新大小(字节)": result.output_bytes,
        "原尺寸": dims(result.original_size),
        "新尺寸": dims(result.output_size),
        "质量": result.quality if result.quality is not None else "",
        "处理方式": action_names.get(result.action, result.action),
        "输出路径": str(result.target) if result.target else "",
        "耗时(秒)": round(result.elapsed, 3),
        "备注": result.detail,
    }


def write_report(report: BatchReport, path: Union[str, Path]) -> Path:
    """把结果写成 CSV（utf-8-sig，Excel 直接打开不乱码）或 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_row(result) for result in report.results]

    if path.suffix.lower() == ".json":
        payload = {
            "输出目录": str(report.output_dir) if report.output_dir else "",
            "单张预算(字节)": report.budget,
            "总预算(字节)": report.total_budget,
            "耗时(秒)": round(report.elapsed, 3),
            "原总大小(字节)": report.total_original_bytes,
            "新总大小(字节)": report.total_output_bytes,
            "成功": len(report.results) - len(report.failed),
            "失败": len(report.failed),
            "明细": rows,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    return path
