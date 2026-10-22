#!/usr/bin/env python3
"""Windows 串口交互与日志保存工具。"""

from __future__ import annotations

import json
import codecs
import queue
import re
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError as exc:
    raise SystemExit("缺少 pyserial，请运行：python -m pip install pyserial") from exc


class SerialLogRecorder(tk.Tk):
    POLL_MS = 50
    ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def __init__(self) -> None:
        super().__init__()
        self.title("串口日志与指令工具")
        self.geometry("1180x820")
        self.minsize(900, 650)

        self.serial_port: serial.Serial | None = None
        self.log_file = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.io_lock = threading.Lock()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.total_bytes = 0
        self.saving_enabled = False
        self.text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.ansi_pending = ""
        self.display_ansi_pending = ""
        self.display_ansi_tag: str | None = None

        self.preset_path = Path(__file__).with_name("serial_commands.json")
        self.presets: list[dict[str, str]] = self._load_presets()
        self.selected_preset_index: int | None = None

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.directory_var = tk.StringVar(value=str(Path(__file__).parent / "串口日志"))
        self.prefix_var = tk.StringVar(value="测试")
        self.status_var = tk.StringVar(value="未打开串口")
        self.file_var = tk.StringVar(value="-")
        self.command_var = tk.StringVar()
        self.preset_hint_var = tk.StringVar(value="点击常用指令后会填入输入框，可修改后再发送")

        self._configure_style()
        self._build_ui()
        self.refresh_ports()
        self.after(self.POLL_MS, self._drain_ui_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Command.TButton", padding=(8, 5))

    def _build_ui(self) -> None:
        settings = ttk.LabelFrame(self, text="串口与保存设置", padding=10)
        settings.pack(fill="x", padx=10, pady=(10, 5))

        ttk.Label(settings, text="串口").grid(row=0, column=0, sticky="w")
        self.port_box = ttk.Combobox(settings, textvariable=self.port_var, width=23)
        self.port_box.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(settings, text="刷新", command=self.refresh_ports).grid(row=0, column=2)
        ttk.Label(settings, text="波特率").grid(row=0, column=3, sticky="w", padx=(18, 0))
        self.baud_box = ttk.Combobox(
            settings, textvariable=self.baud_var, width=12,
            values=("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"),
        )
        self.baud_box.grid(row=0, column=4, sticky="w", padx=(6, 0))

        ttk.Label(settings, text="保存目录").grid(row=1, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(settings, textvariable=self.directory_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=6, pady=(9, 0)
        )
        ttk.Button(settings, text="选择…", command=self.choose_directory).grid(
            row=1, column=4, sticky="ew", pady=(9, 0)
        )
        ttk.Label(settings, text="文件前缀").grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(settings, textvariable=self.prefix_var).grid(
            row=2, column=1, sticky="ew", padx=6, pady=(9, 0)
        )
        ttk.Label(settings, text="自动生成：前缀-COM口-年月日-时分秒.txt（UTF-8）").grid(
            row=2, column=2, columnspan=3, sticky="w", pady=(9, 0)
        )
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        actions = ttk.Frame(self, padding=(10, 4))
        actions.pack(fill="x")
        self.open_button = ttk.Button(actions, text="打开串口", command=self.open_without_saving)
        self.open_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="关闭串口", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.start_button = ttk.Button(actions, text="开始保存", command=self.start_recording, state="disabled")
        self.start_button.pack(side="left", padx=(8, 0))
        self.end_save_button = ttk.Button(actions, text="结束保存", command=self.stop_saving, state="disabled")
        self.end_save_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="清空日志显示", command=self.clear_display).pack(side="left", padx=(8, 0))
        ttk.Label(actions, textvariable=self.status_var, style="Title.TLabel").pack(side="right")

        info = ttk.Frame(self, padding=(10, 2, 10, 5))
        info.pack(fill="x")
        ttk.Label(info, text="当前日志文件：").pack(side="left")
        ttk.Label(info, textvariable=self.file_var).pack(side="left", fill="x", expand=True)

        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        output_frame = ttk.LabelFrame(panes, text="实时串口日志", padding=6)
        panes.add(output_frame, weight=4)
        self.output = tk.Text(
            output_frame, wrap="none", font=("Consolas", 11), state="disabled",
            background="#101418", foreground="#d8dee9", insertbackground="white",
            selectbackground="#264f78", relief="flat", padx=10, pady=8,
        )
        self.output.tag_configure("tx", foreground="#58a6ff")
        self.output.tag_configure("error", foreground="#ff6b6b")
        self.output.tag_configure("warning", foreground="#ffd166")
        self.output.tag_configure("success", foreground="#7ee787")
        self.output.tag_configure("marker", foreground="#c792ea")
        self.output.tag_configure("ansi_black", foreground="#7d8590")
        self.output.tag_configure("ansi_red", foreground="#ff7b72")
        self.output.tag_configure("ansi_green", foreground="#7ee787")
        self.output.tag_configure("ansi_yellow", foreground="#f2cc60")
        self.output.tag_configure("ansi_blue", foreground="#79c0ff")
        self.output.tag_configure("ansi_magenta", foreground="#d2a8ff")
        self.output.tag_configure("ansi_cyan", foreground="#56d4dd")
        self.output.tag_configure("ansi_white", foreground="#f0f6fc")
        ybar = ttk.Scrollbar(output_frame, orient="vertical", command=self.output.yview)
        xbar = ttk.Scrollbar(output_frame, orient="horizontal", command=self.output.xview)
        self.output.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.output.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)

        command_frame = ttk.LabelFrame(panes, text="指令发送与常用指令", padding=8)
        panes.add(command_frame, weight=1)
        self.command_entry = ttk.Entry(command_frame, textvariable=self.command_var, font=("Consolas", 11))
        self.command_entry.grid(row=0, column=0, sticky="ew")
        self.command_entry.bind("<Return>", lambda _event: self.send_command())
        self.send_button = ttk.Button(command_frame, text="发送", command=self.send_command, state="disabled")
        self.send_button.grid(row=0, column=1, padx=(7, 0))
        ttk.Button(command_frame, text="新增常用", command=self.add_preset).grid(row=0, column=2, padx=(7, 0))
        ttk.Button(command_frame, text="修改选中", command=self.edit_selected_preset).grid(row=0, column=3, padx=(7, 0))
        ttk.Button(command_frame, text="删除选中", command=self.delete_selected_preset).grid(row=0, column=4, padx=(7, 0))
        command_frame.columnconfigure(0, weight=1)

        ttk.Label(command_frame, text="常用指令（点击只填入输入框，不会立即发送）：").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(8, 3)
        )
        preset_outer = ttk.Frame(command_frame)
        preset_outer.grid(row=2, column=0, columnspan=5, sticky="nsew")
        self.preset_canvas = tk.Canvas(preset_outer, height=112, highlightthickness=0)
        preset_scroll = ttk.Scrollbar(preset_outer, orient="vertical", command=self.preset_canvas.yview)
        self.preset_canvas.configure(yscrollcommand=preset_scroll.set)
        self.preset_canvas.pack(side="left", fill="both", expand=True)
        preset_scroll.pack(side="right", fill="y")
        self.preset_frame = ttk.Frame(self.preset_canvas)
        self.preset_window = self.preset_canvas.create_window((0, 0), window=self.preset_frame, anchor="nw")
        self.preset_frame.bind("<Configure>", self._update_preset_scrollregion)
        self.preset_canvas.bind("<Configure>", self._resize_preset_window)
        self._render_presets()
        ttk.Label(command_frame, textvariable=self.preset_hint_var, foreground="#555555").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(3, 0)
        )

    def refresh_ports(self) -> None:
        ports = [p.device for p in list_ports.comports()]
        self.port_box["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.directory_var.get() or None)
        if selected:
            self.directory_var.set(selected)

    def start_recording(self) -> None:
        if self.serial_port is None or not self.serial_port.is_open:
            messagebox.showwarning("串口未打开", "请先点击“打开串口”。")
            return
        if self.log_file is not None:
            return

        directory = Path(self.directory_var.get().strip()).expanduser()
        prefix = self._safe_prefix(self.prefix_var.get())
        timestamp = datetime.now().strftime("%Y_%m_%d_%H-%M-%S")
        port = self.port_var.get().strip()
        log_path = directory / f"{prefix}-{port}-{timestamp}.txt"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            log_file = log_path.open("xb")
            # 写入 UTF-8 BOM，确保 Windows 记事本能自动识别中文编码。
            log_file.write(codecs.BOM_UTF8)
            log_file.flush()
        except Exception as exc:
            messagebox.showerror("无法开始保存", str(exc))
            return

        with self.io_lock:
            self.log_file = log_file
            self.saving_enabled = True
        self.file_var.set(str(log_path))
        self.start_button.configure(state="disabled")
        self.end_save_button.configure(state="normal")
        self.status_var.set(f"串口已打开，正在保存：{self.total_bytes} 字节")
        self._append_display(f"\n===== 开始保存 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n", "marker")

    def stop_saving(self) -> None:
        if self.log_file is None:
            return
        with self.io_lock:
            log_file = self.log_file
            self.log_file = None
            self.saving_enabled = False
            try:
                log_file.flush()
                log_file.close()
            except Exception:
                pass
        self.start_button.configure(state="normal")
        self.end_save_button.configure(state="disabled")
        self.status_var.set(f"串口保持打开，已结束保存；共接收 {self.total_bytes} 字节")
        self._append_display(f"\n===== 结束保存 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n", "marker")

    def open_without_saving(self) -> None:
        self._open_serial()

    def _open_serial(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("无法开始", "请选择或输入串口，例如 COM21。")
            return
        try:
            baud = int(self.baud_var.get().strip())
            if baud <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("无法开始", "波特率必须是正整数。")
            return

        try:
            serial_port = serial.Serial(port=port, baudrate=baud, timeout=0.2)
        except Exception as exc:
            messagebox.showerror("无法打开串口", f"{exc}\n\n请关闭 MobaXterm、SSCOM 或 idf.py monitor 后重试。")
            return

        self.log_file = None
        self.serial_port = serial_port
        self.saving_enabled = False
        self.total_bytes = 0
        self.text_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.ansi_pending = ""
        self.display_ansi_pending = ""
        self.display_ansi_tag = None
        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.file_var.set("尚未开始保存")
        self.status_var.set(f"已连接 {port} @ {baud}，未保存")
        self.open_button.configure(state="disabled")
        self.start_button.configure(state="normal")
        self.end_save_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.send_button.configure(state="normal")
        self.port_box.configure(state="disabled")
        self.baud_box.configure(state="disabled")
        self._append_display(f"\n===== 串口已打开（未保存） {datetime.now():%Y-%m-%d %H:%M:%S} =====\n", "marker")
        self.command_entry.focus_set()

    def _reader_loop(self) -> None:
        assert self.serial_port is not None
        try:
            while not self.stop_event.is_set():
                waiting = self.serial_port.in_waiting
                data = self.serial_port.read(waiting if waiting else 1)
                if not data:
                    continue
                self.total_bytes += len(data)
                # 使用增量解码器，中文字节即使被串口分成多批也不会乱码。
                decoded = self.text_decoder.decode(data, final=False)
                if decoded:
                    clean_text = self._strip_ansi(decoded)
                    if clean_text:
                        with self.io_lock:
                            if self.log_file is not None:
                                self.log_file.write(clean_text.encode("utf-8"))
                                self.log_file.flush()
                    # 窗口保留原始 ANSI 序列并渲染颜色；TXT 只保存上面的纯文本。
                    self.ui_queue.put(("data", decoded))
                self.ui_queue.put(("count", self.total_bytes))
        except Exception as exc:
            self.ui_queue.put(("error", str(exc)))
        finally:
            self.ui_queue.put(("stopped", None))

    def stop_recording(self) -> None:
        if self.reader_thread:
            if self.log_file is not None:
                self.stop_saving()
            self.stop_event.set()
            self.stop_button.configure(state="disabled")
            self.start_button.configure(state="disabled")
            self.end_save_button.configure(state="disabled")
            self.send_button.configure(state="disabled")
            self.status_var.set("正在关闭串口…")

    def _finish_stop(self) -> None:
        for resource in (self.serial_port, self.log_file):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self.serial_port = None
        self.log_file = None
        was_saving = self.saving_enabled
        self.saving_enabled = False
        self.reader_thread = None
        self.open_button.configure(state="normal")
        self.start_button.configure(state="disabled")
        self.end_save_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.port_box.configure(state="normal")
        self.baud_box.configure(state="normal")
        if was_saving:
            self.status_var.set(f"已关闭串口，共保存 {self.total_bytes} 字节")
        else:
            self.status_var.set(f"已关闭串口，共接收 {self.total_bytes} 字节（未保存）")
        self._append_display(f"\n===== 串口已关闭 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n", "marker")

    def send_command(self) -> None:
        if self.serial_port is None or not self.serial_port.is_open:
            messagebox.showwarning("串口未打开", "请先点击“打开串口”。")
            return
        command = self.command_var.get().strip()
        if not command:
            return
        payload = (command + "\r\n").encode("utf-8")
        try:
            with self.io_lock:
                self.serial_port.write(payload)
                self.serial_port.flush()
                if self.log_file is not None:
                    marker = f"\r\n[TX {datetime.now():%H:%M:%S.%f}] {command}\r\n".encode("utf-8")
                    self.log_file.write(marker)
                    self.log_file.flush()
            self._append_display(f"\n[发送] {command}\n", "tx")
            self.command_entry.focus_set()
            self.command_entry.selection_range(0, "end")
        except Exception as exc:
            messagebox.showerror("发送失败", str(exc))

    def select_preset(self, index: int) -> None:
        self.selected_preset_index = index
        preset = self.presets[index]
        self.command_var.set(preset["command"])
        self.preset_hint_var.set(
            f"已选：{preset['name']}　|　{preset['command']}　|　{preset.get('description', '')}"
        )
        self.command_entry.focus_set()
        self.command_entry.icursor("end")

    def add_preset(self) -> None:
        command = self.command_var.get().strip()
        if not command:
            messagebox.showwarning("没有指令", "请先在输入框填写要保存的指令。")
            return
        name = simpledialog.askstring("新增常用指令", "按钮名称：", initialvalue=command, parent=self)
        if not name or not name.strip():
            return
        description = simpledialog.askstring("新增常用指令", "简短说明（可留空）：", parent=self) or ""
        self.presets.append({"name": name.strip(), "command": command, "description": description.strip()})
        self._save_presets()
        self._render_presets()
        self.select_preset(len(self.presets) - 1)

    def edit_selected_preset(self) -> None:
        index = self.selected_preset_index
        if index is None or index >= len(self.presets):
            messagebox.showwarning("未选中指令", "请先点击一个常用指令按钮。")
            return
        preset = self.presets[index]
        command = self.command_var.get().strip()
        if not command:
            messagebox.showwarning("没有指令", "输入框不能为空。")
            return
        name = simpledialog.askstring("修改常用指令", "按钮名称：", initialvalue=preset["name"], parent=self)
        if not name or not name.strip():
            return
        description = simpledialog.askstring(
            "修改常用指令", "简短说明（可留空）：",
            initialvalue=preset.get("description", ""), parent=self,
        )
        if description is None:
            return
        self.presets[index] = {"name": name.strip(), "command": command, "description": description.strip()}
        self._save_presets()
        self._render_presets()
        self.select_preset(index)

    def delete_selected_preset(self) -> None:
        index = self.selected_preset_index
        if index is None or index >= len(self.presets):
            messagebox.showwarning("未选中指令", "请先点击一个常用指令按钮。")
            return
        preset = self.presets[index]
        if not messagebox.askyesno("删除常用指令", f"确定删除“{preset['name']}”吗？"):
            return
        del self.presets[index]
        self.selected_preset_index = None
        self._save_presets()
        self._render_presets()
        self.preset_hint_var.set("点击常用指令后会填入输入框，可修改后再发送")

    def _render_presets(self) -> None:
        for widget in self.preset_frame.winfo_children():
            widget.destroy()
        for index, preset in enumerate(self.presets):
            button = ttk.Button(
                self.preset_frame, text=preset["name"], style="Command.TButton",
                command=lambda idx=index: self.select_preset(idx),
            )
            button.grid(row=index // 8, column=index % 8, padx=(0, 6), pady=(0, 5), sticky="ew")
            button.bind(
                "<Enter>",
                lambda _event, item=preset: self.preset_hint_var.set(
                    f"{item['command']}　|　{item.get('description', '')}"
                ),
            )
            button.bind(
                "<Leave>",
                lambda _event: self.preset_hint_var.set("点击常用指令后会填入输入框，可修改后再发送"),
            )
        for column in range(8):
            self.preset_frame.columnconfigure(column, weight=1)

    def _update_preset_scrollregion(self, _event=None) -> None:
        self.preset_canvas.configure(scrollregion=self.preset_canvas.bbox("all"))

    def _resize_preset_window(self, event) -> None:
        self.preset_canvas.itemconfigure(self.preset_window, width=event.width)

    def _load_presets(self) -> list[dict[str, str]]:
        if not self.preset_path.exists():
            return []
        try:
            data = json.loads(self.preset_path.read_text(encoding="utf-8"))
            return [
                {"name": str(x["name"]), "command": str(x["command"]), "description": str(x.get("description", ""))}
                for x in data if isinstance(x, dict) and "name" in x and "command" in x
            ]
        except Exception as exc:
            messagebox.showerror("常用指令读取失败", f"{self.preset_path}\n{exc}")
            return []

    def _save_presets(self) -> None:
        self.preset_path.write_text(json.dumps(self.presets, ensure_ascii=False, indent=2), encoding="utf-8")

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "data":
                    self._append_ansi_text(str(payload))
                elif kind == "count":
                    action = "正在保存" if self.saving_enabled else "已接收（未保存）"
                    self.status_var.set(f"{action}：{int(payload)} 字节")
                elif kind == "error":
                    messagebox.showerror("串口读取失败", str(payload))
                    self.stop_event.set()
                elif kind == "stopped":
                    self._finish_stop()
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self._drain_ui_queue)

    def _append_rx_text(self, text: str) -> None:
        lower = text.lower()
        tag = None
        if any(word in lower for word in ("error", "failed", "abort", "panic", "assert")):
            tag = "error"
        elif any(word in lower for word in ("warning", " w (", "timeout", "retry")):
            tag = "warning"
        elif any(word in lower for word in ("success", "passed", "ota ok", "done")):
            tag = "success"
        self._append_display(text, tag)

    def _append_ansi_text(self, text: str) -> None:
        """解析串口 ANSI SGR 颜色；不完整序列留待下一批数据。"""
        combined = self.display_ansi_pending + text
        self.display_ansi_pending = ""
        position = 0
        for match in self.ANSI_ESCAPE_RE.finditer(combined):
            if match.start() > position:
                segment = combined[position:match.start()]
                if self.display_ansi_tag:
                    self._append_display(segment, self.display_ansi_tag)
                else:
                    self._append_rx_text(segment)
            sequence = match.group(0)
            if sequence.endswith("m"):
                self._apply_ansi_sgr(sequence[2:-1])
            position = match.end()

        remainder = combined[position:]
        escape_at = remainder.rfind("\x1b")
        if escape_at >= 0 and remainder[escape_at:].startswith("\x1b["):
            self.display_ansi_pending = remainder[escape_at:]
            remainder = remainder[:escape_at]
        if remainder:
            if self.display_ansi_tag:
                self._append_display(remainder, self.display_ansi_tag)
            else:
                self._append_rx_text(remainder)

    def _apply_ansi_sgr(self, parameters: str) -> None:
        color_tags = {
            30: "ansi_black", 31: "ansi_red", 32: "ansi_green", 33: "ansi_yellow",
            34: "ansi_blue", 35: "ansi_magenta", 36: "ansi_cyan", 37: "ansi_white",
            90: "ansi_black", 91: "ansi_red", 92: "ansi_green", 93: "ansi_yellow",
            94: "ansi_blue", 95: "ansi_magenta", 96: "ansi_cyan", 97: "ansi_white",
        }
        try:
            codes = [int(item) if item else 0 for item in parameters.split(";")]
        except ValueError:
            return
        for code in codes:
            if code in (0, 39):
                self.display_ansi_tag = None
            elif code in color_tags:
                self.display_ansi_tag = color_tags[code]

    def _strip_ansi(self, text: str) -> str:
        """移除终端颜色码，并保留跨串口分包的不完整 ANSI 序列。"""
        combined = self.ansi_pending + text
        self.ansi_pending = ""
        last_escape = combined.rfind("\x1b")
        if last_escape >= 0:
            suffix = combined[last_escape:]
            if suffix.startswith("\x1b[") and self.ANSI_ESCAPE_RE.match(suffix) is None:
                # 颜色控制序列可能刚好被串口拆开，留到下一批再处理。
                self.ansi_pending = suffix
                combined = combined[:last_escape]
        return self.ANSI_ESCAPE_RE.sub("", combined)

    def _append_display(self, text: str, tag: str | None = None) -> None:
        # 只有原本就在底部时才自动跟随；用户上翻后保持当前位置。
        follow_tail = self.output.yview()[1] >= 0.999
        self.output.configure(state="normal")
        self.output.insert("end", text, tag or ())
        if int(self.output.index("end-1c").split(".")[0]) > 15000:
            self.output.delete("1.0", "3000.0")
        if follow_tail:
            self.output.see("end")
        self.output.configure(state="disabled")

    def clear_display(self) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.configure(state="disabled")

    @staticmethod
    def _safe_prefix(value: str) -> str:
        cleaned = "".join(ch for ch in value.strip() if ch not in '<>:"/\\|?*')
        return cleaned or "serial"

    def _on_close(self) -> None:
        if self.reader_thread:
            if not messagebox.askyesno("正在保存", "日志仍在保存，确定结束并退出吗？"):
                return
            self.stop_event.set()
            self.reader_thread.join(timeout=1.0)
            self._finish_stop()
        self.destroy()


if __name__ == "__main__":
    SerialLogRecorder().mainloop()
