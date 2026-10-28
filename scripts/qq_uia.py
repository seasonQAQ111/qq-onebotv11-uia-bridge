# -*- coding: utf-8 -*-
"""
QQ NT UI Automation controller.

Talks to the real QQ desktop client the way a human does: it reads the message
list and types into the input box through Windows UI Automation. No process
injection and no protocol hook.

Verified against the QQ build on this machine:
  - top level window: class "Chrome_WidgetWin_1", title "QQ"
  - the UIA tree is built once a client asks for it and the window has been
    foreground at least once; after that reads keep working in the background
  - message list: Pane named "\u6d88\u606f\u5217\u8868"; each message is a group
    class "message" and self ones carry "message-container--self"
  - session list: Pane named "\u4f1a\u8bdd\u5217\u8868" holding
    "recent-contact-item" rows. A row exposes title, timestamp, preview and an
    optional red unread badge, so new traffic can be spotted without clicking.
  - switching: the conversation row is clicked inside the "\u4f1a\u8bdd\u5217\u8868" pane,
    and the list is scrolled with the wheel when the row is off screen. The chat
    header is then read back, so a mis-click is never mistaken for a switch.
  - editor: GroupControl whose class contains "ProseMirror" and
    "ExEditor-qq-msg-editor". It carries "is-empty" only while the box is empty,
    so a send can be confirmed to have actually left the input box.
  - send button: ButtonControl class "send-msg"
  - chat header: ButtonControl class "chat-header__contact-name" whose Name is
    the conversation currently on screen

Every UIA property read goes through _safe()/_nname()/_nclass()... because
Chromium drops nodes whenever it re-renders and a stale node then raises
comtypes.COMError ("event cannot call any subscriber"). One dead node must
never take down a poll cycle.

CLI:
    python qq_uia.py chats
    python qq_uia.py sessions
    python qq_uia.py read  [--chat NAME] [--json]
    python qq_uia.py send  --text "..." [--chat NAME]
    python qq_uia.py open  --chat NAME
    python qq_uia.py probe [--chat NAME]
"""

import argparse
import ctypes
import json
import os
import re
import subprocess
import time

import uiautomation as auto
import pyperclip

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_GETOBJECT = 0x003D
OBJID_CLIENT = 0xFFFFFFFC
SW_RESTORE = 9
VK_MENU = 0x12
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
GA_ROOT = 2
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

QQ_WINDOW_CLASS = "Chrome_WidgetWin_1"
RENDERER_CLASS = "Chrome_RenderWidgetHostHWND"
MSG_LIST_NAME = "\u6d88\u606f\u5217\u8868"
SESSION_LIST_NAME = "\u4f1a\u8bdd\u5217\u8868"
IMAGE_TAG = "\u56fe\u7247"
HEADER_NAME_CLASS = "chat-header__contact-name"

# The left rail shows a weather chip ("阴", "晴", ...) right under the account
# name, so the account lookup skips those short text nodes.
WEATHER_WORDS = {
    "\u6674", "\u9634", "\u591a\u4e91", "\u5c0f\u96e8", "\u4e2d\u96e8", "\u5927\u96e8", "\u66b4\u96e8",
    "\u96f7\u9635\u96e8", "\u9635\u96e8", "\u96ea", "\u5c0f\u96ea", "\u4e2d\u96ea", "\u5927\u96ea",
    "\u96fe", "\u973e", "\u6d6e\u5c18", "\u626c\u6c99", "\u98ce", "\u5927\u98ce", "\u4eca\u5929",
}
EDITOR_CLASS = "ExEditor-qq-msg-editor"
EMPTY_EDITOR_CLASS = "is-empty"
SEND_BUTTON_CLASS = "send-msg"
BADGE_CLASS = "q-badge"
TS_RE = re.compile(r"^\d{1,2}:\d{2}$")
UNREAD_RE = re.compile(r"^(\d+)\+?$")


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _nname(node):
    return _safe(lambda: (node.Name or "").strip()) or ""


def _nclass(node):
    return _safe(lambda: node.ClassName or "") or ""


def _ntype(node):
    return _safe(lambda: node.ControlTypeName) or ""


def _nrect(node):
    return _safe(lambda: node.BoundingRectangle)


def _rect_contains(outer, inner):
    try:
        return (inner.left >= outer.left and inner.right <= outer.right
                and inner.top >= outer.top and inner.bottom <= outer.bottom)
    except Exception:
        return False


def _window_at(x, y):
    """Top level HWND sitting at a screen point, 0 when there is none."""
    try:
        pt = ctypes.wintypes.POINT(int(x), int(y))
        h = user32.WindowFromPoint(pt)
        if not h:
            return 0
        return user32.GetAncestor(h, GA_ROOT) or h
    except Exception:
        return 0


def _window_desc(hwnd):
    """'title (class, pid)' for logs, without raising on a dead handle."""
    if not hwnd:
        return "<nothing>"
    try:
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return "%r (%s, pid %d)" % (title.value, cls.value, pid.value)
    except Exception:
        return hex(hwnd)


def _process_image_name(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def qq_pids():
    """Return pids of running QQ.exe processes (window enumeration + image name)."""
    pids = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value and pid.value not in pids:
            if _process_image_name(pid.value).lower().endswith("\\qq.exe"):
                pids.add(pid.value)
        return True

    user32.EnumWindows(cb, 0)
    return pids


def find_qq_windows():
    """Return [(hwnd, title)] for visible QQ top level windows, main window first."""
    pids = qq_pids()
    results = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != QQ_WINDOW_CLASS:
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        # Closing QQ to tray hides the main window. It is still a valid UIA
        # target and can be restored, so do not discard it as "not running".
        if not user32.IsWindowVisible(hwnd) and title.value != "QQ":
            return True
        results.append((hwnd, title.value))
        return True

    user32.EnumWindows(cb, 0)
    results.sort(key=lambda t: (t[1] != "QQ",))
    return results


def find_renderer_hwnd(top_hwnd):
    """Find the Chromium renderer child window (diagnostics only)."""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value == RENDERER_CLASS:
            found.append(hwnd)
        return True

    user32.EnumChildWindows(top_hwnd, cb, 0)
    return found[0] if found else None


class QQWindow:
    def __init__(self):
        self.hwnd = None
        self.renderer_hwnd = None
        self.title = ""
        self.win = None

    # ---- connect ----

    def find(self, chat_name=None):
        wins = find_qq_windows()
        if not wins:
            return None
        if chat_name:
            for h, t in wins:
                if chat_name in t:
                    return h, t
        return wins[0]

    def account_nickname(self):
        """Nickname of the QQ account signed in in this window, '' if unknown.

        Two accounts can be signed in at once and both windows are titled
        "QQ", so the account name in the left rail (the text above the search
        box) is what tells them apart.
        """
        def once():
            self.poke()
            for node in self._walk(self._fresh_root()):
                if _ntype(node) != "TextControl":
                    continue
                name = _nname(node)
                if not name or name in WEATHER_WORDS or len(name) > 24:
                    continue
                return name
            return ""

        try:
            return self._retry(once, attempts=2)
        except Exception:
            return ""

    def connect(self, chat_name=None, expect=None, account=None):
        """Attach to the QQ window that actually holds the conversations we watch.

        Two QQ accounts can be signed in at once, and then several windows are
        titled "QQ". Picking the first one can latch onto the wrong account and
        every switch then fails, so candidates are scored by how many of the
        expected conversation names they expose.

        `account` pins the window by the nickname shown in its left rail;
        scoring stays as the fallback when that name is not found.
        """
        wins = find_qq_windows()
        if not wins:
            raise RuntimeError("QQ window not found (is the client running and visible?)")

        pick = None
        if account:
            named = []
            for h, title in wins:
                self.hwnd, self.title = h, title
                try:
                    self.win = auto.ControlFromHandle(h)
                except Exception:
                    continue
                here = self.account_nickname()
                if here == account:
                    named.append((h, title))
            if named:
                pick = named[0]
                if len(named) > 1:
                    print("qq_uia: warning - %d windows report account %r, "
                          "using the first" % (len(named), account))
            else:
                print("qq_uia: warning - no QQ window reports account %r, "
                      "falling back to session matching" % account)
        if expect and pick is None:
            best_score, best = -1, None
            for h, title in wins:
                self.hwnd, self.title = h, title
                try:
                    self.win = auto.ControlFromHandle(h)
                except Exception:
                    continue
                names = set()
                try:
                    names = set(self.list_sessions())
                except Exception:
                    pass
                score = sum(1 for n in expect if n in names)
                try:
                    if self.current_chat() in expect:
                        score += 1
                except Exception:
                    pass
                if score > best_score:
                    best_score, best = score, (h, title)
                if best_score >= len(expect) + 1:
                    break
            if best is not None and best_score > 0:
                pick = best
                if best_score < len(expect):
                    print("qq_uia: warning - the chosen QQ window only shows %d of %d "
                          "watched conversations" % (best_score, len(expect)))
        if pick is None:
            pick = self.find(chat_name)
        if not pick:
            raise RuntimeError("QQ window not found (is the client running and visible?)")
        self.hwnd, self.title = pick
        self.force_foreground()
        # Asking for the element makes Chromium build the accessibility tree.
        self.win = auto.ControlFromHandle(self.hwnd)
        if self.win is None:
            raise RuntimeError("failed to build UIA root for QQ window")
        self.renderer_hwnd = find_renderer_hwnd(self.hwnd)
        return self

    def click(self, x, y, label="", wait=0.25):
        """Click a screen point, but only if it really belongs to this window.

        Several chat clients are usually open at once, and a click aimed at QQ
        lands in whatever window happens to be on top of that pixel -- which is
        how a stray click can poke another app. Checking first (and clicking
        with SetCursorPos + a relative button event rather than uiautomation's
        normalised absolute move) keeps the mouse inside QQ.
        """
        x, y = int(x), int(y)
        root = _window_at(x, y)
        if root != self.hwnd:
            self.force_foreground()
            root = _window_at(x, y)

        temporary_topmost = False
        was_topmost = bool(user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
        if root != self.hwnd and not was_topmost:
            # Windows can deny SetForegroundWindow when the bridge runs from a
            # background process. Briefly raising QQ above other windows makes
            # the target pixel belong to QQ again; the click then activates it.
            user32.SetWindowPos(
                self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
            time.sleep(0.15)
            temporary_topmost = True
            root = _window_at(x, y)

        if root != self.hwnd:
            if temporary_topmost:
                user32.SetWindowPos(
                    self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
                )
            raise RuntimeError("%s: refusing to click (%d,%d) - that point belongs to %s, "
                               "not to the QQ window %s"
                               % (label or "click", x, y, _window_desc(root),
                                  _window_desc(self.hwnd)))
        try:
            user32.SetCursorPos(x, y)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(wait)
        finally:
            if temporary_topmost:
                user32.SetWindowPos(
                    self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
                )

    def click_node(self, node, label=""):
        """Click a point inside a UIA element after checking who owns it.

        The input-method candidate window (Sogou's SoPY_Comp and friends) is a
        separate window that can sit right on the centre of the editor. The
        ownership check then refuses the click - correctly, because that pixel
        belongs to the IME - and the message is dropped with "refusing to
        click". Try a few points inside the element instead and take the first
        one that really belongs to QQ.
        """
        r = _nrect(node)
        if r is None or (r.right - r.left) <= 0 or (r.bottom - r.top) <= 0:
            raise RuntimeError("%s: element has no usable geometry" % (label or "click"))
        w, h = r.right - r.left, r.bottom - r.top
        pad_x, pad_y = max(6, w // 10), max(6, h // 4)
        points = (
            (r.left + w // 2, r.top + h // 2),
            (r.left + pad_x, r.top + h // 2),
            (r.right - pad_x, r.top + h // 2),
            (r.left + pad_x, r.bottom - pad_y),
            (r.right - pad_x, r.bottom - pad_y),
        )
        first_error = None
        for x, y in points:
            try:
                self.click(x, y, label)
                return
            except RuntimeError as exc:
                if first_error is None:
                    first_error = exc
        raise first_error

    def alive(self):
        return bool(self.hwnd) and bool(_safe(lambda: user32.IsWindow(self.hwnd)))

    def force_foreground(self):
        if not self.hwnd:
            return False
        try:
            if not user32.IsWindowVisible(self.hwnd) or user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, SW_RESTORE)
            if user32.GetForegroundWindow() == self.hwnd:
                return True
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, 2, 0)
            user32.SetForegroundWindow(self.hwnd)
            time.sleep(0.3)
            return user32.GetForegroundWindow() == self.hwnd
        except Exception:
            return False

    def poke(self):
        """Re-assert the a11y client so the Chromium tree does not freeze."""
        if not self.alive():
            return
        try:
            res = ctypes.c_ulong()
            user32.SendMessageTimeoutW(self.hwnd, WM_GETOBJECT, 0, OBJID_CLIENT,
                                       0x0002, 200, ctypes.byref(res))
        except Exception:
            pass

    # ---- traverse ----

    @staticmethod
    def _walk(root, max_depth=2000, max_nodes=12000):
        stack = [(root, 0)]
        out = []
        while stack and len(out) < max_nodes:
            c, d = stack.pop()
            if d > max_depth:
                continue
            out.append(c)
            kids = _safe(lambda: c.GetChildren(), []) or []
            for ch in reversed(kids):
                stack.append((ch, d + 1))
        return out

    def _fresh_root(self):
        win = auto.ControlFromHandle(self.hwnd)
        if win is None:
            raise RuntimeError("UIA root unavailable for hwnd %s" % hex(self.hwnd or 0))
        self.win = win
        return win

    def _retry(self, fn, attempts=3, delay=0.35):
        """Retry a UIA read that may hit a half-torn-down tree."""
        last = None
        for _ in range(max(1, attempts)):
            try:
                return fn()
            except Exception as e:
                last = e
                self.poke()
                time.sleep(delay)
                try:
                    self._fresh_root()
                except Exception:
                    pass
        raise last

    def _pane_by_name(self, name):
        def once():
            win = self._fresh_root()
            c = auto.PaneControl(searchFromControl=win, Name=name)
            if c.Exists(0.6, 0.1):
                return c
            c = auto.Control(searchFromControl=win, Name=name)
            return c if c.Exists(0.4, 0.05) else None

        try:
            return self._retry(once, attempts=2)
        except Exception:
            return None

    def _msg_list(self):
        return self._pane_by_name(MSG_LIST_NAME)

    # ---- current conversation ----

    def _current_chat_once(self):
        self.poke()
        for node in self._walk(self._fresh_root()):
            if _ntype(node) == "ButtonControl" and HEADER_NAME_CLASS in _nclass(node):
                n = _nname(node)
                if n:
                    return n
        return ""

    def current_chat(self):
        """Name of the conversation on screen (chat header), '' if unknown."""
        try:
            return self._retry(self._current_chat_once)
        except Exception:
            return ""

    # ---- session list (read without clicking) ----

    def _session_nodes(self):
        self.poke()
        sess = self._pane_by_name(SESSION_LIST_NAME)
        if sess is None:
            return []
        return [n for n in self._walk(sess) if "recent-contact-item" in _nclass(n)]

    def session_items(self):
        """Snapshot QQ's session list without touching the mouse.

        Returns [{'name','time','preview','unread'}]. The unread count comes
        from the red badge, which lets the bridge see *which* conversation has
        something new before deciding to switch into it.
        """
        items = []
        for node in self._session_nodes():
            raw = []
            for d in self._walk(node):
                if _ntype(d) != "TextControl":
                    continue
                raw.append((_nname(d), _nclass(d), _nrect(d)))
            badges = [r for n, c, r in raw if BADGE_CLASS in c and r is not None]
            visible, unread = [], 0
            for n, c, r in raw:
                if BADGE_CLASS in c or not n:
                    continue
                inside = r is not None and any(_rect_contains(b, r) for b in badges)
                if inside:
                    m = UNREAD_RE.match(n)
                    if m and not unread:
                        unread = int(m.group(1))
                    continue
                visible.append(n)
            if not visible:
                continue
            name = visible[0]
            rest = list(visible[1:])
            tstamp = ""
            if rest and TS_RE.match(rest[0]):
                tstamp = rest.pop(0)
            items.append({"name": name, "time": tstamp,
                          "preview": "".join(rest), "unread": unread})
        return items

    def list_sessions(self):
        return [i["name"] for i in self.session_items()]

    # ---- switching (session list only) ----

    def _session_row_label(self, node):
        """First real text of a session row, skipping unread/level badges."""
        for d in self._walk(node):
            if _ntype(d) == "TextControl":
                n = _nname(d)
                if n and BADGE_CLASS not in _nclass(d):
                    return n
        return ""

    def _find_session_row(self, name):
        for node in self._session_nodes():
            if self._session_row_label(node) == name:
                return node
        return None

    def _hover_session_list(self):
        pane = self._pane_by_name(SESSION_LIST_NAME)
        if pane is None:
            return None
        r = _nrect(pane)
        if r is None:
            return None
        x, y = (r.left + r.right) // 2, (r.top + r.bottom) // 2
        if _window_at(x, y) != self.hwnd:
            return None                 # never wheel over another application
        user32.SetCursorPos(x, y)
        time.sleep(0.1)
        return pane

    def scroll_sessions(self, direction, times=3):
        """Scroll the session list one notch; QQ exposes no ScrollPattern.

        The wheel works on whatever sits under the cursor, so the pointer is
        parked over the list first. Returns False when the list is missing.
        """
        if self._hover_session_list() is None:
            return False
        if direction > 0:
            auto.WheelDown(wheelTimes=times, interval=0.05, waitTime=0.15)
        else:
            auto.WheelUp(wheelTimes=times, interval=0.05, waitTime=0.15)
        return True

    def _click_and_confirm(self, node, want):
        r = _nrect(node)
        if r is None:
            return False
        self.click_node(node, "session row %r" % want)
        for _ in range(16):
            time.sleep(0.2)
            if self.current_chat() == want:
                return True
        return False

    def open_chat(self, name, verify=None, attempts=2, scroll_steps=6):
        """Switch QQ to the conversation called `name`.

        The session list is the only route: the row is clicked where it is
        visible, and the list is wheeled when it is not. The chat header is
        checked afterwards, so a mis-click is never taken for a success.
        """
        if not name:
            return True
        want = verify or name
        if self.current_chat() == want:
            return True
        for _ in range(max(1, attempts)):
            self.force_foreground()
            row = self._find_session_row(name)
            if row is not None and self._click_and_confirm(row, want):
                return True
            for step in range(scroll_steps):
                if not self.scroll_sessions(1):
                    break
                row = self._find_session_row(name)
                if row is not None and self._click_and_confirm(row, want):
                    return True
            # Maybe it sits above the fold instead of below it.
            for step in range(scroll_steps * 2):
                if not self.scroll_sessions(-1):
                    break
                row = self._find_session_row(name)
                if row is not None and self._click_and_confirm(row, want):
                    return True
            if self.current_chat() == want:
                return True
            time.sleep(0.3)
        return self.current_chat() == want
    # ---- message parsing ----

    def _parse_message(self, item):
        """Split one message bubble into sender / text / images.

        QQ renders the level chip and the sender name inside the bubble too
        ("LV8", "kadami", the nickname), so text is only taken from the
        message-content wrapper; otherwise every group message would be
        prefixed with its author's level badge.
        """
        sender = ""
        is_self = False
        wrapper = None
        for node in self._walk(item):
            cls = _nclass(node)
            if "message-container--self" in cls:
                is_self = True
            elif cls == "avatar-span":
                if not sender:
                    sender = _nname(node)
            elif wrapper is None and "message-content__wrapper" in cls:
                wrapper = node
        texts = []
        images = 0
        for node in self._walk(wrapper if wrapper is not None else item):
            cls = _nclass(node)
            if wrapper is None and ("user-name" in cls or "group-member-label" in cls):
                continue
            t = _ntype(node)
            if t == "TextControl":
                s = _nname(node)
                if s and not TS_RE.match(s):
                    texts.append(s)
            elif t == "ImageControl" and IMAGE_TAG in _nname(node):
                images += 1
        text = "".join(texts).strip()
        if images:
            text = (text + " [" + IMAGE_TAG + "]") if text else ("[" + IMAGE_TAG + "]")
        if not sender and not text:
            return None
        runtime_id = _safe(lambda: item.GetRuntimeId(), None)
        if runtime_id is not None:
            try:
                runtime_id = [int(part) for part in runtime_id]
            except Exception:
                runtime_id = str(runtime_id)
        return {
            "sender": sender,
            "text": text,
            "is_self": is_self,
            "native_id": runtime_id,
        }
    def read_messages(self):
        """Return the visible messages of the open chat, oldest first."""
        ml = self._msg_list()
        if ml is None:
            return None
        msgs = []
        for node in self._walk(ml):
            if _nclass(node) == "message":
                parsed = self._parse_message(node)
                if parsed:
                    msgs.append(parsed)
        return msgs

    # ---- sending ----

    def _editor(self):
        for node in self._walk(self._fresh_root()):
            if EDITOR_CLASS in _nclass(node):
                return node
        for node in self._walk(self._fresh_root()):
            if "ProseMirror" in _nclass(node):
                return node
        return None

    def editor_empty(self):
        """True/False while the input box can be seen, None when it cannot."""
        ed = self._editor()
        if ed is None:
            return None
        return EMPTY_EDITOR_CLASS in _nclass(ed)

    def _send_button(self):
        for node in self._walk(self._fresh_root()):
            if _ntype(node) == "ButtonControl" and SEND_BUTTON_CLASS in _nclass(node):
                return node
        return None

    def send_text(self, text, mode="enter", verify_sent=True, timeout=4.0):
        """Type `text` into the open conversation and actually send it.

        With verify_sent the call only returns once the input box is empty
        again, so a swallowed Enter is reported instead of silently losing the
        message (or worse, leaving it to be sent into the next conversation).
        """
        if not text:
            return False
        if not self.force_foreground():
            raise RuntimeError("could not bring QQ to foreground; send aborted")
        editor = self._editor()
        if editor is None:
            raise RuntimeError("input editor not found")
        self.click_node(editor, "chat input")
        auto.SendKeys("{Ctrl}a", waitTime=0.05)
        time.sleep(0.1)
        pyperclip.copy(text)
        auto.SendKeys("{Ctrl}v", waitTime=0.05)

        # is-empty clears as soon as the pasted text lands; waiting for that
        # avoids racing QQ and sending an empty box.
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.editor_empty() is False:
                break
            time.sleep(0.1)

        if mode == "button":
            btn = self._send_button()
            if btn is None:
                raise RuntimeError("send button not found")
            for _ in range(10):
                if _safe(lambda: btn.IsEnabled):
                    break
                time.sleep(0.1)
            btn.Click(simulateMove=False)
        elif mode == "ctrl_enter":
            auto.SendKeys("{Ctrl}{Enter}")
        else:
            auto.SendKeys("{Enter}")

        if not verify_sent:
            time.sleep(0.2)
            return True

        def wait_cleared(seconds):
            end = time.time() + seconds
            while time.time() < end:
                if self.editor_empty() is True:
                    return True
                time.sleep(0.1)
            return False

        if wait_cleared(timeout):
            return True
        # Enter is occasionally swallowed (a stale key focus, mostly). The send
        # button is the same action through a different door, so try it once
        # before declaring the message lost.
        if mode != "button":
            btn = self._send_button()
            if btn is not None and _safe(lambda: btn.IsEnabled):
                try:
                    self.click_node(btn, "send button")
                except Exception:
                    pass
                if wait_cleared(timeout):
                    return True
        raise RuntimeError("message did not leave the input box")


    # ---- images ----

    def copy_image_to_clipboard(self, path):
        """Put an image file on the Windows clipboard.

        PowerShell runs the clipboard call on an STA thread (the bridge worker
        thread is not one) and needs no PIL/win32clipboard, which is the same
        route the WeChat bridge takes. CREATE_NO_WINDOW keeps the helper silent.
        """
        abs_path = os.path.abspath(path)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$img=[System.Drawing.Image]::FromFile('%s');"
            "[System.Windows.Forms.Clipboard]::SetImage($img);"
            "$img.Dispose()" % abs_path.replace("'", "''")
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden",
                            "-Command", script],
                           check=True, timeout=25, creationflags=0x08000000)
        except subprocess.TimeoutExpired:
            raise RuntimeError("copying %s to the clipboard timed out" % abs_path)
        except subprocess.CalledProcessError as e:
            raise RuntimeError("could not load %s onto the clipboard (unsupported "
                               "image format?)" % abs_path) from e

    def send_image(self, path, mode="enter", verify_sent=True, timeout=8.0):
        """Paste an image into the open conversation and send it.

        QQ attaches a pasted image to the editor, so the same is-empty flag
        that confirms a text send proves the image was attached and then left
        the input box.
        """
        if not path or not os.path.isfile(path):
            raise RuntimeError("image not found: %s" % path)
        if not self.force_foreground():
            raise RuntimeError("could not bring QQ to foreground; send aborted")
        self.copy_image_to_clipboard(path)
        time.sleep(0.25)
        editor = self._editor()
        if editor is None:
            raise RuntimeError("input editor not found")
        self.click_node(editor, "chat input")
        auto.SendKeys("{Ctrl}v", waitTime=0.05)

        deadline = time.time() + timeout
        attached = False
        while time.time() < deadline:
            if self.editor_empty() is False:
                attached = True
                break
            time.sleep(0.15)
        if not attached:
            raise RuntimeError("image did not land in the input box")

        if mode == "button":
            btn = self._send_button()
            if btn is None:
                raise RuntimeError("send button not found")
            btn.Click(simulateMove=False)
        else:
            auto.SendKeys("{Enter}")

        if not verify_sent:
            time.sleep(0.3)
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.editor_empty() is True:
                return True
            time.sleep(0.15)
        raise RuntimeError("image did not leave the input box")

def _cli():
    ap = argparse.ArgumentParser(description="QQ NT UI Automation controller")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("chats", help="list session names")
    p.add_argument("--chat", default="")

    p = sub.add_parser("sessions", help="list sessions with preview/unread")
    p.add_argument("--chat", default="")

    p = sub.add_parser("read", help="read messages of the open chat")
    p.add_argument("--chat", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("send", help="send a message")
    p.add_argument("--chat", default="")
    p.add_argument("--text", required=True)
    p.add_argument("--mode", default="enter", choices=["enter", "button", "ctrl_enter"])

    p = sub.add_parser("open", help="switch QQ to a conversation")
    p.add_argument("--chat", required=True)

    p = sub.add_parser("probe", help="dump the parsed messages of the open chat")
    p.add_argument("--chat", default="")

    args = ap.parse_args()
    qq = QQWindow().connect(args.chat or None)

    if args.cmd == "chats":
        print("\n".join(qq.list_sessions()))
    elif args.cmd == "sessions":
        print(json.dumps(qq.session_items(), ensure_ascii=False, indent=2))
    elif args.cmd == "read":
        if args.chat:
            qq.open_chat(args.chat)
        msgs = qq.read_messages()
        if args.json:
            print(json.dumps(msgs, ensure_ascii=False, indent=2))
        else:
            for m in msgs or []:
                who = "me" if m["is_self"] else m["sender"]
                print("%s: %s" % (who, m["text"]))
    elif args.cmd == "send":
        if args.chat:
            ok = qq.open_chat(args.chat)
            if not ok:
                raise SystemExit("could not switch to %r (now on %r)"
                                 % (args.chat, qq.current_chat()))
        qq.send_text(args.text, args.mode)
        print("sent")
    elif args.cmd == "open":
        ok = qq.open_chat(args.chat)
        print("opened" if ok else "failed", "->", qq.current_chat())
    elif args.cmd == "probe":
        if args.chat:
            qq.open_chat(args.chat)
        ml = qq._msg_list()
        if ml is None:
            print("message list not found")
            return
        for node in qq._walk(ml):
            if _nclass(node) == "message":
                print(json.dumps(qq._parse_message(node), ensure_ascii=False))


if __name__ == "__main__":
    _cli()
