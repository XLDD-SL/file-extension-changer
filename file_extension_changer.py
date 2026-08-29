#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件后缀转换器（File Extension Changer）
========================================

一个仅使用 Python 标准库（零第三方依赖）的本地批量改后缀小工具：
将选中的文件（默认筛选 .jpg / .jpeg / .png，也允许任意类型）的扩展名
批量改为 .zip / .7z 或自定义后缀。全程只执行文件系统重命名操作，
不读取、不转换、不压缩文件内容。

运行说明
--------
Windows:
    安装 Python 3.8+（官方安装包自带 tkinter），命令行执行：
        python file_extension_changer.py
    或直接双击运行。

Linux:
    需先安装 tkinter（Debian/Ubuntu:  sudo apt install python3-tk
                    Fedora:           sudo dnf install python3-tkinter），然后：
        python3 file_extension_changer.py

安全说明
--------
- 本工具只调用 os.rename 修改文件名（同目录内纯元数据操作），
  绝不 open / 读写被操作文件的内容；也刻意不使用 shutil.move——
  shutil.move 跨磁盘时会退化为“复制 + 删除”，会真正触碰文件内容。
- 目标文件名冲突时自动追加序号（photo_1.zip、photo_2.zip ...），
  任何情况下都不会覆盖已存在的文件。
- 每次转换的批次会被记录，「撤销上次转换」可一键恢复原始扩展名。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set

# tkinter 缺失时给出友好提示（常见于 Linux 未安装 python3-tk），
# 避免双击运行时窗口一闪而过、看不到任何错误信息。
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "未找到 tkinter 图形库，无法启动界面。\n"
        "Windows：请使用 Python 官方安装包重新安装，并勾选 “tcl/tk and IDLE” 组件。\n"
        "Linux：Debian/Ubuntu 请执行 sudo apt install python3-tk；\n"
        "        Fedora 请执行 sudo dnf install python3-tkinter。\n"
    )
    try:
        input("按回车键退出...")
    except EOFError:
        pass
    sys.exit(1)

# ======================================================================
# ① 常量与配置
# ======================================================================

APP_TITLE = "文件后缀转换器"
APP_VERSION = "1.0.0"

#: 转换前的安全确认文案（需求规定的固定文案，请勿修改）
CONFIRM_MESSAGE = (
    "修改扩展名不会改变文件内容，但可能导致文件无法被原始程序打开。\n确定要继续吗？"
)

#: 预设目标后缀（下拉菜单项，显示时带点）
PRESET_SUFFIXES = (".zip", ".7z")
DEFAULT_SUFFIX = "zip"

#: “添加文件”对话框的筛选档：默认图片格式，可切换为所有文件
FILETYPES = (
    ("图片文件", ("*.jpg", "*.jpeg", "*.png")),
    ("所有文件", ("*", "*")),
)

#: 自定义后缀中不允许出现的字符（含各类空白字符）
ILLEGAL_SUFFIX_CHARS = set('\\/:*?"<>|') | set(" \t\r\n\f\v")

#: 会话日志文件名（写入系统临时目录）
LOG_FILENAME = "file_extension_changer.log"

# ---- 行状态 ----
ST_PENDING = "待转换"
ST_CONVERTED = "已转换"
ST_SKIPPED = "已跳过"
ST_FAILED = "转换失败"
ST_RESTORED = "已恢复"

#: 状态 → Treeview 标签（用于给不同状态的行着色）
ROW_TAG = {
    ST_PENDING: "pending",
    ST_CONVERTED: "converted",
    ST_SKIPPED: "skipped",
    ST_FAILED: "failed",
    ST_RESTORED: "restored",
}

# ---- 工作线程 → 主线程 的消息类型（经 queue.Queue 传递，保证线程安全）----
M_LOG = "LOG"                    # (M_LOG, 格式化后的日志行)
M_PROGRESS = "PROGRESS"          # (M_PROGRESS, 已完成数, 总数, 来源"convert"/"undo")
M_ROW_OK = "ROW_OK"              # (M_ROW_OK, iid, 旧路径字符串, 新路径字符串)
M_ROW_SKIP = "ROW_SKIP"          # (M_ROW_SKIP, iid)
M_ROW_FAIL = "ROW_FAIL"          # (M_ROW_FAIL, iid)
M_FINISHED = "FINISHED"          # (M_FINISHED, 成功数, 跳过数, 失败数)  成功数=-1 表示意外中止
M_UNDO_OK = "UNDO_OK"            # (M_UNDO_OK, iid, 恢复后的原路径字符串)
M_UNDO_FAIL = "UNDO_FAIL"        # (M_UNDO_FAIL, iid)
M_UNDO_FINISHED = "UNDO_FINISHED"  # (M_UNDO_FINISHED, 成功数, 失败数)

#: Windows 下使用微软雅黑保证中文渲染统一；其他平台交给 Tk 自动选择
UI_FONT = ("Microsoft YaHei", 9) if sys.platform == "win32" else None


# ======================================================================
# ② 数据结构
# ======================================================================

@dataclass
class ItemRecord:
    """文件列表中的一条记录。"""

    iid: str            # 对应 Treeview 行标识
    initial_path: Path  # 添加时的原始路径（对照信息，不随转换变化）
    current_path: Path  # 当前实际路径（转换后更新为新路径，撤销后还原）
    status: str = ST_PENDING

    @property
    def ext_display(self) -> str:
        """「当前扩展名」列的显示文本（无扩展名时显示占位符）。"""
        return self.current_path.suffix or "（无）"


# ======================================================================
# ③ 核心逻辑层（纯函数，不依赖 tkinter，可独立测试）
# ======================================================================

def normalize_suffix(raw: str) -> str:
    """规整用户输入的后缀：去掉首尾空白与前导点，并转为小写。"""
    return raw.strip().lstrip(".").lower()


def is_valid_suffix(suffix: str) -> bool:
    """校验规整后的后缀是否合法：非空，且不含非法字符（含空白）。"""
    return bool(suffix) and not any(ch in ILLEGAL_SUFFIX_CHARS for ch in suffix)


def preview_name(path: Path, suffix: str) -> str:
    """返回重命名后的目标文件名（仅文件名部分），用于列表预览列。"""
    return path.stem + "." + suffix


def unique_target(path: Path, suffix: str) -> Path:
    """
    生成同目录下不冲突的目标路径。

    这是防止覆盖已有文件的安全防线：Windows 上 os.rename 遇到同名
    目标会抛 FileExistsError，而 Linux 上会“静默覆盖”，因此必须先
    探测存在性并追加序号（photo_1.zip、photo_2.zip ...）。

    注：Path.exists() 基于 os.stat，天然遵循各平台的大小写语义
    （Windows 不敏感 / Linux 敏感），无需额外特判。
    """
    target = path.with_name(path.stem + "." + suffix)
    if not target.exists():
        return target
    index = 1
    while True:
        candidate = path.with_name("{}_{}.{}".format(path.stem, index, suffix))
        if not candidate.exists():
            return candidate
        index += 1


def convert_worker(records: List[ItemRecord], suffix: str, msg_queue: "queue.Queue") -> None:
    """
    工作线程：逐个执行重命名（只做 os.rename，绝不触碰文件内容）。

    通过 msg_queue 向主线程发送进度与结果消息；子线程内只允许
    logging 和队列写入，绝不允许直接操作任何 tkinter 控件
    （Tkinter 非线程安全）。

    消息格式见模块顶部常量定义。
    """
    total = len(records)
    ok = skip = fail = 0
    for idx, rec in enumerate(records, start=1):
        try:
            current = rec.current_path
            if current.suffix.lower() == "." + suffix:
                # 已是目标后缀：跳过而非加序号（photo.zip -> photo_1.zip 语义混乱）
                skip += 1
                logging.info("跳过：%s —— 已是 .%s 格式", current.name, suffix)
                msg_queue.put((M_ROW_SKIP, rec.iid))
            else:
                target = unique_target(current, suffix)
                # 同目录重命名：纯元数据操作，文件 inode/MFT 记录不变
                os.rename(current, target)
                ok += 1
                logging.info("成功：%s → %s", current.name, target.name)
                msg_queue.put((M_ROW_OK, rec.iid, str(current), str(target)))
        except FileNotFoundError:
            fail += 1
            logging.error("失败：%s —— 路径不存在（文件可能已被移动或删除）", rec.current_path)
            msg_queue.put((M_ROW_FAIL, rec.iid))
        except PermissionError:
            fail += 1
            logging.error("失败：%s —— 权限不足或文件被占用", rec.current_path)
            msg_queue.put((M_ROW_FAIL, rec.iid))
        except FileExistsError:
            # unique_target 已尽量避免，这里是“探测-重命名”间隙的兜底防线
            fail += 1
            logging.error("失败：%s —— 目标文件名已存在，为避免覆盖已放弃", rec.current_path)
            msg_queue.put((M_ROW_FAIL, rec.iid))
        except OSError as exc:
            fail += 1
            logging.error("失败：%s —— 系统错误：%s", rec.current_path, exc)
            msg_queue.put((M_ROW_FAIL, rec.iid))
        msg_queue.put((M_PROGRESS, idx, total, "convert"))
    msg_queue.put((M_FINISHED, ok, skip, fail))


def undo_worker(batch: List[dict], msg_queue: "queue.Queue") -> None:
    """
    工作线程：撤销一个批次的转换，把每个文件从“新路径”改回“原路径”。

    batch 为撤销开始时主线程对栈顶批次的快照，元素结构：
        {"iid": 行标识, "old": 原路径 Path, "new": 当前路径 Path}
    """
    total = len(batch)
    ok = fail = 0
    for idx, item in enumerate(batch, start=1):
        old: Path = item["old"]
        new: Path = item["new"]
        try:
            # 撤销前的两条防线：新文件必须还在，原路径必须空闲。
            # Linux 上 os.rename 会静默覆盖，所以“原路径冲突”必须显式预检查。
            if not new.exists():
                raise FileNotFoundError(new)
            if old.exists():
                raise FileExistsError(old)
            os.rename(new, old)
            ok += 1
            logging.info("撤销成功：%s → %s", new.name, old.name)
            msg_queue.put((M_UNDO_OK, item["iid"], str(old)))
        except FileNotFoundError:
            fail += 1
            logging.error("撤销失败：%s —— 文件不存在（可能已被移动或删除）", new)
            msg_queue.put((M_UNDO_FAIL, item["iid"]))
        except FileExistsError:
            fail += 1
            logging.error("撤销失败：%s —— 原路径已存在同名文件，发生冲突", old)
            msg_queue.put((M_UNDO_FAIL, item["iid"]))
        except PermissionError:
            fail += 1
            logging.error("撤销失败：%s —— 权限不足或文件被占用", new)
            msg_queue.put((M_UNDO_FAIL, item["iid"]))
        except OSError as exc:
            fail += 1
            logging.error("撤销失败：%s —— 系统错误：%s", new, exc)
            msg_queue.put((M_UNDO_FAIL, item["iid"]))
        msg_queue.put((M_PROGRESS, idx, total, "undo"))
    msg_queue.put((M_UNDO_FINISHED, ok, fail))


# ======================================================================
# ④ 日志层：logging → 线程安全队列 → 主线程写入界面
# ======================================================================

class QueueLogHandler(logging.Handler):
    """把 logging 记录转发到线程安全队列，由主线程统一写入界面日志区。"""

    def __init__(self, msg_queue: "queue.Queue") -> None:
        super().__init__()
        self._msg_queue = msg_queue
        self.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._msg_queue.put((M_LOG, self.format(record)))
        except Exception:  # pragma: no cover - logging 兜底，避免日志异常引发递归
            self.handleError(record)


def setup_logging(msg_queue: "queue.Queue") -> Path:
    """
    配置全局 logging：
      - QueueLogHandler → 界面日志区（经队列转发，线程安全）
      - FileHandler     → 系统临时目录下的会话日志文件（便于排查问题）
    返回日志文件路径。
    """
    log_path = Path(tempfile.gettempdir()) / LOG_FILENAME
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    queue_handler = QueueLogHandler(msg_queue)
    root.addHandler(queue_handler)

    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
        )
    )
    root.addHandler(file_handler)
    return log_path


# ======================================================================
# 帮助文本
# ======================================================================

HELP_TEXT = """【文件后缀转换器 · 使用说明】

一、基本操作
  1. 点击「添加文件」选择一个或多个文件（默认筛选图片格式，
     可在对话框中切换为“所有文件”）。
  2. 在「目标格式」下拉框选择 .zip / .7z，或在“自定义后缀”输入框
     填写其他后缀（无需带点，如 tar.gz；留空则使用下拉框的值）。
  3. 确认列表中「新文件名预览」无误后，点击「开始转换」，
     并在安全确认框中点击“是”。
  4. 转换完成后，点击「撤销上次转换」可把本批次文件恢复为
     原始扩展名（可多次点击，逐批回退）。

二、安全说明
  · 本工具只修改文件名，不读取、不修改文件内容。
  · 修改扩展名不会破坏文件数据，但可能导致文件无法被原始程序
    直接打开（改回后缀即可恢复）。
  · 目标文件名已存在时，会自动追加序号（如 photo_1.zip），
    绝不覆盖已有文件。
  · 已转换的文件不能从列表移除，需先撤销转换。

三、运行环境
  · Windows：Python 3.8+ 官方安装包自带 tkinter，直接运行：
        python file_extension_changer.py
  · Linux：需先安装 tkinter——
        Debian/Ubuntu: sudo apt install python3-tk
        Fedora:        sudo dnf install python3-tkinter
    然后运行：
        python3 file_extension_changer.py

四、其他
  · 「保存列表」可把当前文件清单导出为文本文件，便于核对。
  · 会话日志文件：{log_path}
"""


# ======================================================================
# ⑤ UI 层
# ======================================================================

class FileExtensionChangerApp(tk.Tk):
    """主窗口：界面布局、事件处理与线程消息调度。"""

    def __init__(self) -> None:
        super().__init__()
        self.title("{0} v{1}".format(APP_TITLE, APP_VERSION))
        self.geometry("880x640")
        self.minsize(720, 480)

        # ---- 运行期状态 ----
        self.msg_queue: "queue.Queue" = queue.Queue()
        self._records: Dict[str, ItemRecord] = {}  # iid -> 记录
        self._dedup: Set[str] = set()              # normcase(路径字符串) 集合，用于去重
        self.undo_stack: List[List[dict]] = []     # 撤销批次栈，栈顶 = 最近一次转换
        self._busy = False                         # 是否有工作线程在执行
        self._current_batch: List[dict] = None     # 正在累积的转换批次（转换期间非 None）

        log_path = setup_logging(self.msg_queue)
        logging.info("%s v%s 已启动", APP_TITLE, APP_VERSION)
        logging.info("会话日志文件：%s", log_path)

        if UI_FONT:
            # Windows 上统一使用微软雅黑，保证中文标签显示一致
            ttk.Style(self).configure(".", font=UI_FONT)

        self._build_menubar()
        self._build_ui()
        self._refresh_controls()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-o>", lambda _event: self._add_files())

        # Tkinter 非线程安全：主线程以 100ms 周期轮询队列，消费工作线程消息
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_menubar(self) -> None:
        """构建菜单栏：文件（添加/保存/退出）与帮助（使用说明）。"""
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="添加文件…", accelerator="Ctrl+O", command=self._add_files)
        file_menu.add_command(label="保存列表…", command=self._save_list)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="使用说明", command=self._show_help)
        menubar.add_cascade(label="帮助", menu=help_menu)

        self.configure(menu=menubar)

    def _build_ui(self) -> None:
        """构建主布局：工具栏 / 文件列表 / 格式行 / 操作行 / 进度行 / 日志区。"""
        container = ttk.Frame(self, padding=8)
        container.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        # 列表区与日志区随窗口缩放伸缩
        container.rowconfigure(1, weight=3)
        container.rowconfigure(5, weight=1)
        container.columnconfigure(0, weight=1)

        self._build_toolbar(container)
        self._build_tree(container)
        self._build_format_row(container)
        self._build_action_row(container)
        self._build_progress_row(container)
        self._build_log_area(container)

    def _build_toolbar(self, parent: ttk.Frame) -> None:
        """工具按钮区：添加 / 移除 / 清空 / 保存。"""
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew")
        self.btn_add = ttk.Button(bar, text="添加文件…", command=self._add_files)
        self.btn_remove = ttk.Button(bar, text="移除选中", command=self._remove_selected)
        self.btn_clear = ttk.Button(bar, text="清空列表", command=self._clear_list)
        self.btn_save = ttk.Button(bar, text="保存列表", command=self._save_list)
        for i, btn in enumerate((self.btn_add, self.btn_remove, self.btn_clear, self.btn_save)):
            btn.pack(side="left", padx=(0, 8), pady=(0, 6))

    def _build_tree(self, parent: ttk.Frame) -> None:
        """文件列表：四列表格 + 垂直滚动条，支持多选。"""
        frame = ttk.Frame(parent)
        frame.grid(row=1, column=0, sticky="nsew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        columns = ("path", "ext", "new", "status")
        self.tree = ttk.Treeview(
            frame, columns=columns, show="headings",
            selectmode="extended", height=12,
        )
        for col, title in (
            ("path", "原文件路径"),
            ("ext", "当前扩展名"),
            ("new", "新文件名预览"),
            ("status", "状态"),
        ):
            self.tree.heading(col, text=title)
        self.tree.column("path", width=340, anchor="w")
        self.tree.column("ext", width=95, minwidth=75, anchor="center", stretch=False)
        self.tree.column("new", width=200, anchor="w")
        self.tree.column("status", width=85, minwidth=75, anchor="center", stretch=False)

        # 不同状态的行着色，便于一眼区分结果
        self.tree.tag_configure("converted", foreground="#1a7f37")
        self.tree.tag_configure("failed", foreground="#c00000")
        self.tree.tag_configure("restored", foreground="#0b5394")
        self.tree.tag_configure("skipped", foreground="#9a6700")

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

    def _build_format_row(self, parent: ttk.Frame) -> None:
        """目标格式行：预设下拉 + 自定义输入。"""
        row = ttk.Frame(parent)
        row.grid(row=2, column=0, sticky="ew", pady=(8, 4))

        ttk.Label(row, text="目标格式：").pack(side="left")
        self.suffix_var = tk.StringVar(value=PRESET_SUFFIXES[0])
        self.suffix_combo = ttk.Combobox(
            row, textvariable=self.suffix_var,
            values=list(PRESET_SUFFIXES), state="readonly", width=8,
        )
        self.suffix_combo.pack(side="left", padx=(0, 16))
        self.suffix_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_previews())

        ttk.Label(row, text="自定义后缀：").pack(side="left")
        self.custom_var = tk.StringVar()
        self.custom_entry = ttk.Entry(row, textvariable=self.custom_var, width=14)
        self.custom_entry.pack(side="left", padx=(4, 6))
        # 输入过程中实时刷新预览；失焦时做合法性校验
        self.custom_entry.bind("<KeyRelease>", lambda _e: self._refresh_previews())
        self.custom_entry.bind("<FocusOut>", self._validate_custom_suffix)
        ttk.Label(row, text="（可输入其他后缀，无需带点；留空则使用左侧选择）").pack(side="left")

    def _build_action_row(self, parent: ttk.Frame) -> None:
        """操作按钮行：开始转换 / 撤销上次转换。"""
        row = ttk.Frame(parent)
        row.grid(row=3, column=0, sticky="ew", pady=(0, 6))

        self.btn_convert = ttk.Button(row, text="开始转换", command=self._start_convert)
        self.btn_convert.pack(side="left")
        self.btn_undo = ttk.Button(row, text="撤销上次转换", command=self._undo_last)
        self.btn_undo.pack(side="left", padx=(8, 0))

    def _build_progress_row(self, parent: ttk.Frame) -> None:
        """进度行：进度条 + 计数 + 右侧状态文字。"""
        row = ttk.Frame(parent)
        row.grid(row=4, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(row, text="进度：").pack(side="left")
        self.progress = ttk.Progressbar(row, length=320, mode="determinate")
        self.progress.pack(side="left", padx=(0, 8))
        self.progress_label = ttk.Label(row, text="0/0")
        self.progress_label.pack(side="left")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(row, textvariable=self.status_var).pack(side="right")

    def _build_log_area(self, parent: ttk.Frame) -> None:
        """底部日志区：只读 Text + 滚动条，新日志自动滚动到底部。"""
        group = ttk.LabelFrame(parent, text="操作日志")
        group.grid(row=5, column=0, sticky="nsew")
        group.rowconfigure(0, weight=1)
        group.columnconfigure(0, weight=1)

        font_kw = {"font": UI_FONT} if UI_FONT else {}
        self.log_text = tk.Text(
            group, height=8, wrap="word", state="disabled",
            relief="sunken", bd=1, **font_kw,
        )
        sb = ttk.Scrollbar(group, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

    # ------------------------------------------------------------------
    # 状态刷新辅助
    # ------------------------------------------------------------------

    def _current_suffix(self) -> str:
        """
        计算当前生效的目标后缀（不带点）。
        自定义输入框非空时优先；为空则使用下拉框选择值。
        """
        custom = normalize_suffix(self.custom_var.get())
        if custom:
            return custom
        preset = normalize_suffix(self.suffix_var.get())
        return preset or DEFAULT_SUFFIX

    def _validate_custom_suffix(self, _event=None) -> None:
        """自定义后缀失焦校验：不合法时弹窗警告并清空（回退到下拉框值）。"""
        raw = self.custom_var.get().strip()
        if not raw:
            return
        if not is_valid_suffix(normalize_suffix(raw)):
            messagebox.showwarning(
                "后缀不合法",
                "“{0}” 不是合法的后缀。\n\n"
                "后缀不能包含 \\ / : * ? \" < > | 或空白字符。".format(raw),
            )
            self.custom_var.set("")

    def _update_row(self, rec: ItemRecord) -> None:
        """按记录当前状态刷新表格行（扩展名 / 预览 / 状态 / 颜色）。"""
        self.tree.set(rec.iid, "ext", rec.ext_display)
        if rec.status == ST_PENDING:
            # 待转换：预览列显示将要变成的目标文件名
            self.tree.set(rec.iid, "new", preview_name(rec.current_path, self._current_suffix()))
        else:
            # 其他状态：显示当前实际文件名（转换后的名字或恢复后的原名）
            self.tree.set(rec.iid, "new", rec.current_path.name)
        self.tree.set(rec.iid, "status", rec.status)
        self.tree.item(rec.iid, tags=(ROW_TAG.get(rec.status, ""),))

    def _refresh_previews(self) -> None:
        """目标格式变化后：刷新所有未转换行的预览列；跳过/失败/已恢复行回到待转换。"""
        suffix = self._current_suffix()
        for rec in list(self._records.values()):
            if rec.status == ST_CONVERTED:
                continue  # 已转换行保持既成结果，不动
            if rec.status != ST_PENDING:
                rec.status = ST_PENDING  # 新目标下重新可转换
            self.tree.set(rec.iid, "new", preview_name(rec.current_path, suffix))
            self.tree.set(rec.iid, "status", rec.status)
            self.tree.item(rec.iid, tags=(ROW_TAG[rec.status],))

    def _refresh_controls(self) -> None:
        """根据忙碌状态与撤销栈内容，集中刷新所有可交互控件的可用性。"""
        if self._busy:
            for btn in (self.btn_add, self.btn_remove, self.btn_clear,
                        self.btn_save, self.btn_convert, self.btn_undo):
                btn.configure(state="disabled")
            self.suffix_combo.configure(state="disabled")
            self.custom_entry.configure(state="disabled")
        else:
            for btn in (self.btn_add, self.btn_remove, self.btn_clear,
                        self.btn_save, self.btn_convert):
                btn.configure(state="normal")
            self.btn_undo.configure(
                state="normal" if self.undo_stack else "disabled"
            )
            self.suffix_combo.configure(state="readonly")
            self.custom_entry.configure(state="normal")

    def _update_count_status(self) -> None:
        """空闲时在状态栏显示当前文件数量。"""
        if not self._busy:
            self.status_var.set(
                "共 {0} 个文件".format(len(self._records)) if self._records else "就绪"
            )

    # ------------------------------------------------------------------
    # 文件列表操作
    # ------------------------------------------------------------------

    def _add_files(self) -> None:
        """弹出多选文件对话框，去重后加入列表。"""
        if self._busy:
            return
        paths = filedialog.askopenfilenames(
            parent=self, title="选择要添加的文件", filetypes=FILETYPES,
        )
        if not paths:
            return
        suffix = self._current_suffix()
        added = skipped = 0
        for raw in paths:
            path = Path(raw)
            key = os.path.normcase(str(path))  # Windows 下忽略大小写差异去重
            if key in self._dedup:
                skipped += 1
                continue
            rec = ItemRecord(iid="", initial_path=path, current_path=path)
            rec.iid = self.tree.insert(
                "", "end",
                values=(str(path), rec.ext_display,
                        preview_name(path, suffix), ST_PENDING),
                tags=(ROW_TAG[ST_PENDING],),
            )
            self._records[rec.iid] = rec
            self._dedup.add(key)
            added += 1

        message = "已添加 {0} 个文件".format(added)
        if skipped:
            message += "，{0} 个重复文件已跳过".format(skipped)
        logging.info(message)
        self._update_count_status()

    def _remove_selected(self) -> None:
        """移除选中的行；已转换的行不允许移除（需先撤销）。"""
        if self._busy:
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "请先在列表中选中要移除的文件。")
            return
        removed = blocked = 0
        for iid in selection:
            rec = self._records.get(iid)
            if rec is None:
                continue
            if rec.status == ST_CONVERTED:
                blocked += 1
                logging.warning(
                    "无法移除：%s —— 该文件已转换，请先「撤销上次转换」",
                    rec.initial_path.name,
                )
                continue
            self.tree.delete(iid)
            self._dedup.discard(os.path.normcase(str(rec.initial_path)))
            del self._records[iid]
            removed += 1
        if blocked:
            logging.warning("有 %d 个已转换文件未移除", blocked)
        logging.info("已从列表移除 %d 个文件", removed)
        self._update_count_status()

    def _clear_list(self) -> None:
        """清空整个列表；存在已转换行时先弹窗确认（文件系统不受影响）。"""
        if self._busy:
            return
        if not self._records:
            return
        has_converted = any(r.status == ST_CONVERTED for r in self._records.values())
        if has_converted:
            proceed = messagebox.askyesno(
                APP_TITLE,
                "列表中存在已转换的文件。\n\n"
                "清空列表只影响界面显示，不会改动文件本身；\n"
                "之后仍可通过「撤销上次转换」恢复文件。\n\n确定清空列表吗？",
            )
            if not proceed:
                return
        count = len(self._records)
        self.tree.delete(*self.tree.get_children())
        self._records.clear()
        self._dedup.clear()
        logging.info("已清空列表（%d 个文件）", count)
        self._update_count_status()

    def _save_list(self) -> None:
        """把当前文件清单导出为文本文件（utf-8-sig，兼容 Windows 记事本）。"""
        if self._busy:
            return
        if not self._records:
            messagebox.showinfo(APP_TITLE, "列表为空，没有可保存的内容。")
            return
        filename = filedialog.asksaveasfilename(
            parent=self, title="保存文件列表", defaultextension=".txt",
            initialfile="文件列表.txt",
            filetypes=[("文本文件", ("*.txt",)), ("所有文件", ("*", "*"))],
        )
        if not filename:
            return
        suffix = self._current_suffix()
        try:
            # 只写入清单文本；绝不读取被操作文件的内容
            with open(filename, "w", encoding="utf-8-sig") as fp:
                fp.write("原文件路径 | 当前扩展名 | 新文件名预览 | 状态\n")
                for rec in self._records.values():
                    if rec.status == ST_PENDING:
                        preview = preview_name(rec.current_path, suffix)
                    else:
                        preview = rec.current_path.name
                    fp.write("{0} | {1} | {2} | {3}\n".format(
                        rec.initial_path, rec.ext_display, preview, rec.status,
                    ))
        except OSError as exc:
            logging.error("保存列表失败：%s", exc)
            messagebox.showerror(APP_TITLE, "保存列表失败：\n{0}".format(exc))
            return
        logging.info("文件列表已保存到：%s", filename)
        messagebox.showinfo(APP_TITLE, "文件列表已保存到：\n{0}".format(filename))

    # ------------------------------------------------------------------
    # 转换与撤销
    # ------------------------------------------------------------------

    def _start_convert(self) -> None:
        """转换入口：校验 → 安全确认 → 启动工作线程。"""
        if self._busy:
            return
        if not self._records:
            messagebox.showinfo(APP_TITLE, "请先点击「添加文件」选择要转换的文件。")
            return
        suffix = self._current_suffix()
        if not is_valid_suffix(suffix):
            messagebox.showwarning(
                APP_TITLE,
                "目标后缀不合法：不能为空，且不能包含 \\ / : * ? \" < > | 或空白字符。",
            )
            return
        pending = [rec for rec in self._records.values() if rec.status != ST_CONVERTED]
        if not pending:
            messagebox.showinfo(APP_TITLE, "列表中没有可转换的文件（全部已转换）。")
            return
        # 安全确认（需求固定文案），用户拒绝则不做任何变更
        if not messagebox.askyesno("安全确认", CONFIRM_MESSAGE):
            logging.info("用户取消了本次转换")
            return

        self._busy = True
        self._current_batch = []
        self._refresh_controls()
        self.progress.configure(maximum=len(pending), value=0)
        self.progress_label.configure(text="0/{0}".format(len(pending)))
        self.status_var.set("正在转换 0/{0} ...".format(len(pending)))
        logging.info("开始转换 %d 个文件 → .%s", len(pending), suffix)
        threading.Thread(
            target=self._convert_thread, args=(pending, suffix),
            name="convert-worker", daemon=True,
        ).start()

    def _convert_thread(self, records: List[ItemRecord], suffix: str) -> None:
        """工作线程入口：执行转换并对意外异常兜底。"""
        try:
            convert_worker(records, suffix, self.msg_queue)
        except Exception:  # pragma: no cover - 兜底防线
            logging.exception("转换过程发生意外错误")
            self.msg_queue.put((M_FINISHED, -1, 0, 0))

    def _undo_last(self) -> None:
        """撤销最近一次转换：对栈顶批次逐文件恢复原始扩展名。"""
        if self._busy or not self.undo_stack:
            return
        batch = self.undo_stack[-1]
        if not batch:  # 空批次（上次全部失败）直接丢弃
            self.undo_stack.pop()
            self._refresh_controls()
            return
        snapshot = list(batch)
        self._busy = True
        self._refresh_controls()
        self.progress.configure(maximum=len(snapshot), value=0)
        self.progress_label.configure(text="0/{0}".format(len(snapshot)))
        self.status_var.set("正在撤销 0/{0} ...".format(len(snapshot)))
        logging.info("开始撤销最近一次转换，共 %d 个文件", len(snapshot))
        threading.Thread(
            target=self._undo_thread, args=(snapshot,),
            name="undo-worker", daemon=True,
        ).start()

    def _undo_thread(self, batch: List[dict]) -> None:
        """工作线程入口：执行撤销并对意外异常兜底。"""
        try:
            undo_worker(batch, self.msg_queue)
        except Exception:  # pragma: no cover - 兜底防线
            logging.exception("撤销过程发生意外错误")
            self.msg_queue.put((M_UNDO_FINISHED, 0, len(batch)))

    def _remove_from_top_batch(self, iid: str) -> None:
        """撤销成功一项后，把它从栈顶批次中移除（失败项保留以便重试）。"""
        if not self.undo_stack:
            return
        batch = self.undo_stack[-1]
        for i, item in enumerate(batch):
            if item["iid"] == iid:
                del batch[i]
                break

    # ------------------------------------------------------------------
    # 队列轮询：工作线程 → 主线程 的唯一通道
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        """每 100ms 排空一次消息队列；所有 UI 更新都发生在主线程。"""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                self._handle_message(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, msg: tuple) -> None:
        """按消息类型分发处理（均在主线程执行）。"""
        kind = msg[0]

        if kind == M_LOG:
            self._append_log(msg[1])

        elif kind == M_PROGRESS:
            _, idx, total, source = msg
            self.progress.configure(maximum=total, value=idx)
            self.progress_label.configure(text="{0}/{1}".format(idx, total))
            verb = "转换" if source == "convert" else "撤销"
            self.status_var.set("正在{0} {1}/{2} ...".format(verb, idx, total))

        elif kind == M_ROW_OK:
            _, iid, old_str, new_str = msg
            rec = self._records.get(iid)
            if rec is not None:
                rec.current_path = Path(new_str)
                rec.status = ST_CONVERTED
                self._update_row(rec)
            if self._current_batch is not None:
                self._current_batch.append(
                    {"iid": iid, "old": Path(old_str), "new": Path(new_str)}
                )

        elif kind == M_ROW_SKIP:
            rec = self._records.get(msg[1])
            if rec is not None:
                rec.status = ST_SKIPPED
                self._update_row(rec)

        elif kind == M_ROW_FAIL:
            rec = self._records.get(msg[1])
            if rec is not None:
                rec.status = ST_FAILED
                self._update_row(rec)

        elif kind == M_FINISHED:
            _, ok, skip, fail = msg
            self._busy = False
            if self._current_batch:
                self.undo_stack.append(self._current_batch)  # 本批次入栈，供撤销
            self._current_batch = None
            self._refresh_controls()
            if ok < 0:  # 工作线程意外中止
                self.status_var.set("转换异常中止，详见日志")
                messagebox.showerror(APP_TITLE, "转换过程中发生意外错误，详情请查看日志。")
                return
            self.progress.configure(value=self.progress["maximum"])
            parts = ["成功 {0} 个".format(ok)]
            if skip:
                parts.append("跳过 {0} 个".format(skip))
            if fail:
                parts.append("失败 {0} 个".format(fail))
            summary = "，".join(parts)
            logging.info("转换结束：%s", summary)
            self.status_var.set("转换完成：{0}".format(summary))
            messagebox.showinfo(APP_TITLE, "转换完成：{0}。".format(summary))

        elif kind == M_UNDO_OK:
            _, iid, old_str = msg
            self._remove_from_top_batch(iid)
            rec = self._records.get(iid)
            if rec is not None:
                rec.current_path = Path(old_str)
                rec.status = ST_RESTORED
                self._update_row(rec)

        elif kind == M_UNDO_FAIL:
            # 撤销失败的行保持“已转换”状态（文件确实未恢复），原因见日志
            pass

        elif kind == M_UNDO_FINISHED:
            _, ok, fail = msg
            self._busy = False
            if self.undo_stack and not self.undo_stack[-1]:
                self.undo_stack.pop()  # 批次全部恢复完成，弹出
            self._refresh_controls()
            if fail:
                summary = "撤销完成：恢复 {0} 个，失败 {1} 个（可重试）".format(ok, fail)
                self.status_var.set(summary)
                logging.warning(summary)
                messagebox.showwarning(
                    APP_TITLE,
                    "撤销完成：恢复 {0} 个，失败 {1} 个。\n\n"
                    "失败项已保留，处理冲突后可再次点击「撤销上次转换」重试。详情见日志。".format(ok, fail),
                )
            else:
                summary = "撤销完成：恢复 {0} 个".format(ok)
                self.status_var.set(summary)
                logging.info(summary)

    # ------------------------------------------------------------------
    # 日志区 / 帮助 / 退出
    # ------------------------------------------------------------------

    def _append_log(self, line: str) -> None:
        """向只读日志区追加一行并自动滚动到底部；超长时裁剪早期日志。"""
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 5000:  # 防止长会话内存无限增长
            self.log_text.delete("1.0", "{0}.0".format(line_count - 4000))
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _show_help(self) -> None:
        """弹出使用说明窗口。"""
        win = tk.Toplevel(self)
        win.title("使用说明")
        win.transient(self)
        win.geometry("680x560")

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        font_kw = {"font": UI_FONT} if UI_FONT else {}
        text = tk.Text(frame, wrap="word", state="disabled", relief="flat", **font_kw)
        sb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=sb.set)
        text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        text.configure(state="normal")
        text.insert("1.0", HELP_TEXT.format(
            log_path=Path(tempfile.gettempdir()) / LOG_FILENAME,
        ))
        text.configure(state="disabled")

    def _on_close(self) -> None:
        """关闭窗口；任务执行中先确认（工作线程为守护线程，退出即中止）。"""
        if self._busy:
            if not messagebox.askyesno(
                APP_TITLE, "仍有任务正在处理，确定退出吗？\n未完成的部分将中断。"
            ):
                return
        logging.info("程序退出")
        self.destroy()


# ======================================================================
# ⑥ 入口
# ======================================================================

def enable_windows_dpi_awareness() -> None:
    """Windows 上启用 DPI 感知，避免高分屏下界面模糊（失败则静默忽略）。"""
    if sys.platform != "win32":  # pragma: no cover
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        pass


def main() -> None:
    """程序入口。"""
    enable_windows_dpi_awareness()
    app = FileExtensionChangerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
