import json
import ipaddress
import base64
import ctypes
import os
import queue
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image


APP_TITLE = "网络配置切换-可选择版"
ASSET_DIR = Path(__file__).resolve().parent / "assets"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "NetworkSwitcher" / "configs"
SYSTEM_DIR = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
NETSH = str(SYSTEM_DIR / "netsh.exe")
POWERSHELL = str(SYSTEM_DIR / "WindowsPowerShell" / "v1.0" / "powershell.exe")


@dataclass
class Adapter:
    name: str
    state: str = ""
    description: str = ""
    ipv4: str = ""
    gateway: str = ""
    index: int = 0

    @property
    def display_name(self):
        parts = [self.name]
        if self.description:
            parts.append(self.description)
        if self.ipv4:
            parts.append(f"IPv4 {self.ipv4}")
        if self.state:
            parts.append(self.state)
        return "  |  ".join(parts)


def run_cmd(cmd: Sequence[str], check=False, timeout=15):
    """Run a Windows command without invoking a shell."""
    if isinstance(cmd, (str, bytes)):
        raise TypeError("命令必须使用参数列表。")
    try:
        result = subprocess.run(
            list(cmd), shell=False, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"命令无法完成：{exc}") from exc
    for encoding in ("utf-8", "oem", "gbk"):
        try:
            stdout = result.stdout.decode(encoding)
            stderr = result.stderr.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        stdout = result.stdout.decode("utf-8", errors="ignore")
        stderr = result.stderr.decode("utf-8", errors="ignore")
    if check and result.returncode != 0:
        raise RuntimeError(stderr.strip() or stdout.strip() or "命令执行失败")
    return result.returncode, stdout, stderr


def is_valid_ip(value, allow_empty=False):
    value = (value or "").strip()
    if allow_empty and not value:
        return True
    try:
        ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return True


def is_valid_mask(value):
    if not is_valid_ip(value):
        return False
    try:
        # Invert and check contiguous low bits; IPv4Network also accepts hostmasks.
        inverted = (~int(ipaddress.IPv4Address(value))) & 0xffffffff
        if inverted & (inverted + 1):
            return False
    except (ipaddress.NetmaskValueError, ipaddress.AddressValueError):
        return False
    return True


def format_command(cmd):
    """Format an argv list for the read-only command preview."""
    return subprocess.list2cmdline(list(cmd))


def powershell_json(script):
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    _, output, _ = run_cmd([POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], check=True, timeout=30)
    try:
        return json.loads(output.lstrip("\ufeff")) if output.strip() else []
    except json.JSONDecodeError as exc:
        raise RuntimeError("系统返回的网络配置不是有效 JSON。") from exc


class Transitions:
    """One elapsed-time animation loop; replacing a key cancels its old target."""

    def __init__(self, owner, clock=time.perf_counter):
        self.owner = owner
        self.clock = clock
        self.jobs = {}
        self.timer = None

    def animate(self, key, duration, update, done=None):
        self.jobs[key] = (self.clock(), duration, update, done)
        if self.timer is None:
            self.timer = self.owner.after(16, self.tick)

    def tick(self):
        self.timer = None
        now = self.clock()
        for key, job in list(self.jobs.items()):
            if self.jobs.get(key) is not job:
                continue
            start, duration, update, done = job
            fraction = min(1.0, max(0.0, (now - start) / duration))
            eased = 1 - (1 - fraction) ** 3
            update(eased)
            if fraction >= 1 and self.jobs.get(key) is job:
                del self.jobs[key]
                if done:
                    done()
        if self.jobs and self.timer is None:
            self.timer = self.owner.after(16, self.tick)

    def color(self, widget, option, target, duration=0.18):
        start = widget.cget(option)
        if not isinstance(start, str) or not start.startswith("#"):
            widget.configure(**{option: target})
            return
        source_rgb = tuple(int(start[i:i + 2], 16) for i in (1, 3, 5))
        target_rgb = tuple(int(target[i:i + 2], 16) for i in (1, 3, 5))

        def update(fraction):
            color = "#" + "".join(f"{round(a + (b - a) * fraction):02x}" for a, b in zip(source_rgb, target_rgb))
            if widget.cget(option) != color:
                widget.configure(**{option: color})

        self.animate((widget, option), duration, update)

    def close(self):
        if self.timer is not None:
            self.owner.after_cancel(self.timer)
            self.timer = None
        self.jobs.clear()


class NetworkSwitcher(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.adapters = []
        self.adapter_display_map = {}
        self.form_adapter_name = ""
        self.busy = False
        self.task_results = queue.Queue()
        self.transitions = Transitions(self)
        self.progress_delay = None

        self.title(APP_TITLE)
        self.iconbitmap(default=str(ASSET_DIR / "app-icon.ico"))
        self.geometry("1160x800")
        self.minsize(980, 660)
        self.configure(fg_color="#eef2f7")
        self.protocol("WM_DELETE_WINDOW", self.close_app)

        self.build_ui()
        self.refresh_adapters()

    def close_app(self):
        if self.busy:
            self.status_label.configure(text="请等待当前操作结束后再关闭程序。")
            return
        self.destroy()

    def destroy(self):
        self.transitions.close()
        if self.progress_delay is not None:
            self.after_cancel(self.progress_delay)
            self.progress_delay = None
        super().destroy()

    def build_ui(self):
        self.configure(fg_color="#f3f5f9")
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        font = "Microsoft YaHei UI"
        self.details_visible = False
        self.details_height = 0
        self.field_hints = {}
        self.field_defaults = {}

        sidebar = ctk.CTkFrame(self, width=268, fg_color="#111c32", corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(8, weight=1)
        with Image.open(ASSET_DIR / "app-icon.png") as image:
            self.brand_icon = ctk.CTkImage(light_image=image.copy(), dark_image=image.copy(), size=(36, 36))
        ctk.CTkLabel(sidebar, text="  网络配置", image=self.brand_icon, compound="left",
                        text_color="#ffffff", font=(font, 23, "bold")).grid(row=0, column=0, padx=20, pady=(26, 2), sticky="w")
        ctk.CTkLabel(sidebar, text="让每一次连接，都井然有序", text_color="#92a4c2", font=(font, 12)).grid(row=1, column=0, padx=24, pady=(0, 30), sticky="w")
        ctk.CTkLabel(sidebar, text="01   选择目标网卡", text_color="#b8c7df", font=(font, 12, "bold")).grid(row=2, column=0, padx=24, pady=(0, 10), sticky="w")
        self.adapter_var = ctk.StringVar(value="")
        self.adapter_menu = ctk.CTkOptionMenu(
            sidebar, variable=self.adapter_var, values=["正在读取..."], width=220, height=42,
            fg_color="#233553", button_color="#30496d", button_hover_color="#3c5b86",
            dropdown_fg_color="#ffffff", dropdown_text_color="#1b2a43", dropdown_hover_color="#eaf0fa",
            font=(font, 13), dropdown_font=(font, 12), corner_radius=9,
            command=lambda _: self.load_current_config(),
        )
        self.adapter_menu.grid(row=3, column=0, padx=20, pady=(0, 12), sticky="ew")
        self.adapter_description = ctk.CTkLabel(sidebar, text="正在获取网卡信息", text_color="#92a4c2", font=(font, 11), wraplength=220, justify="left", anchor="w")
        self.adapter_description.grid(row=4, column=0, padx=24, pady=(0, 16), sticky="ew")
        tools = ctk.CTkFrame(sidebar, fg_color="transparent")
        tools.grid(row=5, column=0, padx=20, sticky="ew")
        tools.grid_columnconfigure((0, 1), weight=1)
        for column, (text, command) in enumerate((("刷新列表", self.refresh_adapters), ("重新读取", self.load_current_config))):
            ctk.CTkButton(tools, text=text, command=command, width=100, height=36, corner_radius=8,
                             fg_color="#233553", hover_color="#30496d", font=(font, 12)).grid(row=0, column=column, padx=(0, 6) if column == 0 else (6, 0), sticky="ew")
        ctk.CTkFrame(sidebar, fg_color="#2a3850", height=1).grid(row=6, column=0, padx=24, pady=26, sticky="ew")
        files = ctk.CTkFrame(sidebar, fg_color="transparent")
        files.grid(row=7, column=0, padx=20, sticky="ew")
        files.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(files, text="配置文件", text_color="#b8c7df", font=(font, 12, "bold")).grid(row=0, column=0, padx=4, pady=(0, 10), sticky="w")
        for row, (text, command) in enumerate((("导入配置", self.import_config), ("导出当前配置", self.export_config)), 1):
            ctk.CTkButton(files, text=text, command=command, height=38, anchor="w", corner_radius=8,
                             fg_color="transparent", border_width=1, border_color="#34445e", hover_color="#233553",
                             text_color="#d8e3f3", font=(font, 13)).grid(row=row, column=0, pady=(0, 10), sticky="ew")
        note = ctk.CTkFrame(sidebar, fg_color="#1c2b43", corner_radius=12)
        note.grid(row=9, column=0, padx=20, pady=20, sticky="ew")
        ctk.CTkLabel(note, text="每次应用前自动备份", text_color="#dce8fa", font=(font, 12, "bold")).pack(padx=15, pady=(14, 4), anchor="w")
        ctk.CTkLabel(note, text="可从备份 JSON 导入基本配置。\n应用配置需要管理员权限。", text_color="#93a6c3", font=(font, 11), justify="left").pack(padx=15, pady=(0, 14), anchor="w")

        main = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(main, fg_color="transparent")
        header.grid(row=0, column=0, padx=28, pady=(24, 16), sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="连接设置", text_color="#17263e", font=(font, 26, "bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="查看当前连接，按需切换网络配置", text_color="#728096", font=(font, 12)).grid(row=1, column=0, pady=(1, 0), sticky="w")
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        ctk.CTkLabel(header, text="  管理员模式  " if admin else "  普通模式 · 应用需提权  ",
                        fg_color="#e1f3ec" if admin else "#fff0dc", text_color="#187459" if admin else "#996120",
                        corner_radius=8, height=30, font=(font, 11)).grid(row=0, column=1, rowspan=2, sticky="e")

        content = ctk.CTkScrollableFrame(main, fg_color="transparent", corner_radius=0,
                                         scrollbar_button_color="#ced6e3", scrollbar_button_hover_color="#a9b8ce")
        content.grid(row=1, column=0, padx=(22, 16), sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        summary = ctk.CTkFrame(content, fg_color="#ffffff", border_width=1, border_color="#e2e7ef", corner_radius=14)
        summary.grid(row=0, column=0, padx=6, pady=(0, 16), sticky="ew")
        summary.grid_columnconfigure((0, 1, 2), weight=1, uniform="summary")
        self.summary_values = {}
        for col, (key, title) in enumerate((("state", "连接状态"), ("ip", "当前 IPv4"), ("gateway", "默认网关"))):
            ctk.CTkLabel(summary, text=title, text_color="#7b889c", font=(font, 12)).grid(row=0, column=col, padx=18, pady=(12, 1), sticky="w")
            value = ctk.CTkLabel(summary, text="—", text_color="#23344e", font=(font, 15, "bold"))
            value.grid(row=1, column=col, padx=18, pady=(0, 12), sticky="w")
            self.summary_values[key] = value

        form = ctk.CTkFrame(content, fg_color="#ffffff", border_width=1, border_color="#e2e7ef", corner_radius=14)
        form.grid(row=1, column=0, padx=6, pady=(0, 16), sticky="ew")
        form.grid_columnconfigure((0, 1), weight=1, uniform="fields")
        ctk.CTkLabel(form, text="02   配置网络", text_color="#22324b", font=(font, 15, "bold")).grid(row=0, column=0, columnspan=2, padx=22, pady=(16, 10), sticky="w")
        self.mode_var = ctk.StringVar(value="static")
        for col, (text, value) in enumerate((("手动设置 · 静态 IP", "static"), ("自动获取 · DHCP", "dhcp"))):
            ctk.CTkRadioButton(form, text=text, variable=self.mode_var, value=value, command=self.update_mode,
                                 font=(font, 13), fg_color="#326be6", hover_color="#2556c2", border_color="#c6d0df",
                                 text_color="#32435e", radiobutton_width=19, radiobutton_height=19).grid(row=1, column=col, padx=22, pady=(0, 10), sticky="w")
        self.mode_hint = ctk.CTkLabel(form, text="", text_color="#67788f", fg_color="#f2f6fc", corner_radius=8,
                                        font=(font, 11), height=34, anchor="w")
        self.mode_hint.grid(row=2, column=0, columnspan=2, padx=22, pady=(0, 10), sticky="ew")
        self.ip_var = ctk.StringVar()
        self.mask_var = ctk.StringVar()
        self.gateway_var = ctk.StringVar()
        self.dns1_var = ctk.StringVar()
        self.dns2_var = ctk.StringVar()
        self.entries = {}
        self.add_entry(form, 3, 0, "IP 地址", self.ip_var, "192.168.1.100", "必填 · 设备在局域网中的地址", "ip")
        self.add_entry(form, 3, 1, "子网掩码", self.mask_var, "255.255.255.0", "必填 · 例如 255.255.255.0", "mask")
        self.add_entry(form, 4, 0, "默认网关", self.gateway_var, "192.168.1.1", "选填 · 通常是路由器地址", "gateway", span=2)
        self.add_entry(form, 5, 0, "首选 DNS", self.dns1_var, "114.114.114.114", "选填 · 留空将清空静态 DNS", "dns1")
        self.add_entry(form, 5, 1, "备用 DNS", self.dns2_var, "8.8.8.8", "选填 · 需先填写首选 DNS", "dns2")
        ctk.CTkLabel(form, text="仅配置 IPv4。应用前请核对目标网卡及地址。", text_color="#8591a2", font=(font, 11)).grid(row=6, column=0, columnspan=2, padx=22, pady=(0, 10), sticky="w")

        details = ctk.CTkFrame(content, fg_color="#ffffff", border_width=1, border_color="#e2e7ef", corner_radius=12)
        details.grid(row=2, column=0, padx=6, pady=(0, 12), sticky="ew")
        details.grid_columnconfigure(0, weight=1)
        self.details_toggle = ctk.CTkButton(details, text="配置详情与执行记录   ＋", command=self.toggle_details,
                                             height=42, anchor="w", font=(font, 12), fg_color="transparent",
                                             hover_color="#f1f5fb", text_color="#63758f")
        self.details_toggle.grid(row=0, column=0, padx=8, pady=4, sticky="ew")
        self.details_clip = ctk.CTkFrame(details, height=0, fg_color="transparent", corner_radius=0)
        self.details_clip.grid(row=1, column=0, sticky="ew")
        self.details_clip.grid_propagate(False)
        self.details_clip.grid_columnconfigure(0, weight=1)
        self.preview = ctk.CTkTextbox(self.details_clip, height=180, fg_color="#f5f7fb", text_color="#4d607c",
                                       corner_radius=8, font=("Consolas", 12), wrap="word")
        self.preview.grid(row=0, column=0, padx=14, pady=(0, 14), sticky="ew")
        self.details_clip.grid_remove()
        self.write_preview("选择网卡后，可在这里查看当前配置及执行记录。")

        footer = ctk.CTkFrame(main, fg_color="#ffffff", corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(footer, text="就绪", anchor="w", text_color="#6b7b91", font=(font, 11), wraplength=360)
        self.status_label.grid(row=0, column=0, rowspan=2, padx=(28, 12), pady=16, sticky="w")
        self.clear_button = ctk.CTkButton(footer, text="清空填写", command=self.clear_form, width=96, height=42,
                                            fg_color="#eef2f8", hover_color="#e2e8f1", text_color="#5c6e88", corner_radius=9, font=(font, 12))
        self.clear_button.grid(row=0, column=1, rowspan=2, padx=(0, 12), pady=18)
        self.apply_button = ctk.CTkButton(footer, text="应用配置  →", command=self.apply_config, width=148, height=42,
                                            fg_color="#326be6", hover_color="#2456c2", corner_radius=9, font=(font, 13, "bold"))
        self.apply_button.grid(row=0, column=2, rowspan=2, padx=(0, 28), pady=18)
        self.progress = ctk.CTkProgressBar(main, height=3, corner_radius=0, mode="indeterminate", progress_color="#326be6", fg_color="#edf1f7")
        self.progress.grid(row=3, column=0, sticky="ew")
        self.progress.set(0)
        # Reserve the track so loading never shifts the layout.
        self.progress.configure(progress_color="#edf1f7")
        self.add_button_motion(self.apply_button, "#326be6", "#2456c2")
        self.add_button_motion(self.clear_button, "#eef2f8", "#e2e8f1")
        self.update_mode()

    def add_button_motion(self, button, normal, hover):
        button.configure(hover=False)

        def enter(event):
            if button.cget("state") != "disabled":
                self.transitions.color(button, "fg_color", hover, 0.12)

        button.bind("<Enter>", enter, add=True)
        button.bind("<Leave>", lambda event: self.transitions.color(button, "fg_color", normal, 0.18), add=True)

    def add_entry(self, parent, row, column, label, variable, placeholder, hint, key, span=1):
        field = ctk.CTkFrame(parent, fg_color="transparent")
        field.grid(row=row, column=column, columnspan=span, padx=(22, 11) if column == 0 and span == 1 else (11, 22) if column == 1 else 22, pady=(0, 6), sticky="ew")
        field.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(field, text=label, text_color="#455673", font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, pady=(0, 5), sticky="w")
        entry = ctk.CTkEntry(field, textvariable=variable, placeholder_text=placeholder, height=40, corner_radius=8,
                             fg_color="#f9fbfe", border_color="#dce3ef", border_width=1, text_color="#22324b",
                             font=("Consolas", 14))
        entry.grid(row=1, column=0, sticky="ew")
        helper = ctk.CTkLabel(field, text=hint, text_color="#8a96a8", font=("Microsoft YaHei UI", 11), anchor="w")
        helper.grid(row=2, column=0, pady=(1, 0), sticky="w")
        self.entries[key] = entry
        self.field_hints[key] = helper
        self.field_defaults[key] = hint
        entry.bind("<FocusIn>", lambda event: self.transitions.color(entry, "border_color", "#326be6", 0.14))
        entry.bind("<FocusOut>", lambda event: self.check_field(key, variable))

    def check_field(self, key, variable):
        value = variable.get().strip()
        valid = not value or (is_valid_mask(value) if key == "mask" else is_valid_ip(value))
        self.transitions.color(self.entries[key], "border_color", "#dce3ef" if valid else "#d85c65")
        self.field_hints[key].configure(text=self.field_defaults[key] if valid else "请输入连续子网掩码" if key == "mask" else "请输入有效的 IPv4 地址", text_color="#8a96a8" if valid else "#c34c58")

    def toggle_details(self):
        self.details_visible = not self.details_visible
        start = self.details_height
        target = 194 if self.details_visible else 0
        if self.details_visible:
            self.details_clip.grid()

        def update(fraction):
            height = round(start + (target - start) * fraction)
            if height != self.details_height:
                self.details_height = height
                self.details_clip.configure(height=max(1, height))

        def finished():
            if not self.details_visible:
                self.details_clip.grid_remove()

        self.transitions.animate("details", 0.24, update, finished)
        self.details_toggle.configure(text="配置详情与执行记录   －" if self.details_visible else "配置详情与执行记录   ＋")

    def update_adapter_card(self, adapter):
        self.adapter_description.configure(text=adapter.description or "未提供网卡描述")
        state = {"Up": "已连接", "Disconnected": "未连接", "Disabled": "已禁用", "Not Present": "设备不可用"}.get(adapter.state, adapter.state or "未知")
        self.summary_values["state"].configure(text="●  " + state, text_color="#209573" if adapter.state == "Up" else "#8a96a8")
        self.summary_values["ip"].configure(text=adapter.ipv4 or "尚无地址")
        self.summary_values["gateway"].configure(text=adapter.gateway or "未设置")

    def write_preview(self, text):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def start_task(self, work, on_success, status):
        if self.busy:
            return
        self.busy = True
        self.status_label.configure(text=status, text_color="#326be6")
        # Avoid a flash for short tasks and keep chained reads visually continuous.
        self.transitions.jobs.pop("loading-finish", None)

        def show_progress():
            self.progress_delay = None
            self.transitions.color(self.progress, "progress_color", "#326be6", 0.16)
            self.progress.start()

        self.progress_delay = self.after(140, show_progress)
        self.locked_controls = []

        def lock(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, (ctk.CTkButton, ctk.CTkOptionMenu, ctk.CTkRadioButton, ctk.CTkEntry)):
                    self.locked_controls.append((widget, widget.cget("state")))
                    widget.configure(state="disabled")
                else:
                    lock(widget)

        lock(self)

        def worker():
            try:
                self.task_results.put((True, work()))
            except Exception as exc:
                self.task_results.put((False, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(100, lambda: self.poll_task(on_success))

    def poll_task(self, on_success):
        try:
            success, result = self.task_results.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self.poll_task(on_success))
            return
        for widget, state in self.locked_controls:
            widget.configure(state=state)
        self.busy = False
        if self.progress_delay is not None:
            self.after_cancel(self.progress_delay)
            self.progress_delay = None

        def hide_progress():
            self.progress.stop()
            self.progress.set(0)

        self.transitions.color(self.progress, "progress_color", "#edf1f7", 0.20)
        self.transitions.animate("loading-finish", 0.20, lambda fraction: None, hide_progress)
        self.status_label.configure(text_color="#6b7b91")
        if success:
            on_success(result)
        else:
            self.status_label.configure(text="操作失败，请查看错误信息。", text_color="#c34c58")
            messagebox.showerror("操作失败", result)

    def refresh_adapters(self):
        previous = self.selected_adapter()

        def loaded(adapters):
            self.adapters = adapters
            self.adapter_display_map = {a.name: a.name for a in adapters}
            display_names = list(self.adapter_display_map) or ["未检测到网卡"]
            self.adapter_menu.configure(values=display_names)
            selected = next((a.name for a in adapters if a.name == previous), display_names[0])
            self.adapter_var.set(selected)
            self.status_label.configure(text=f"已读取 {len(adapters)} 个网卡。")
            if adapters:
                self.load_current_config()
            else:
                self.clear_form()
                self.update_adapter_card(Adapter(name=""))
                self.write_preview("未检测到网卡，请检查设备后刷新。")

        self.start_task(self.get_adapters_from_powershell, loaded, "正在读取网卡列表...")

    def get_adapters_from_powershell(self):
        ps = (
            "$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue); "
            "$routes = @(Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue); "
            "Get-NetAdapter | ForEach-Object { "
            "$idx = $_.ifIndex; "
            "[pscustomobject]@{ "
            "Name=$_.Name; "
            "Index=$idx; "
            "Description=$_.InterfaceDescription; "
            "Status=$_.Status; "
            "IPv4=($ips | Where-Object InterfaceIndex -eq $idx | Select-Object -First 1 -ExpandProperty IPAddress); "
            "Gateway=($routes | Where-Object { $_.InterfaceIndex -eq $idx -and $_.DestinationPrefix -eq '0.0.0.0/0' } | Sort-Object RouteMetric | Select-Object -First 1 -ExpandProperty NextHop) "
            "} "
            "} | Sort-Object @{Expression={ if ($_.Status -eq 'Up') { 0 } else { 1 } }},Name | ConvertTo-Json -Compress"
        )
        data = powershell_json(ps)
        if isinstance(data, dict):
            data = [data]
        adapters = []
        for item in data:
            name = str(item.get("Name") or "").strip()
            if not name:
                continue
            adapters.append(
                Adapter(
                    name=name,
                    state=str(item.get("Status") or "").strip(),
                    description=str(item.get("Description") or "").strip(),
                    ipv4=str(item.get("IPv4") or "").strip(),
                    gateway=str(item.get("Gateway") or "").strip(),
                    index=int(item["Index"]),
                )
            )
        return adapters

    def selected_adapter(self):
        value = self.adapter_var.get().strip()
        name = self.adapter_display_map.get(value, "")
        if not name or name == "未检测到网卡":
            return ""
        return name

    def selected_adapter_info(self):
        name = self.selected_adapter()
        for adapter in self.adapters:
            if adapter.name == name:
                return adapter
        return Adapter(name=name)

    def read_config(self, adapter):
        # Only an integer interface index is interpolated into PowerShell code.
        index = int(adapter.index)
        if index <= 0:
            raise ValueError("网卡索引无效，请刷新网卡。")
        ps = (
            f"$idx={index}; "
            "$nic = Get-NetAdapter -InterfaceIndex $idx; "
            "$iface = Get-NetIPInterface -InterfaceIndex $idx -AddressFamily IPv4; "
            "$ips = @(Get-NetIPAddress -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue | Sort-Object SkipAsSource); "
            "$routes = @(Get-NetRoute -InterfaceIndex $idx -AddressFamily IPv4 -ErrorAction SilentlyContinue); "
            "$dns = Get-DnsClientServerAddress -InterfaceIndex $idx -AddressFamily IPv4; "
            "[pscustomobject]@{ Name=$nic.Name; Dhcp=[string]$iface.Dhcp; "
            "Addresses=@($ips | Select-Object IPAddress,PrefixLength); "
            "Gateways=@($routes | Where-Object DestinationPrefix -eq '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -ExpandProperty NextHop); "
            "Dns=@($dns.ServerAddresses) } | ConvertTo-Json -Depth 5 -Compress"
        )
        data = powershell_json(ps)
        if not isinstance(data, dict) or data.get("Name") != adapter.name:
            raise RuntimeError("网卡已发生变化，请刷新后重试。")
        if data.get("Dhcp") not in ("Enabled", "Disabled"):
            raise RuntimeError("无法确定网卡的 DHCP 状态。")
        addresses = data.get("Addresses") or []
        gateways = data.get("Gateways") or []
        dns = data.get("Dns") or []
        first = addresses[0] if addresses else {}
        config = {
            "interface": adapter.name,
            "mode": "dhcp" if data["Dhcp"] == "Enabled" else "static",
            "ip": first.get("IPAddress", ""),
            "mask": str(ipaddress.IPv4Network(f"0.0.0.0/{first['PrefixLength']}").netmask) if first else "",
            "gateway": gateways[0] if gateways else "",
            "dns1": dns[0] if dns else "",
            "dns2": dns[1] if len(dns) > 1 else "",
        }
        return config, data

    def show_config(self, adapter, result):
        config, details = result
        self.form_adapter_name = adapter.name
        self.mode_var.set(config["mode"])
        for key, variable in self.form_variables().items():
            variable.set(config[key])
            self.check_field(key, variable)
        adapter.ipv4 = config["ip"]
        adapter.gateway = config["gateway"]
        self.update_adapter_card(adapter)
        self.update_mode()
        self.write_preview(
            f"当前选择网卡：{adapter.name}\n描述：{adapter.description or '未知'}\n"
            f"模式：{'自动获取 DHCP' if config['mode'] == 'dhcp' else '静态 IP'}\n\n"
            + json.dumps(details, ensure_ascii=False, indent=2)
        )
        self.status_label.configure(text=f"已读取“{adapter.name}”的当前配置。")

    def form_variables(self):
        return {"ip": self.ip_var, "mask": self.mask_var, "gateway": self.gateway_var,
                "dns1": self.dns1_var, "dns2": self.dns2_var}

    def load_current_config(self):
        if self.busy or not self.selected_adapter():
            return
        adapter = self.selected_adapter_info()
        self.update_adapter_card(adapter)
        self.start_task(lambda: self.read_config(adapter),
                        lambda result: self.show_config(adapter, result), "正在读取当前配置...")

    def update_mode(self):
        static = self.mode_var.get() == "static"
        state = "normal" if static else "disabled"
        for entry in self.entries.values():
            entry.configure(state=state)
            self.transitions.color(entry, "fg_color", "#f9fbfe" if static else "#f1f3f7", 0.20)
        self.transitions.color(self.mode_hint, "fg_color", "#f2f6fc" if static else "#eaf3fe", 0.20)
        self.mode_hint.configure(text="  手动指定地址和 DNS，适合固定网络环境。" if static else "  由路由器自动分配 IP 地址和 DNS，无需填写下方信息。")
        self.clear_button.configure(state="normal" if static else "disabled")
        if not static:
            commands = self.build_commands({"interface": self.selected_adapter(), "mode": "dhcp"})
            self.write_preview(f"将对网卡“{self.selected_adapter()}”执行：\n\n" + "\n".join(map(format_command, commands)))

    def collect_config(self):
        return {
            "interface": self.selected_adapter(),
            "mode": self.mode_var.get(),
            "ip": self.ip_var.get().strip(),
            "mask": self.mask_var.get().strip(),
            "gateway": self.gateway_var.get().strip(),
            "dns1": self.dns1_var.get().strip(),
            "dns2": self.dns2_var.get().strip(),
        }

    def validate_config(self, config):
        if not config["interface"] or config["interface"] not in self.adapter_display_map.values():
            raise ValueError("请先选择有效的目标网卡。")
        if config["mode"] not in ("static", "dhcp"):
            raise ValueError("配置模式必须是 static 或 dhcp。")
        if config["mode"] == "dhcp":
            return
        if not is_valid_ip(config["ip"]):
            raise ValueError("IP 地址格式不正确。")
        if not is_valid_mask(config["mask"]):
            raise ValueError("子网掩码必须是连续掩码，例如 255.255.255.0。")
        address = ipaddress.IPv4Address(config["ip"])
        network = ipaddress.IPv4Network(f"{address}/{config['mask']}", strict=False)
        if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_reserved:
            raise ValueError("请填写可用的单播 IP 地址。")
        if network.prefixlen < 31 and address in (network.network_address, network.broadcast_address):
            raise ValueError("IP 地址不能是网络地址或广播地址。")
        for key, label in (("gateway", "默认网关"), ("dns1", "首选 DNS"), ("dns2", "备用 DNS")):
            if not is_valid_ip(config[key], allow_empty=True):
                raise ValueError(f"{label}格式不正确。")
            if config[key]:
                value = ipaddress.IPv4Address(config[key])
                if value.is_unspecified or value.is_multicast or value.is_reserved:
                    raise ValueError(f"{label}必须是可用的单播地址。")
        if config["gateway"]:
            gateway = ipaddress.IPv4Address(config["gateway"])
            if gateway.is_loopback or gateway == address or gateway not in network:
                raise ValueError("默认网关必须与 IP 地址在同一子网内，且不能等于本机地址。")
            if network.prefixlen < 31 and gateway in (network.network_address, network.broadcast_address):
                raise ValueError("默认网关不能是网络地址或广播地址。")
        if config["dns2"] and not config["dns1"]:
            raise ValueError("填写备用 DNS 前，请先填写首选 DNS。")
        if config["dns1"] and config["dns1"] == config["dns2"]:
            raise ValueError("首选和备用 DNS 不能相同。")

    def build_commands(self, config, interface_index=None):
        target = str(interface_index) if interface_index is not None else config["interface"]
        base = [NETSH, "interface", "ipv4"]
        if config["mode"] == "dhcp":
            return [
                base + ["set", "address", f"name={target}", "source=dhcp"],
                base + ["set", "dnsservers", f"name={target}", "source=dhcp"],
            ]
        commands = [base + ["set", "address", f"name={target}", "source=static",
                           f"address={config['ip']}", f"mask={config['mask']}",
                           f"gateway={config['gateway'] or 'none'}"]]
        commands.append(base + ["set", "dnsservers", f"name={target}", "source=static",
                                f"address={config['dns1'] or 'none'}", "validate=no"])
        if config["dns2"]:
            commands.append(base + ["add", "dnsservers", f"name={target}",
                                    f"address={config['dns2']}", "index=2", "validate=no"])
        return commands

    def execute_config(self, adapter, commands):
        # Re-read identity and save the original configuration before any mutation.
        original, details = self.read_config(adapter)
        backup_dir = CONFIG_DIR.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".json")
        with backup.open("x", encoding="utf-8") as file:
            json.dump({**original, "system_details": details}, file, ensure_ascii=False, indent=2)
        completed = 0
        for cmd in commands:
            try:
                run_cmd(cmd, check=True, timeout=30)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"已停止执行。失败命令：{format_command(cmd)}\n{exc}\n\n"
                    f"此前完成 {completed} 条命令，网络配置可能已部分改变。\n"
                    f"原配置备份：{backup}\n可通过导入备份查看并恢复基本 IPv4 配置；完整系统记录保存在 system_details。"
                ) from exc
            completed += 1
        return backup

    def apply_config(self):
        if self.busy:
            return
        try:
            config = self.collect_config()
            self.validate_config(config)
            if self.form_adapter_name != config["interface"]:
                raise ValueError("尚未成功读取此网卡的配置，请先读取配置或导入配置。")
            adapter = self.selected_adapter_info()
            if adapter.index <= 0:
                raise ValueError("网卡索引无效，请刷新网卡。")
            commands = self.build_commands(config, adapter.index)
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        if not ctypes.windll.shell32.IsUserAnAdmin():
            messagebox.showerror("需要管理员权限", "请关闭程序，然后右键选择“以管理员身份运行”，再应用配置。")
            return
        self.write_preview("即将执行以下命令：\n\n" + "\n".join(map(format_command, commands)))
        if not messagebox.askyesno("确认应用", f"确定要把配置应用到网卡“{adapter.name}”吗？\n\n这会修改当前网络连接，替换现有 IPv4 地址、网关和 IPv4 DNS。"):
            return

        def applied(backup):
            self.status_label.configure(text=f"已应用到“{adapter.name}”。")
            messagebox.showinfo("应用完成", f"系统已接受配置命令。\n原配置备份：{backup}")
            self.refresh_adapters()

        self.start_task(lambda: self.execute_config(adapter, commands), applied, "正在应用配置，请稍候...")

    def clear_form(self):
        if self.busy:
            return
        for key, variable in self.form_variables().items():
            variable.set("")
            self.check_field(key, variable)
        self.write_preview("已清空填写。")

    def import_config(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title="导入配置 JSON", filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            if Path(path).stat().st_size > 1024 * 1024:
                raise ValueError("配置文件不能超过 1 MB。")
            with open(path, "r", encoding="utf-8-sig") as file:
                data = json.load(file)
            if not isinstance(data, dict):
                raise ValueError("配置文件顶层必须是 JSON 对象。")
            keys = ("interface", "mode", "ip", "mask", "gateway", "dns1", "dns2")
            config = {key: data.get(key, "static" if key == "mode" else "") for key in keys}
            config["dns1"] = data.get("dns") or config["dns1"]
            if any(not isinstance(value, str) for value in config.values()):
                raise ValueError("配置字段必须是字符串。")
            config = {key: value.strip() for key, value in config.items()}
            target = config["interface"] or self.selected_adapter()
            config["interface"] = target
            self.validate_config(config)
            display = next(display for display, name in self.adapter_display_map.items() if name == target)
            self.adapter_var.set(display)
            self.form_adapter_name = target
            self.update_adapter_card(self.selected_adapter_info())
            self.mode_var.set(config["mode"])
            for key, variable in self.form_variables().items():
                variable.set(config[key])
                self.check_field(key, variable)
            self.update_mode()
            self.write_preview("已导入配置：\n\n" + json.dumps(config, ensure_ascii=False, indent=2))
            self.status_label.configure(text=f"配置已导入，目标网卡：“{target}”。")
        except (OSError, ValueError, StopIteration) as exc:
            messagebox.showerror("导入失败", str(exc))

    def export_config(self):
        if self.busy:
            return
        try:
            config = self.collect_config()
            self.validate_config(config)
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            path = filedialog.asksaveasfilename(
                title="导出当前配置", initialdir=str(CONFIG_DIR), defaultextension=".json",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as file:
                json.dump(config, file, ensure_ascii=False, indent=2)
            messagebox.showinfo("导出成功", f"配置已导出到：\n{path}")
        except (OSError, ValueError) as exc:
            messagebox.showerror("导出失败", str(exc))


def main():
    if os.name == "nt":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NetworkSwitcher.Desktop.App")
    app = NetworkSwitcher()
    app.mainloop()


if __name__ == "__main__":
    main()
