# -*- coding: utf-8 -*-
"""
UIA -> OneBot v11 reverse-WebSocket bridge for AstrBot.

Instead of hooking / injecting into the QQ process (NapCat, Lagrange, SnowLuma),
this drives the real QQ desktop client through Windows UI Automation: it reads
the message list and types replies, exactly like a person would. AstrBot sees a
normal OneBot v11 client on its aiocqhttp reverse-WS endpoint, so every plugin,
persona and memory file keeps working unchanged.

Topology:
    QQ desktop client  <--UIA-->  this bridge  <--OneBot v11 WS-->  AstrBot :11229

Multi-session design (this is the part that matters):
  * Every outgoing message is addressed by one of the `chats` entries in
    bridge-config.json. Before typing, the bridge switches QQ to that
    conversation and *verifies the header* -- twice, once after the switch and
    once immediately before typing. If the header does not confirm, the send
    fails loudly instead of dropping the text into whatever chat was open.
  * Conversations are reached by clicking the row inside QQ's session list, and
    the list is scrolled with the wheel when the row is off screen. The chat
    header is read back afterwards, so a mis-click fails the send instead of
    reaching the wrong person.
  * read_mode=watch reads the session list (preview + unread badge) to find out
    which conversations have new traffic, switches into only those, and restores
    the conversation the user was looking at afterwards.
  * read_mode=active never switches while polling; read_mode=cycle walks every
    configured chat each poll.
  * Images AstrBot sends are pasted from the Windows clipboard into QQ's editor
    (base64:// segments are written to logs\\uia-outgoing first), then confirmed
    the same way text sends are.

Usage:
    python onebot_uia_bridge.py                       # bridge-config.json
    python onebot_uia_bridge.py --config X.json
    python onebot_uia_bridge.py --dry-run             # read only, never send
    python onebot_uia_bridge.py --selfcheck           # report config vs QQ, exit
"""

import argparse
import asyncio
import base64
import collections
import hashlib
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

import websockets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qq_uia  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "bridge-config.json")

AT_MARK = "Bot"  # fallback self nickname; self_nickname in the config wins

# --- mute gate ---------------------------------------------------------------
# Someone telling the bot to shut up (the bot's name plus a keyword below) parks the conversation for
# mute_seconds. The bot keeps READING that conversation - every message still
# goes to AstrBot, so memory and context stay intact - but the bridge refuses to
# send anything back, so no prompt and no @ can make it talk before the timer
# runs out. Set mute_swallow_inbound if you would rather it not even read.
# The keyword list lives in bridge-config.json as well; this is the fallback.
DEFAULT_MUTE_SECONDS = 1800
DEFAULT_MUTE_KEYWORDS = [
    "\u95ed\u5634", "\u9589\u5634", "\u522b\u8bf4\u4e86", "\u5225\u8aaa\u4e86", "\u522b\u8bf4\u8bdd", "\u522b\u8bf4\u8bdd\u4e86",
    "\u5c11\u8bf4\u8bdd", "\u5c11\u8bf4\u70b9", "\u5c11\u8bf4\u4e24\u53e5", "\u8bdd\u592a\u591a", "\u8a71\u592a\u591a", "\u8bdd\u771f\u591a",
    "\u8bdd\u597d\u591a", "\u8bdd\u6709\u70b9\u591a", "\u5e9f\u8bdd\u592a\u591a", "\u5c11\u5e9f\u8bdd", "\u522b\u5435", "\u5225\u5435",
    "\u5b89\u9759\u70b9", "\u5b89\u9759\u4e00\u70b9", "\u9759\u4e00\u9759", "shut up", "stfu", "be quiet",
]
# --- end mute gate -----------------------------------------------------------


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def now_ts():
    return int(time.time())


def chat_key(chat):
    return "%s:%s" % (chat["kind"], chat["id"])


def stable_uid(name):
    """Same nickname -> same synthetic uid across restarts."""
    return str(int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16) % 1000000000)


def message_to_text(msg):
    """Extract plain text from an OneBot message (string or segment array)."""
    if isinstance(msg, str):
        return msg
    parts = []
    for seg in msg or []:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            parts.append(d.get("text", ""))
        elif t == "at":
            parts.append("%s " % (d.get("name") or d.get("qq")))
        elif t in ("image", "face", "mface", "record", "video", "file"):
            continue  # not reproducible through the UI; never type a placeholder
    return "".join(parts).strip()


def parse_parts(msg):
    """OneBot message -> ordered [('text', str) | ('image', data-dict)].

    Images are kept in order so a mixed message still reads the way AstrBot
    built it; unsupported segment types are skipped rather than turned into
    placeholder text, which would be typed into the chat.
    """
    if isinstance(msg, str):
        return [("text", msg)] if msg else []
    out = []
    for seg in msg or []:
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            if d.get("text"):
                out.append(("text", d["text"]))
        elif t == "image":
            out.append(("image", d))
        elif t == "at":
            out.append(("text", "%s " % (d.get("name") or d.get("qq"))))
        elif t in ("face", "mface", "record", "video", "file"):
            continue
    return out


def describe_parts(parts):
    bits = []
    for kind, payload in parts:
        bits.append(payload[:40] if kind == "text" else "[image %s]" % os.path.basename(payload))
    return " + ".join(bits)

def sig_of(m):
    """Identity of a message for diffing.

    Only is_self + text: QQ fills the sender element in a beat after the rest
    of the bubble, so two reads of the same message can carry "" and the
    nickname respectively. With the sender in the key those reads look like
    different messages and AstrBot gets the message twice.
    """
    return (bool(m.get("is_self")), m.get("text", ""))


def diff_since(prev_sigs, cur):
    """(fresh messages, trusted).

    QQ keeps only the tail of a conversation on screen, so the snapshot slides
    between polls and the head of the new list is usually *not* a continuation
    of the old one. First try the positional overlap; when that lines up,
    everything past the overlap is genuinely new.

    When it does not line up the snapshot may even be half rendered, so only
    signatures that were absent from the previous snapshot are offered and the
    caller filters those against everything already reported. Without that, a
    partial re-render could hand AstrBot the same message twice and it would
    answer twice.
    """
    cur_sigs = [sig_of(m) for m in cur]
    if not prev_sigs:
        return list(cur), True
    for k in range(min(len(prev_sigs), len(cur_sigs)), 0, -1):
        if cur_sigs[:k] == list(prev_sigs)[-k:]:
            return cur[k:], True
    budget = collections.Counter(prev_sigs)
    fresh = []
    for m in cur:
        s = sig_of(m)
        if budget[s] > 0:
            budget[s] -= 1
        else:
            fresh.append(m)
    return fresh, False

class UiaBackend:
    """Every QQ client touch. Only ever called from the bridge worker thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.qq = None
        self.lock = threading.RLock()

    # ---- attach ----

    def expect_names(self):
        return [c["name"] for c in (self.cfg.get("chats") or []) if c.get("name")]

    def connect(self):
        """Attach to the QQ window that actually holds the watched chats.

        Several QQ accounts can be signed in at once and then more than one
        window is titled "QQ"; attaching to the wrong one makes every switch
        fail, so a window can be pinned by the account nickname (set from the
        console UI) and otherwise the candidates are scored by their session
        lists.
        """
        account = str(self.cfg.get("window_account") or "").strip()
        self.qq = qq_uia.QQWindow().connect(expect=self.expect_names(), account=account or None)
        log("QQ attached: account=%r window=%s renderer=%s" % (
            account or self.qq.account_nickname(),
            hex(self.qq.hwnd),
            hex(self.qq.renderer_hwnd) if self.qq.renderer_hwnd else None))

    def reselect(self, reason):
        """Re-pick the QQ window after a failure that smells like a wrong one."""
        try:
            self.qq = None
            self.connect()
            log("re-attached to QQ after %s (now on %r)" % (reason, self.qq.current_chat()))
            return True
        except Exception as e:
            log("re-attach after %s failed: %s" % (reason, e))
            return False

    def _ensure_root(self):
        if self.qq is None or not self.qq.alive():
            self.connect()
            return
        try:
            self.qq._fresh_root()
        except Exception:
            self.connect()

    # ---- reads ----

    def current_chat(self):
        with self.lock:
            self._ensure_root()
            return self.qq.current_chat()

    def session_items(self):
        with self.lock:
            self._ensure_root()
            return self.qq.session_items()

    # ---- switching ----

    def switch(self, chat):
        """Make QQ show `chat`. True only when the header confirms it."""
        with self.lock:
            self._ensure_root()
            name = chat["name"]
            if self.qq.current_chat() == name:
                return True
            settle = float(self.cfg.get("switch_settle", 0.9))

            def swing():
                return self.qq.open_chat(name, verify=chat.get("verify") or name)

            ok = swing()
            if not ok:
                log("switch retry for %r (QQ is on %r)" % (name, self.qq.current_chat()))
                # A window we cannot reach is usually the wrong QQ account.
                self.reselect("a failed switch")
                time.sleep(settle)
                ok = swing()
            time.sleep(settle)
            got = self.qq.current_chat()
            if got != name:
                log("SWITCH FAILED: wanted %r, QQ shows %r" % (name, got))
                return False
            return True

    def open_chat_by_name(self, name):
        """Used to put back the conversation the user was looking at."""
        return self.switch({"name": name})

    # ---- read + send ----

    def read_chat(self, chat):
        """Messages of `chat`, or None when it could not be read."""
        with self.lock:
            self._ensure_root()
            if self.qq.current_chat() != chat["name"]:
                if self.cfg.get("read_mode", "watch") == "active":
                    return None       # never move the mouse while polling
                if not self.switch(chat):
                    return None
            return self.qq.read_messages()

    def send_parts(self, chat, parts):
        """Send text/image parts in order into `chat` and nowhere else.

        One switch serves the whole message, but the header is re-checked
        before every part so a mid-sequence jump cannot misdeliver anything.
        """
        with self.lock:
            self._ensure_root()
            if not self.switch(chat):
                raise RuntimeError("could not switch QQ to %r" % chat["name"])
            mode = self.cfg.get("send_mode", "enter")
            for kind, payload in parts:
                if self.qq.current_chat() != chat["name"]:
                    raise RuntimeError("conversation changed mid-send")
                if kind == "text":
                    self.qq.send_text(payload, mode)
                else:
                    self.qq.send_image(payload, mode)
            if self.qq.current_chat() != chat["name"]:
                raise RuntimeError("conversation changed while sending")
    def send_text(self, chat, text):
        """Send into `chat` and nowhere else.

        The switch is verified twice so a lost race can never drop the text
        into whichever conversation happened to be open.
        """
        with self.lock:
            self._ensure_root()
            if not self.switch(chat):
                raise RuntimeError("could not switch QQ to %r" % chat["name"])
            if self.qq.current_chat() != chat["name"]:
                raise RuntimeError("conversation changed right before sending")
            self.qq.send_text(text, self.cfg.get("send_mode", "enter"))
            if self.qq.current_chat() != chat["name"]:
                raise RuntimeError("conversation changed while sending")


class Bridge:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run or bool(cfg.get("dry_run"))
        self.self_id = str(cfg.get("self_id", ""))
        self.nick = cfg.get("self_nickname", "") or AT_MARK
        self.emit_self = bool(cfg.get("emit_self", False))
        self.chats = list(cfg.get("chats") or [])
        self.by_key = {chat_key(c): c for c in self.chats}

        self.global_members = {str(k): v for k, v in (cfg.get("members") or {}).items()}
        self.nick_to_uid = {}
        self.uid_to_nick = dict(self.global_members)
        for c in self.chats:
            m = dict(self.global_members)
            m.update({str(k): v for k, v in (c.get("members") or {}).items()})
            self.nick_to_uid[chat_key(c)] = {name: qq for qq, name in m.items()}
            self.uid_to_nick.update(m)
        self.global_nick_to_uid = {}
        for c in self.chats:
            self.global_nick_to_uid.update(self.nick_to_uid[chat_key(c)])

        self.loop = None
        self.ws = None
        self.msg_seq = 0
        self.out_seq = 0
        self.poll_count = 0   # polls since start, drives the sweep
        self.sweep_every = int(cfg.get("sweep_every", 10) or 0)
        self.sweep_idle = float(cfg.get("sweep_idle_seconds", 60) or 0)
        self.last_read_ts = {}
        self.markers = {}     # chat key -> (preview, time, unread>0)
        self.history = {}     # chat key -> [signature, ...]
        self.reported = {}    # chat key -> Counter of signatures already pushed
        self.reported_order = {}   # chat key -> FIFO used to cap that Counter
        self.recent_text = {}      # chat key -> deque of (text, when) pushed
        self.deferred = {}    # chat key -> chat held back during a send burst
        self.last_send_ts = 0.0
        self.read_once = set()     # chat keys already read once in this process
        state_path = str(cfg.get("state_path") or "")
        if not state_path:
            state_path = os.path.join(os.path.dirname(HERE), "logs", "bridge-seen.json")
        elif not os.path.isabs(state_path):
            state_path = os.path.join(os.path.dirname(HERE), state_path)
        self.state_path = state_path
        self.backend = UiaBackend(cfg)
        self.load_state()

        # ---------- mute gate state ----------
        try:
            self.mute_seconds = int(cfg.get("mute_seconds", DEFAULT_MUTE_SECONDS) or DEFAULT_MUTE_SECONDS)
        except (TypeError, ValueError):
            self.mute_seconds = DEFAULT_MUTE_SECONDS
        if self.mute_seconds <= 0:
            self.mute_seconds = DEFAULT_MUTE_SECONDS
        kws = cfg.get("mute_keywords")
        if not isinstance(kws, (list, tuple)):
            kws = DEFAULT_MUTE_KEYWORDS
        self.mute_keywords = [str(k).strip() for k in kws if str(k).strip()]
        self.mute_require_nick = bool(cfg.get("mute_require_nickname_in_group", True))
        self.mute_swallow_inbound = bool(cfg.get("mute_swallow_inbound", False))
        names = set()
        for extra in (cfg.get("self_nickname"), cfg.get("window_account")):
            if extra:
                names.add(str(extra).strip())
        names.add(str(self.nick).strip())
        extra_names = cfg.get("mute_trigger_names")
        if isinstance(extra_names, (list, tuple)):
            names.update(str(x).strip() for x in extra_names)
        self.mute_names = {n for n in names if n}
        self.muted_until = {}
        self.mute_log_ts = {}

        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._worker, name="uia", daemon=True)
        self._check_config()

    # ---------- config sanity ----------

    def _check_config(self):
        seen = {}
        for c in self.chats:
            key = chat_key(c)
            if key in seen:
                log("WARN: duplicate chat entry %s" % key)
            seen[key] = c
        by_name = {}
        for c in self.chats:
            by_name.setdefault(c["name"], []).append(c)
        for name, group in by_name.items():
            if len(group) > 1:
                keys = ", ".join(chat_key(c) for c in group)
                log("WARN: %r is configured more than once (%s); switching by the "
                    "session list cannot tell two rows with the same name apart"
                    % (name, keys))
        missing = [c["name"] for c in self.chats if not c.get("name")]
        if missing:
            log("WARN: %d chat entries have no name" % len(missing))

    def is_ambiguous(self, chat):
        """Two config entries with the same display name would share one row."""
        return len([c for c in self.chats if c["name"] == chat["name"]]) > 1

    # ---------- ids ----------

    def next_msg_id(self):
        self.msg_seq += 1
        return 10_000_000 + self.msg_seq

    def uid_for(self, chat, nickname):
        table = self.nick_to_uid.get(chat_key(chat), {})
        if nickname in table:
            return table[nickname]
        if nickname in self.global_nick_to_uid:
            return self.global_nick_to_uid[nickname]
        return stable_uid(nickname)

    def name_for(self, uid):
        return self.uid_to_nick.get(str(uid), str(uid))

    def members_of(self, chat):
        m = dict(self.global_members)
        m.update({str(k): v for k, v in (chat.get("members") or {}).items()})
        return m

    # ---------- outgoing images ----------

    @staticmethod
    def _sniff_ext(blob):
        if blob[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if blob[:6] in (b"GIF87a", b"GIF89a"):
            return ".gif"
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            return ".webp"
        if blob[:2] == b"BM":
            return ".bmp"
        return ".png"

    def _save_outgoing(self, blob, ext):
        out_dir = self.cfg.get("outbox_dir") or os.path.join(
            os.path.dirname(HERE), "logs", "uia-outgoing")
        os.makedirs(out_dir, exist_ok=True)
        self.out_seq += 1
        path = os.path.join(out_dir, "img_%d_%d%s" % (now_ts(), self.out_seq, ext))
        with open(path, "wb") as f:
            f.write(blob)
        return path

    def prune_outbox(self):
        """Drop outgoing images older than a day; AstrBot sends one per reply."""
        out_dir = self.cfg.get("outbox_dir") or os.path.join(
            os.path.dirname(HERE), "logs", "uia-outgoing")
        if not os.path.isdir(out_dir):
            return
        cutoff = time.time() - 24 * 3600
        for name in os.listdir(out_dir):
            path = os.path.join(out_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    def resolve_image(self, data):
        """OneBot image segment -> local file path, or None.

        AstrBot's aiocqhttp adapter always sends images as
        {"type": "image", "data": {"file": "base64://..."}}, but file:// URIs,
        plain paths and http(s) URLs are accepted too.
        """
        raw = str((data or {}).get("file") or (data or {}).get("url") or "").strip()
        if not raw:
            return None
        try:
            if raw.startswith("base64://"):
                blob = base64.b64decode(raw[len("base64://"):])
                return self._save_outgoing(blob, self._sniff_ext(blob))
            if raw.startswith("data:image/"):
                head, _, body = raw.partition(",")
                head = head.lower()
                ext = ".jpg" if ("jpeg" in head or "jpg" in head) else (
                    ".gif" if "gif" in head else (".webp" if "webp" in head else ".png"))
                return self._save_outgoing(base64.b64decode(body), ext)
            if raw.startswith("file://"):
                path = raw[7:]
                if re.match(r"^/[A-Za-z]:", path):
                    path = path[1:]
                path = urllib.parse.unquote(path)
                return path if os.path.isfile(path) else None
            if re.match(r"^https?://", raw):
                out_dir = self.cfg.get("outbox_dir") or os.path.join(
                    os.path.dirname(HERE), "logs", "uia-outgoing")
                os.makedirs(out_dir, exist_ok=True)
                with urllib.request.urlopen(raw, timeout=20) as resp:
                    blob = resp.read()
                return self._save_outgoing(blob, self._sniff_ext(blob))
            if os.path.isfile(raw):
                return raw
            # Bare file name: look it up in the configured attachment folders.
            for root in (self.cfg.get("image_search_dirs") or []):
                cand = os.path.join(root, raw)
                if os.path.isfile(cand):
                    return cand
        except Exception as e:
            log("WARN: could not resolve image %r: %s" % (raw[:60], e))
        return None
    # ---------- event builders ----------

    def build_event(self, chat, m):
        gid = chat["id"] if chat["kind"] == "group" else None
        if m["is_self"]:
            uid = self.self_id
            nickname = self.nick
        elif chat["kind"] == "private":
            uid = chat["id"]
            nickname = chat["name"]
        else:
            nickname = m["sender"] or "unknown"
            uid = self.uid_for(chat, nickname)
        text = m["text"]
        segments = []
        if chat["kind"] == "group" and not m["is_self"] and text:
            if re.search(r"@\s*" + re.escape(self.nick), text):
                segments.append({"type": "at", "data": {"qq": self.self_id, "name": self.nick}})
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        base = {
            "post_type": "message",
            "time": now_ts(),
            "self_id": int(self.self_id) if self.self_id.isdigit() else 0,
            "message_id": self.next_msg_id(),
            "message": segments,
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": int(uid) if str(uid).isdigit() else 0,
                "nickname": nickname,
                "card": nickname,
                "role": "member",
            },
        }
        if chat["kind"] == "group":
            base.update({
                "message_type": "group",
                "sub_type": "normal",
                "group_id": int(gid) if str(gid).isdigit() else 0,
                "group_name": chat["name"],
            })
        else:
            base.update({
                "message_type": "private",
                "sub_type": "friend",
                "user_id": int(uid) if str(uid).isdigit() else 0,
            })
        return base

    # ---------- GUI worker ----------

    @staticmethod
    def idle_seconds():
        """Seconds since the last keyboard or mouse input anywhere on the box."""
        try:
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            now = ctypes.windll.kernel32.GetTickCount()
            return max(0.0, ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0)
        except Exception:
            return 0.0

    @staticmethod
    def hold_awake():
        """Ask Windows not to sleep while the bridge is up.

        Sleep takes the QQ window down with it and the bot goes quiet until
        somebody touches the keyboard, which is half of why an untouched
        machine stops answering. This also survives display-off: the renderer
        keeps producing the accessibility tree they are read from.
        """
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            res = ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            log("keep-awake: system sleep blocked (ok=%s)" % bool(res))
        except Exception as e:
            log("keep-awake failed: %s" % e)

    def start(self):
        self.worker.start()

    def _worker(self):
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            self._worker_body()

    def _worker_body(self):
        if bool(self.cfg.get("keep_awake", True)):
            self.hold_awake()
        try:
            self.backend.connect()
            self.prune_outbox()
            self.selfcheck()
        except Exception as e:
            log("FATAL: cannot attach to QQ: %s" % e)
            os._exit(2)
        interval = float(self.cfg.get("poll_interval", 3.0))
        last_poll = 0.0
        while True:
            try:
                fn, fut = self.jobs.get(timeout=0.15)
                try:
                    result = fn()
                    if fut is not None:
                        self.loop.call_soon_threadsafe(fut.set_result, result)
                except Exception as e:
                    if fut is not None:
                        self.loop.call_soon_threadsafe(fut.set_exception, e)
            except queue.Empty:
                pass
            if time.time() - last_poll >= interval and self.jobs.empty():
                last_poll = time.time()
                try:
                    self.poll_once()
                except Exception as e:
                    log("poll error: %s" % e)

    def submit(self, fn):
        """Run fn on the GUI thread and await its result."""
        fut = self.loop.create_future()
        self.jobs.put((fn, fut))
        return fut

    # ---------- selfcheck ----------

    def selfcheck(self):
        items = self.backend.session_items()
        names = {i["name"] for i in items}
        log("QQ sessions: %s" % ", ".join(sorted(names)))
        for c in self.chats:
            here = c["name"] in names
            log("  configured %-28s [%s] %s" % (
                c["name"], chat_key(c), "present" if here else "NOT in session list"))
        log("current conversation: %r" % self.backend.current_chat())

    # ---------- polling ----------

    def poll_once(self):
        if self.cfg.get("read_mode", "watch") == "watch":
            self.poll_watch()
            return
        for chat in self.chats:
            self.collect(chat)

    def _changed_chats(self):
        """In watch mode: configured chats whose session row moved.

        The session list is virtualised, so a configured chat can be missing
        from the snapshot just because it scrolled out of view. Those are
        picked up by a slow sweep instead of being ignored, otherwise messages
        in them would never reach AstrBot.
        """
        until = {}
        for it in self.backend.session_items():
            until.setdefault(it["name"], it)
        self.poll_count += 1
        sweep = bool(self.sweep_every) and (self.poll_count % self.sweep_every == 0)
        force = max(0.0, float(self.cfg.get("force_read_seconds", 300) or 0))
        idle_needed = max(0.0, float(self.cfg.get("force_read_idle_seconds", 60) or 0))
        out = []
        for chat in self.chats:
            key = chat_key(chat)
            info = until.get(chat["name"])
            if info is None:
                idle = time.time() - self.last_read_ts.get(key, 0.0)
                if key not in self.history or (sweep and idle >= self.sweep_idle):
                    out.append(chat)
                    log("watch: %s is not in the visible session list, reading it anyway" % key)
                continue
            marker = (info["preview"], info["time"], info["unread"] > 0)
            prev = self.markers.get(key)
            self.markers[key] = marker
            if prev is not None and prev == marker:
                # Chromium stops repainting a window that is minimised or fully
                # covered, and QQ's session row then freezes: the preview and
                # the unread badge stop moving, so change detection alone waits
                # forever. Open the chat and read it directly every
                # force_read_seconds so an idle machine cannot swallow a night.
                # last_read_ts only tracks this process, so a chat nobody has
                # read yet says "not read yet" rather than pretending the epoch
                # is a read time.
                age = None
                if key in self.last_read_ts:
                    age = time.time() - self.last_read_ts[key]
                # Only while nobody is at the keyboard: opening a chat raises
                # QQ and clicks, which would interrupt somebody who is typing.
                # The idle case is the one that silently loses a whole night.
                if force and (age is None or age >= force) and self.idle_seconds() >= idle_needed:
                    out.append(chat)
                    log("stale sweep: %s %s (machine idle %ds), reading it anyway"
                        % (key, ("unread for %ds" % int(age)) if age is not None
                           else "not read since this bridge started", int(self.idle_seconds())))
                continue
            out.append(chat)
        return out
    def _in_send_hold(self):
        """True while AstrBot is still delivering a reply burst.

        AstrBot splits a reply into several messages with random pauses between
        them. Hopping to another conversation to poll in the middle of that
        burst makes QQ visibly thrash, so read-driven switching waits for the
        burst to finish and the pending conversations are read afterwards.
        """
        hold = float(self.cfg.get("send_hold_seconds", 12) or 0)
        return hold > 0 and (time.time() - self.last_send_ts) < hold

    def poll_watch(self):
        changed = self._changed_chats()
        if self._in_send_hold():
            for chat in changed:
                self.deferred[chat_key(chat)] = chat
            if changed:
                log("send hold: keeping QQ on %r, %d conversation(s) queued for later"
                    % (self.backend.current_chat(), len(self.deferred)))
            return
        todo = list(self.deferred.values())
        self.deferred.clear()
        queued = {chat_key(c) for c in todo}
        todo.extend(c for c in changed if chat_key(c) not in queued)

        origin = self.backend.current_chat()
        touched = False
        for chat in todo:
            try:
                self.collect(chat)
                touched = True
            except Exception as e:
                log("read error for %s: %s" % (chat["name"], e))
        if touched and origin and self.cfg.get("restore_after_read", True):
            if self.backend.current_chat() != origin:
                try:
                    self.backend.open_chat_by_name(origin)
                except Exception as e:
                    log("could not restore %r: %s" % (origin, e))
    @staticmethod
    def _looks_partial(prev_sigs, msgs):
        """A snapshot far smaller than the last one is a half-drawn list."""
        if not msgs:
            return True
        return len(prev_sigs) >= 4 and len(msgs) <= len(prev_sigs) // 2

    def collect(self, chat):
        """Read one conversation and push whatever is new to AstrBot."""
        msgs = self.backend.read_chat(chat)
        if msgs is None:
            return
        key = chat_key(chat)
        first_read = key not in self.read_once
        prev = self.history.get(key)

        if prev is None:
            log("baseline %s: %d messages" % (key, len(msgs)))
            self._remember(key, msgs)
            self.history[key] = [sig_of(m) for m in msgs][-400:]
            self.read_once.add(key)
            self.save_state()
            return

        if self._looks_partial(prev, msgs):
            # Chromium hands back a partially drawn list now and then. Writing
            # that into history would make the next full snapshot look like a
            # flood of new messages, so the old history is kept instead.
            log("ignoring a short snapshot for %s (%d messages, had %d)"
                % (key, len(msgs), len(prev)))
            return

        self.last_read_ts[key] = time.time()
        fresh, trusted = diff_since(prev, msgs)
        if trusted:
            self.history[key] = [sig_of(m) for m in msgs][-400:]
        elif first_read:
            # The first snapshot after a restart does not line up with what the
            # previous process already reported (QQ redraws or slides the list).
            # Replying to it would answer messages that were handled minutes
            # ago, so this read only re-seeds the comparison state.
            log("re-baseline %s without replying (first read, %d messages, %d known)"
                % (key, len(msgs), len(prev)))
            self._remember(key, msgs)
            self.history[key] = [sig_of(m) for m in msgs][-400:]
            self.read_once.add(key)
            self.save_state()
            return
        else:
            already = self.reported.get(key) or collections.Counter()
            window = float(self.cfg.get("dedupe_window_seconds", 180) or 0)
            now = time.time()
            recent = self.recent_text.setdefault(key, collections.deque(maxlen=300))
            kept = []
            for m in fresh:
                text = m["text"]
                seen_now = any(t == text and now - ts < window for t, ts in recent)
                if already[sig_of(m)] > 0 or seen_now:
                    log("skip %s: %r looks like a re-render, not a new message"
                        % (key, text[:40]))
                    continue
                kept.append(m)
            log("untrusted snapshot for %s: %d of %d messages looked new"
                % (key, len(kept), len(msgs)))
            fresh = kept

        for m in fresh:
            self._mark_reported(key, m)
            if m["is_self"] and not self.emit_self:
                continue
            text = m.get("text", "")
            # Reading is never blocked: the bot hears everything even while its
            # gate is down, so it can pick the thread back up when the timer ends.
            self.mute_scan(key, chat, text)
            if self.mute_hide_inbound(key, text):
                continue
            self.emit(self.build_event(chat, m))
        self.read_once.add(key)
        self.save_state()

    def _mark_reported(self, key, m):
        counter = self.reported.setdefault(key, collections.Counter())
        order = self.reported_order.setdefault(key, collections.deque())
        recent = self.recent_text.setdefault(key, collections.deque(maxlen=300))
        sig = sig_of(m)
        counter[sig] += 1
        order.append(sig)
        recent.append((m.get("text", ""), time.time()))
        while len(order) > 500:
            old = order.popleft()
            if counter[old] > 0:
                counter[old] -= 1
                if counter[old] == 0:
                    del counter[old]

    def _remember(self, key, msgs):
        for m in msgs:
            self._mark_reported(key, m)

    # ---------- dedupe state that survives a restart ----------

    def load_state(self):
        """Reload what the previous process already reported.

        The dedupe structures above live in memory. Without this, a restart
        starts from an empty set, and the first snapshot can look like a burst
        of new messages even though they were answered before the restart -
        which made the model answer the same message twice.
        """
        path = getattr(self, "state_path", "")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except Exception as e:
            log("could not read dedupe state %s: %s" % (path, e))
            return
        window = max(float(self.cfg.get("dedupe_window_seconds", 180) or 0), 60.0)
        now = time.time()
        loaded = 0
        for key, item in (blob.get("chats") or {}).items():
            if not isinstance(item, dict):
                continue
            try:
                hist = [(bool(s[0]), str(s[1])) for s in (item.get("history") or [])]
                if hist:
                    self.history[key] = hist[-400:]
                counter = collections.Counter()
                for entry in (item.get("reported") or []):
                    counter[(bool(entry[0]), str(entry[1]))] += int(entry[2])
                if counter:
                    self.reported[key] = counter
                recent = collections.deque(maxlen=300)
                for text, ts in (item.get("recent") or []):
                    if now - float(ts) < window:
                        recent.append((str(text), float(ts)))
                if recent:
                    self.recent_text[key] = recent
                loaded += 1
            except Exception:
                continue
        if loaded:
            log("dedupe state restored for %d chat(s)" % loaded)

    def save_state(self):
        """Write the dedupe structures so the next start inherits them."""
        path = getattr(self, "state_path", "")
        if not path:
            return
        window = max(float(self.cfg.get("dedupe_window_seconds", 180) or 0), 60.0)
        now = time.time()
        chats = {}
        for key, hist in self.history.items():
            counter = self.reported.get(key) or collections.Counter()
            recent = self.recent_text.get(key) or collections.deque()
            chats[key] = {
                "history": [[bool(s[0]), s[1]] for s in hist[-400:]],
                "reported": [[bool(s[0]), s[1], int(n)] for s, n in counter.items()],
                "recent": [[t, ts] for t, ts in recent if now - ts < window],
            }
        tmp = path + ".tmp"
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "chats": chats}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            log("could not write dedupe state: %s" % e)

    def emit(self, event):
        text = event.get("raw_message", "")
        log("EVENT %s from %s: %s" % (
            event.get("message_type"), event["sender"]["nickname"], text[:60]))
        if self.ws is None:
            return
        payload = json.dumps(event, ensure_ascii=False)
        asyncio.run_coroutine_threadsafe(self.ws.send(payload), self.loop)

    # ---------- mute gate ----------

    def mute_hit(self, text):
        """The keyword that asked the bot to stop talking, or None."""
        blob = str(text or "")
        if not blob.strip():
            return None
        low = blob.lower()
        for kw in self.mute_keywords:
            if kw.lower() in low:
                return kw
        return None

    def mute_armed_here(self, chat, text):
        """In a group only a message aimed at the bot may arm the gate.

        Friends say "shut up" to each other all day; without this a stray line
        aimed at somebody else would silence the bot for half an hour.
        """
        if chat["kind"] != "group" or not self.mute_require_nick:
            return True
        blob = str(text or "")
        if re.search(r"@\s*" + re.escape(self.nick), blob):
            return True
        return any(name in blob for name in self.mute_names)

    def mute_minutes(self):
        return max(1, int(round(self.mute_seconds / 60.0)))

    def mute_remaining(self, key):
        return max(0, int(round(self.muted_until.get(key, 0.0) - time.time())))

    def is_muted(self, key):
        until = self.muted_until.get(key)
        if until is None:
            return False
        if until <= time.time():
            self.muted_until.pop(key, None)
            self.mute_log_ts.pop(key, None)
            log("MUTE OFF %s: the %d min timer ran out" % (key, self.mute_minutes()))
            return False
        return True

    def mute_chat(self, key, name, keyword):
        rearmed = key in self.muted_until
        self.muted_until[key] = time.time() + self.mute_seconds
        log("MUTE ON  %s (%s) for %d min - heard %r%s"
            % (key, name, self.mute_minutes(), keyword,
               " (timer restarted)" if rearmed else ""))

    def mute_scan(self, key, chat, text):
        """Arm (or re-arm) the gate when this message tells the bot to shut up."""
        keyword = self.mute_hit(text)
        if keyword and self.mute_armed_here(chat, text):
            self.mute_chat(key, chat["name"], keyword)
            return True
        return False

    def mute_hide_inbound(self, key, text):
        """True when this message should be kept from AstrBot as well.

        Off by default, because the gate is about the bot's mouth, not its ears:
        the conversation keeps flowing into AstrBot so memory and context stay
        whole, and only the reply is stopped. Turn mute_swallow_inbound on if a
        conversation should go fully dark instead.
        """
        if not self.mute_swallow_inbound:
            return False
        if not self.is_muted(key):
            return False
        now = time.time()
        if now - self.mute_log_ts.get(key, 0.0) > 60:
            self.mute_log_ts[key] = now
            log("MUTE HIDE %s: swallowing %r (%ds left)"
                % (key, str(text)[:40], self.mute_remaining(key)))
        return True

    # ---------- action handling ----------

    async def handle_action(self, action, params):
        if action in ("send_msg", "send_group_msg", "send_private_msg"):
            return await self.do_send(action, params)
        if action == "get_login_info":
            return self.ok({"user_id": int(self.self_id) if self.self_id.isdigit() else 0,
                            "nickname": self.nick})
        if action in ("get_stranger_info", "get_group_member_info"):
            uid = str(params.get("user_id") or params.get("qq") or "")
            chat = self.by_key.get("group:" + str(params.get("group_id"))) \
                if params.get("group_id") else None
            name = self.members_of(chat).get(uid) if chat else None
            name = name or self.name_for(uid)
            return self.ok({"user_id": int(uid) if uid.isdigit() else 0,
                            "nickname": name, "card": name,
                            "nick": name, "sex": "unknown", "age": 0})
        if action == "get_group_info":
            gid = str(params.get("group_id", ""))
            c = self.by_key.get("group:" + gid)
            return self.ok({"group_id": int(gid) if gid.isdigit() else 0,
                            "group_name": c["name"] if c else "", "member_count": 0,
                            "max_member_count": 0})
        if action == "get_group_list":
            data = []
            for c in self.chats:
                if c["kind"] == "group":
                    data.append({"group_id": int(c["id"]), "group_name": c["name"],
                                 "member_count": 0, "max_member_count": 0})
            return self.ok(data)
        if action == "get_friend_list":
            data = []
            for c in self.chats:
                if c["kind"] == "private":
                    data.append({"user_id": int(c["id"]), "nickname": c["name"],
                                 "remark": c["name"]})
            return self.ok(data)
        if action == "get_group_member_list":
            gid = str(params.get("group_id", ""))
            chat = self.by_key.get("group:" + gid)
            data = []
            if chat is not None:
                for uid, name in self.members_of(chat).items():
                    data.append({"user_id": int(uid) if uid.isdigit() else 0,
                                 "nickname": name, "card": name, "role": "member"})
            return self.ok(data)
        if action in ("get_version_info", "get_status"):
            return self.ok({"app_name": "uia-bridge", "app_version": "0.2",
                            "protocol_version": "v11", "good": True, "online": True})
        if action in ("can_send_image", "can_send_record", "send_like",
                      "set_msg_emoji_like", "delete_msg", "set_group_ban",
                      "set_group_whole_ban"):
            return self.ok({})
        return self.ok({})

    @staticmethod
    def ok(data):
        return {"status": "ok", "retcode": 0, "data": data}

    @staticmethod
    def failed(reason=""):
        if reason:
            log("SEND REFUSED: %s" % reason)
        return {"status": "failed", "retcode": 100, "data": None}

    async def do_send(self, action, params):
        if action == "send_msg":
            if params.get("message_type") == "group" or params.get("group_id"):
                action = "send_group_msg"
            else:
                action = "send_private_msg"
        if action == "send_group_msg":
            key = "group:" + str(params.get("group_id"))
        else:
            key = "private:" + str(params.get("user_id"))
        chat = self.by_key.get(key)
        if chat is None:
            return self.failed("no chat configured for %s (text=%r)"
                               % (key, message_to_text(params.get("message"))[:40]))
        if self.is_ambiguous(chat):
            return self.failed("%r is configured twice, so the session list cannot "
                               "tell the two rows apart (key %s)" % (chat["name"], key))

        if self.is_muted(key):
            # Answer "ok" on purpose: a failed retcode makes AstrBot retry, and
            # staying quiet is the whole point of the gate.
            log("MUTE BLOCK %s: dropping %r (%ds left)"
                % (key, message_to_text(params.get("message"))[:40], self.mute_remaining(key)))
            return self.ok({"message_id": self.next_msg_id()})

        for seg in (params.get("message") or []):
            if isinstance(seg, dict) and seg.get("type") in ("record", "video", "file"):
                log("WARN: %s to %s cannot be sent through the UI (only text and images "
                    "can); dropping it" % (seg.get("type"), key))
        parts = []
        for kind, payload in parse_parts(params.get("message")):
            if kind == "text":
                if payload.strip():
                    parts.append(("text", payload))
                continue
            path = self.resolve_image(payload)
            if path:
                parts.append(("image", path))
            else:
                log("WARN: dropping an image AstrBot asked to send to %s "
                    "(could not turn the segment into a file)" % key)
        if not parts:
            return self.ok({"message_id": self.next_msg_id()})

        log("SEND -> %s : %s" % (chat["name"], describe_parts(parts)))
        if self.dry_run:
            return self.ok({"message_id": self.next_msg_id()})
        try:
            await self.submit(lambda: self.backend.send_parts(chat, parts))
        except Exception as e:
            return self.failed("could not deliver to %s: %s" % (key, e))
        self.last_send_ts = time.time()
        return self.ok({"message_id": self.next_msg_id()})
    # ---------- websocket ----------

    def headers(self):
        h = {"X-Client-Role": "universal", "X-Self-ID": str(self.self_id)}
        token = (self.cfg.get("token") or "").strip()
        if token:
            h["Authorization"] = "Bearer " + token
        return h

    def self_int(self):
        return int(self.self_id) if self.self_id.isdigit() else 0

    async def send_lifecycle(self):
        ev = {
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "time": now_ts(),
            "self_id": self.self_int(),
        }
        await self.ws.send(json.dumps(ev))

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(20)
            if self.ws is None:
                continue
            ev = {
                "post_type": "meta_event", "meta_event_type": "heartbeat",
                "time": now_ts(), "interval": 20000,
                "self_id": self.self_int(),
                "status": {"online": True, "good": True},
            }
            try:
                await self.ws.send(json.dumps(ev))
            except Exception:
                return

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.start()
        url = self.cfg["ws_url"]
        log("connecting to %s as self_id=%s" % (url, self.self_id))
        while True:
            try:
                async with websockets.connect(url, additional_headers=self.headers(),
                                              ping_interval=20, ping_timeout=20,
                                              max_size=None) as ws:
                    self.ws = ws
                    log("connected to AstrBot")
                    await self.send_lifecycle()
                    hb = asyncio.create_task(self.heartbeat_loop())
                    try:
                        async for raw in ws:
                            try:
                                obj = json.loads(raw)
                            except Exception:
                                continue
                            if "action" in obj:
                                res = await self.handle_action(obj["action"], obj.get("params") or {})
                                res["echo"] = obj.get("echo")
                                await ws.send(json.dumps(res, ensure_ascii=False))
                    finally:
                        hb.cancel()
                        self.ws = None
            except Exception as e:
                log("ws error: %s" % e)
            log("reconnecting in 5s ...")
            await asyncio.sleep(5)


def single_instance_name(config_path):
    """Mutex name shared with the start/watch scripts (config name, cleaned)."""
    stem = os.path.splitext(os.path.basename(str(config_path)))[0]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", stem) or "default"
    return "onebot_uia_bridge_" + safe


def claim_single_instance(config_path):
    """Take a named-mutex lease for this config; False if one is already held.

    Two bridges pointed at one AstrBot make every message arrive twice: AstrBot
    does not dedupe across WebSocket clients, and both copies also drive the
    same QQ window. Scanning the process list cannot see a bridge whose command
    line is unreadable, so the lease lives in the kernel instead, where every
    process on the machine sees the same object.

    Returns the mutex handle (hold it for the process lifetime), None when the
    platform offers no mutex, or False when another bridge already holds it.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    name = single_instance_name(config_path)
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        log("could not create the single-instance mutex %s (error %s)"
            % (name, ctypes.get_last_error()))
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    return handle


def main():
    ap = argparse.ArgumentParser(description="UIA -> OneBot v11 bridge for AstrBot")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selfcheck", action="store_true",
                    help="attach to QQ, report config vs live sessions, then exit")
    args = ap.parse_args()
    if not args.selfcheck:
        # --selfcheck only reads QQ and never opens a socket, so it may run next
        # to a live bridge; every other mode must be the only one for a config.
        guard = claim_single_instance(args.config)
        if guard is False:
            log("another bridge is already running for %s - refusing to start" % args.config)
            log("two bridges on one AstrBot deliver every message twice")
            log("run scripts\\stop-bridge.ps1 first if you really want a new one")
            sys.exit(3)
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    bridge = Bridge(cfg, dry_run=args.dry_run)
    if args.selfcheck:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            bridge.backend.connect()
            bridge.selfcheck()
        return
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
