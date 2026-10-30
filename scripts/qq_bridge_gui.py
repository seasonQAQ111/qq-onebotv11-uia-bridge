r"""Desktop console for the QQ <-> AstrBot UIA bridge.

Lists every QQ window that is signed in on this PC, lets you choose which
account the bridge should drive, and starts/stops the bridge process.

Run it with the project venv (scripts\start-bridge-gui.ps1 does that).
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
LOG_DIR = os.path.join(PROJECT, "logs")
CONFIG = os.path.join(HERE, "bridge-config.json")
BRIDGE = os.path.join(HERE, "onebot_uia_bridge.py")
BRIDGE_LOG = os.path.join(LOG_DIR, "uia-bridge.log")
PID_FILE = os.path.join(LOG_DIR, "uia-bridge.pid")
START_SCRIPT = os.path.join(HERE, "start-bridge.ps1")
STOP_SCRIPT = os.path.join(HERE, "stop-bridge.ps1")

sys.path.insert(0, HERE)

BG = "#f4f5f7"
CARD = "#ffffff"
FG = "#1f2328"
MUTED = "#6b7280"
OK = "#1a7f37"
BAD = "#b42318"


# ---------------------------------------------------------------- helpers

def read_config():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)


def write_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


def watched_names(cfg):
    return [c["name"] for c in (cfg.get("chats") or []) if c.get("name")]


def read_pid():
    try:
        with open(PID_FILE, encoding="ascii") as fh:
            return int(fh.read().strip())
    except Exception:
        return 0


def pid_alive(pid):
    if not pid:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def log_lines(limit=400):
    if not os.path.exists(BRIDGE_LOG):
        return []
    try:
        with open(BRIDGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 200000))
            text = fh.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    return [ln for ln in text.splitlines() if ln.strip()][-limit:]


def detect_windows():
    """Read every QQ window: account nickname, sessions, watched hits."""
    import uiautomation as auto

    import qq_uia

    cfg = read_config()
    watched = watched_names(cfg)
    out = []
    with auto.UIAutomationInitializerInThread():
        wins = qq_uia.find_qq_windows()
        for hwnd, title in wins:
            win = qq_uia.QQWindow()
            win.hwnd = hwnd
            win.title = title
            try:
                win.win = auto.ControlFromHandle(hwnd)
            except Exception:
                continue
            account = ""
            sessions = []
            try:
                account = win.account_nickname()
            except Exception:
                account = ""
            try:
                sessions = win.list_sessions()
            except Exception:
                sessions = []
            hits = [n for n in watched if n in sessions]
            out.append({
                "hwnd": hwnd,
                "title": title,
                "account": account,
                "sessions": sessions,
                "hits": hits,
                "watched": watched,
            })
    return out


def run_script(script):
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        raise RuntimeError("PowerShell not found")
    proc = subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
        cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    raw = proc.stdout or b""
    for enc in ("utf-8", "gbk"):
        try:
            return proc.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return proc.returncode, raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- app

class BridgeConsole:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.ticks = 0
        self.windows = []
        self.last_log = ""

        root.title("QQ 桥接控制台")
        root.geometry("1000x700")
        root.minsize(820, 560)
        root.configure(bg=BG)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))

        self._build_windows_section()
        self._build_bridge_section()
        self._build_log_section()

        self.refresh_windows()
        self.refresh_status()
        self.refresh_log()
        self.root.after(3000, self._tick)

    # ---- layout ----

    def _build_windows_section(self):
        frame = ttk.LabelFrame(self.root, text=" QQ 窗口（本机已登录） ")
        frame.pack(fill="x", padx=12, pady=(12, 8))

        cols = ("account", "sessions", "watched", "preview")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", height=4)
        self.tree.heading("account", text="账号")
        self.tree.heading("sessions", text="会话数")
        self.tree.heading("watched", text="含监视会话")
        self.tree.heading("preview", text="会话预览")
        self.tree.column("account", width=150, anchor="w")
        self.tree.column("sessions", width=70, anchor="center")
        self.tree.column("watched", width=100, anchor="center")
        self.tree.column("preview", width=560, anchor="w")
        self.tree.pack(fill="x", padx=10, pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_pick_label())

        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_refresh = ttk.Button(bar, text="刷新列表", command=self.refresh_windows)
        self.btn_refresh.pack(side="left")
        self.btn_pick = ttk.Button(bar, text="设为桥接使用的 QQ", command=self.pick_account)
        self.btn_pick.pack(side="left", padx=6)
        self.lbl_pick = ttk.Label(bar, text="桥接当前使用：-", foreground=MUTED)
        self.lbl_pick.pack(side="left", padx=12)

    def _build_bridge_section(self):
        frame = ttk.LabelFrame(self.root, text=" 桥接 ")
        frame.pack(fill="x", padx=12, pady=8)

        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=(8, 4))
        self.lbl_state = ttk.Label(row, text="状态：读取中", font=("Microsoft YaHei UI", 10, "bold"))
        self.lbl_state.pack(side="left")
        self.lbl_link = ttk.Label(row, text="", foreground=MUTED)
        self.lbl_link.pack(side="left", padx=14)

        row2 = ttk.Frame(frame)
        row2.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_start = ttk.Button(row2, text="启动桥接", command=lambda: self.bridge_action("start"))
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row2, text="停止桥接", command=lambda: self.bridge_action("stop"))
        self.btn_stop.pack(side="left", padx=6)
        self.btn_restart = ttk.Button(row2, text="重启桥接", command=lambda: self.bridge_action("restart"))
        self.btn_restart.pack(side="left")
        ttk.Button(row2, text="打开日志文件", command=self.open_log).pack(side="right")

    def _build_log_section(self):
        frame = ttk.LabelFrame(self.root, text=" 运行日志 ")
        frame.pack(fill="both", expand=True, padx=12, pady=(8, 12))

        self.log_text = tk.Text(frame, wrap="none", bg="#11151c", fg="#d7dce5",
                                insertbackground="#d7dce5", relief="flat", height=16)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        bar.pack(side="right", fill="y", pady=10, padx=(0, 10))
        self.log_text.configure(yscrollcommand=bar.set)
        self.log_text.configure(state="disabled")

    # ---- background helpers ----

    def _work(self, fn, done):
        if self.busy:
            return
        self.busy = True
        self._set_buttons("disabled")

        def runner():
            try:
                result = fn()
            except Exception as exc:  # keep the UI alive on any failure
                result = exc
            self.events.put((done, result))

        threading.Thread(target=runner, daemon=True).start()

    def _drain(self):
        while True:
            try:
                done, result = self.events.get_nowait()
            except queue.Empty:
                return
            self.busy = False
            self._set_buttons("normal")
            done(result)

    def _set_buttons(self, state):
        for btn in (self.btn_start, self.btn_stop, self.btn_restart,
                    self.btn_refresh, self.btn_pick):
            btn.configure(state=state)

    # ---- QQ windows ----

    def refresh_windows(self):
        self._work(detect_windows, self._show_windows)

    def _show_windows(self, rows):
        if isinstance(rows, Exception):
            messagebox.showerror("读取 QQ 窗口失败", str(rows))
            return
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            hits = row["hits"]
            watched = row["watched"]
            preview = " / ".join(row["sessions"][:6])
            if len(row["sessions"]) > 6:
                preview += " …"
            account = row["account"] or "(读不到昵称)"
            self.tree.insert("", "end", iid=str(row["hwnd"]),
                             values=(account, len(row["sessions"]),
                                     "%d/%d" % (len(hits), len(watched)), preview))
        self.windows = rows
        self._sync_pick_label()

    def _selected_window(self):
        sel = self.tree.selection()
        if not sel:
            return None
        hwnd = sel[0]
        for row in getattr(self, "windows", []):
            if str(row["hwnd"]) == hwnd:
                return row
        return None

    def _sync_pick_label(self):
        try:
            cfg = read_config()
            current = str(cfg.get("window_account") or "").strip() or "自动（按会话匹配）"
        except Exception:
            current = "-"
        row = self._selected_window()
        extra = ""
        if row:
            extra = "　|　已选：%s" % (row["account"] or "(读不到昵称)")
        self.lbl_pick.configure(text="桥接当前使用：%s%s" % (current, extra))

    def pick_account(self):
        row = self._selected_window()
        if not row:
            messagebox.showinfo("选择 QQ", "先在上面的列表里点一个 QQ 窗口")
            return
        account = row["account"]
        if not account:
            messagebox.showwarning("读不到昵称", "这个窗口读不到登录昵称，换一个窗口试试")
            return
        cfg = read_config()
        cfg["window_account"] = account
        write_config(cfg)
        self._sync_pick_label()
        running = pid_alive(read_pid())
        if running and messagebox.askyesno(
                "已保存", "已设为 %s。\n重启桥接后生效，现在重启吗？" % account):
            self.bridge_action("restart")

    # ---- bridge ----

    def bridge_action(self, what):
        if what == "start":
            jobs = [(START_SCRIPT, "启动")]
        elif what == "stop":
            jobs = [(STOP_SCRIPT, "停止")]
        else:
            jobs = [(STOP_SCRIPT, "停止"), (START_SCRIPT, "启动")]

        def runner():
            out = []
            for script, label in jobs:
                code, text = run_script(script)
                out.append((label, code, text))
            return out

        self._work(runner, self._after_bridge_action)

    def _after_bridge_action(self, result):
        if isinstance(result, Exception):
            messagebox.showerror("操作失败", str(result))
            return
        for label, code, text in result:
            if code != 0:
                messagebox.showerror("%s失败" % label, text.strip() or "exit %d" % code)
        self.refresh_status()
        self.refresh_log()

    def refresh_status(self):
        pid = read_pid()
        running = pid_alive(pid)
        lines = log_lines(60)
        connected = False
        attached = ""
        for line in lines:
            if "connected to AstrBot" in line:
                connected = True
            elif "reconnecting" in line or "ws error" in line:
                connected = False
            if "QQ attached:" in line:
                attached = line.split("QQ attached:", 1)[1].strip()
        if running:
            self.lbl_state.configure(text="状态：运行中 (pid %d)" % pid, foreground=OK)
            link = "AstrBot：已连接" if connected else "AstrBot：未连接"
            if attached:
                link += "　|　%s" % attached
            self.lbl_link.configure(text=link, foreground=OK if connected else BAD)
        else:
            self.lbl_state.configure(text="状态：已停止", foreground=BAD)
            self.lbl_link.configure(text="", foreground=MUTED)

    def refresh_log(self):
        lines = log_lines()
        text = "\n".join(lines)
        if text == self.last_log:
            return
        self.last_log = text
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def open_log(self):
        if os.path.exists(BRIDGE_LOG):
            os.startfile(BRIDGE_LOG)
        else:
            messagebox.showinfo("日志", "还没有日志文件：%s" % BRIDGE_LOG)

    # ---- loop ----

    def _tick(self):
        self._drain()
        self.refresh_status()
        self.refresh_log()
        self.ticks += 1
        if self.ticks % 7 == 0 and not self.busy:
            self.refresh_windows()
        self.root.after(3000, self._tick)


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:
        pass
    style.configure("TLabelframe", background=BG)
    style.configure("TLabelframe.Label", background=BG, foreground=FG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    BridgeConsole(root)
    root.mainloop()


if __name__ == "__main__":
    main()
