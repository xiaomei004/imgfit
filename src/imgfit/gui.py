"""图形界面（Tkinter，零第三方依赖）。

功能与命令行完全一致：选文件 → 定预算 → 压 → 看结果 / 导出报表。
做成 GUI 是因为给同学用的时候，命令行参数没人愿意记。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from . import __version__
from .engine import ACTION_COMPRESSED, ACTION_COPIED, ACTION_FAILED, MODE_QUALITY, MODE_RESOLUTION
from .pipeline import BatchOptions, BatchReport, collect_inputs, default_output_dir, run_batch, write_report
from .size import SizeError, format_size, parse_size

MODES = {
    "分辨率优先（文档 / 证书）": MODE_RESOLUTION,
    "质量优先（照片 / 素材）": MODE_QUALITY,
}


def _open_folder(path: Path) -> None:
    """跨平台打开文件夹。"""
    target = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
    except Exception:  # pragma: no cover - 打不开就算了
        pass


class ImgFitApp:
    """主窗口。"""

    def __init__(self, root) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.files: List[Path] = []
        self.queue: "queue.Queue" = queue.Queue()
        self.cancel_event = threading.Event()
        self.report: Optional[BatchReport] = None
        self.running = False
        self._auto_output = True

        root.title(f"imgfit {__version__} · 图片批量压缩")
        root.minsize(760, 620)
        try:
            root.tk.call("tk", "scaling", 1.2)
        except Exception:  # pragma: no cover
            pass

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 界面搭建

    def _build_ui(self) -> None:
        tk, ttk = self.tk, self.ttk

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # ---- 文件列表 ----
        files_box = ttk.LabelFrame(outer, text=" 待处理文件 ", padding=8)
        files_box.pack(fill="both", expand=True)

        bar = ttk.Frame(files_box)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="添加图片…", command=self._add_files).pack(side="left")
        ttk.Button(bar, text="添加文件夹…", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self._clear_files).pack(side="left")
        self.count_label = ttk.Label(bar, text="共 0 个文件")
        self.count_label.pack(side="right")

        self.file_tree = ttk.Treeview(
            files_box, columns=("name", "size"), show="headings", height=6, selectmode="extended"
        )
        self.file_tree.heading("name", text="文件名")
        self.file_tree.heading("size", text="原大小")
        self.file_tree.column("name", width=460, anchor="w")
        self.file_tree.column("size", width=120, anchor="e")
        file_scroll = ttk.Scrollbar(files_box, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        file_scroll.pack(side="right", fill="y")

        # ---- 设置 ----
        options = ttk.LabelFrame(outer, text=" 压缩设置 ", padding=8)
        options.pack(fill="x", pady=8)
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="目标大小").grid(row=0, column=0, sticky="w")
        budget_row = ttk.Frame(options)
        budget_row.grid(row=0, column=1, sticky="we", padx=6)
        self.budget_var = tk.StringVar(value="200kb")
        self.budget_entry = ttk.Entry(budget_row, textvariable=self.budget_var, width=12)
        self.budget_entry.pack(side="left")
        for preset in ("100kb", "200kb", "500kb", "1mb"):
            ttk.Button(
                budget_row, text=preset, width=6, command=lambda v=preset: self.budget_var.set(v)
            ).pack(side="left", padx=(4, 0))
        ttk.Label(options, text="KB 按 1024 计算，另自动留 5KB 余量").grid(row=0, column=2, sticky="w")

        ttk.Label(options, text="压缩模式").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.mode_var = tk.StringVar(value="分辨率优先（文档 / 证书）")
        self.mode_box = ttk.Combobox(
            options, textvariable=self.mode_var, values=list(MODES), state="readonly", width=28
        )
        self.mode_box.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))

        ttk.Label(options, text="输出目录").grid(row=2, column=0, sticky="w", pady=(6, 0))
        out_row = ttk.Frame(options)
        out_row.grid(row=2, column=1, columnspan=2, sticky="we", padx=6, pady=(6, 0))
        self.output_var = tk.StringVar(value="")
        self.output_entry = ttk.Entry(out_row, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.output_entry.bind("<Key>", lambda _e: setattr(self, "_auto_output", False))
        ttk.Button(out_row, text="选择…", command=self._pick_output).pack(side="left", padx=(6, 0))

        flags = ttk.Frame(options)
        flags.grid(row=3, column=1, columnspan=2, sticky="w", padx=6, pady=(6, 0))
        self.keep_exif_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            flags, text="保留 EXIF/ICC（默认清除，含拍摄信息与定位）", variable=self.keep_exif_var
        ).pack(side="left")
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(flags, text="覆盖同名文件", variable=self.overwrite_var).pack(side="left", padx=12)

        # ---- 操作区 ----
        actions = ttk.Frame(outer)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="开始压缩", command=self._start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(actions, mode="determinate", length=260)
        self.progress.pack(side="left", padx=12, fill="x", expand=True)
        self.status_label = ttk.Label(actions, text="就绪")
        self.status_label.pack(side="left", padx=6)

        # ---- 结果 ----
        result_box = ttk.LabelFrame(outer, text=" 结果 ", padding=8)
        result_box.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("name", "before", "after", "dims", "how")
        self.result_tree = ttk.Treeview(result_box, columns=columns, show="headings", height=7)
        for key, title, width, anchor in (
            ("name", "文件名", 300, "w"),
            ("before", "原大小", 100, "e"),
            ("after", "新大小", 100, "e"),
            ("dims", "尺寸", 110, "center"),
            ("how", "方式", 130, "center"),
        ):
            self.result_tree.heading(key, text=title)
            self.result_tree.column(key, width=width, anchor=anchor)
        self.result_tree.tag_configure("failed", foreground="#b00020")
        self.result_tree.tag_configure("copied", foreground="#666666")
        result_scroll = ttk.Scrollbar(result_box, orient="vertical", command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_tree.pack(side="left", fill="both", expand=True)
        result_scroll.pack(side="right", fill="y")

        # ---- 底部 ----
        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(8, 0))
        self.open_button = ttk.Button(footer, text="打开输出目录", command=self._open_output, state="disabled")
        self.open_button.pack(side="left")
        self.export_button = ttk.Button(footer, text="导出 CSV…", command=self._export, state="disabled")
        self.export_button.pack(side="left", padx=6)
        self.summary_label = ttk.Label(footer, text="")
        self.summary_label.pack(side="right")

    # ------------------------------------------------------------ 文件管理

    def _refresh_files(self) -> None:
        self.file_tree.delete(*self.file_tree.get_children())
        for path in self.files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            self.file_tree.insert("", "end", values=(path.name, format_size(size)))
        self.count_label.configure(text=f"共 {len(self.files)} 个文件")
        if self._auto_output and self.files:
            self.output_var.set(str(default_output_dir(self.files)))

    def _add_files(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(
            title="选择图片",
            filetypes=[
                ("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff"),
                ("所有文件", "*.*"),
            ],
        )
        if not chosen:
            return
        existing = {str(p).lower() for p in self.files}
        for item in chosen:
            path = Path(item)
            if str(path).lower() not in existing:
                self.files.append(path)
                existing.add(str(path).lower())
        self._refresh_files()

    def _add_folder(self) -> None:
        from tkinter import filedialog, messagebox

        folder = filedialog.askdirectory(title="选择文件夹")
        if not folder:
            return
        output = Path(self.output_var.get()) if self.output_var.get() else None
        found = collect_inputs([folder], recursive=True, exclude_dirs=[output] if output else [])
        if not found:
            messagebox.showinfo("imgfit", "这个文件夹里没有找到图片。")
            return
        existing = {str(p).lower() for p in self.files}
        for path in found:
            if str(path).lower() not in existing:
                self.files.append(path)
                existing.add(str(path).lower())
        self._refresh_files()

    def _clear_files(self) -> None:
        if self.running:
            return
        self.files.clear()
        self.report = None
        self.result_tree.delete(*self.result_tree.get_children())
        self.summary_label.configure(text="")
        self.progress.configure(value=0)
        self.status_label.configure(text="就绪")
        self.open_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self._refresh_files()

    def _pick_output(self) -> None:
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self._auto_output = False
            self.output_var.set(folder)

    # ------------------------------------------------------------ 执行

    def _options(self) -> BatchOptions:
        budget = parse_size(self.budget_var.get())
        output = Path(self.output_var.get() or default_output_dir(self.files))
        return BatchOptions(
            output_dir=output,
            budget=budget,
            mode=MODES.get(self.mode_var.get(), MODE_RESOLUTION),
            overwrite=self.overwrite_var.get(),
            recursive=True,
            jobs=1,  # GUI 里保持单进程，避免 Windows 下多进程与 Tk 同存的坑
            keep_metadata=self.keep_exif_var.get(),
        )

    def _start(self) -> None:
        from tkinter import messagebox

        if self.running:
            return
        if not self.files:
            messagebox.showwarning("imgfit", "先添加要压缩的图片。")
            return
        try:
            options = self._options()
        except SizeError as exc:
            messagebox.showerror("目标大小有误", str(exc))
            return

        self._auto_output = False
        self.output_var.set(str(options.output_dir))
        self.result_tree.delete(*self.result_tree.get_children())
        self.summary_label.configure(text="")
        self.report = None
        self.progress.configure(value=0, maximum=len(self.files))
        self.cancel_event.clear()
        self._set_running(True)

        files = list(self.files)
        thread = threading.Thread(target=self._worker, args=(files, options), daemon=True)
        thread.start()
        self.root.after(100, self._drain)

    def _worker(self, files: List[Path], options: BatchOptions) -> None:
        try:
            report = run_batch(
                files,
                options,
                progress=lambda done, total, result: self.queue.put(("progress", (done, total, result))),
                should_cancel=self.cancel_event.is_set,
            )
            self.queue.put(("done", report))
        except Exception as exc:  # 兜底：任何异常都别让界面卡死
            self.queue.put(("error", exc))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    done, total, result = payload
                    self.progress.configure(maximum=total, value=done)
                    self.status_label.configure(text=f"处理中 {done}/{total}")
                    self._insert_result(result)
                elif kind == "done":
                    self._finish(payload)
                elif kind == "error":
                    self._set_running(False)
                    self.status_label.configure(text="出错")
                    from tkinter import messagebox

                    messagebox.showerror("imgfit", f"处理失败：{payload}")
        except queue.Empty:
            pass
        if self.running:
            self.root.after(100, self._drain)

    def _insert_result(self, result) -> None:
        if result.action == ACTION_FAILED:
            self.result_tree.insert(
                "", "end",
                values=(result.source.name, format_size(result.original_bytes), "-", "-", "失败"),
                tags=("failed",),
            )
            return
        dims = f"{result.output_size[0]}x{result.output_size[1]}" if result.output_size else "-"
        how = "原样复制" if result.action == ACTION_COPIED else f"重新编码 q{result.quality}"
        tags = ("copied",) if result.action == ACTION_COPIED else ()
        self.result_tree.insert(
            "", "end",
            values=(
                result.source.name,
                format_size(result.original_bytes),
                format_size(result.output_bytes),
                dims,
                how,
            ),
            tags=tags,
        )
        self.result_tree.see(self.result_tree.get_children()[-1])

    def _finish(self, report: BatchReport) -> None:
        self.report = report
        self._set_running(False)
        self.progress.configure(value=self.progress.cget("maximum"))
        failed = len(report.failed)
        if failed:
            self.status_label.configure(text=f"完成，{failed} 个失败")
        else:
            self.status_label.configure(text="完成")
        ratio = (
            report.total_output_bytes / report.total_original_bytes if report.total_original_bytes else 0
        )
        self.summary_label.configure(
            text=(
                f"{len(report.results)} 个文件 · 失败 {failed} · "
                f"{format_size(report.total_original_bytes)} → {format_size(report.total_output_bytes)}"
                f"（{ratio:.0%}）· {report.elapsed:.1f}s"
            )
        )
        self.open_button.configure(state="normal")
        self.export_button.configure(state="normal")
        if failed:
            from tkinter import messagebox

            lines = [f"· {r.source.name}: {r.detail}" for r in report.failed[:8]]
            if failed > 8:
                lines.append(f"…… 还有 {failed - 8} 个")
            messagebox.showwarning("有文件失败", "以下文件没能压进预算：\n\n" + "\n".join(lines))

    def _cancel(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        self.status_label.configure(text="正在取消…")

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        if running:
            self.budget_entry.configure(state="disabled")
            self.output_entry.configure(state="disabled")
            self.mode_box.configure(state="disabled")
        else:
            self.budget_entry.configure(state="normal")
            self.output_entry.configure(state="normal")
            self.mode_box.configure(state="readonly")

    def _open_output(self) -> None:
        if self.report and self.report.output_dir:
            _open_folder(self.report.output_dir)

    def _export(self) -> None:
        from tkinter import filedialog, messagebox

        if not self.report:
            return
        path = filedialog.asksaveasfilename(
            title="导出报告",
            defaultextension=".csv",
            filetypes=[("CSV 表格", "*.csv"), ("JSON", "*.json")],
            initialfile="压缩报告.csv",
        )
        if not path:
            return
        try:
            written = write_report(self.report, path)
            messagebox.showinfo("imgfit", f"报告已保存：\n{written}")
        except OSError as exc:
            messagebox.showerror("imgfit", f"保存失败：{exc}")

    def _on_close(self) -> None:
        from tkinter import messagebox

        if self.running:
            if not messagebox.askyesno("imgfit", "还在压缩中，确定要退出吗？"):
                return
            self.cancel_event.set()
        self.root.destroy()


def main() -> int:
    """GUI 入口。"""
    try:
        import tkinter as tk
    except ImportError:  # pragma: no cover - 极少见
        print("当前 Python 缺少 tkinter，请改用命令行：imgfit --help")
        return 1

    root = tk.Tk()
    ImgFitApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
