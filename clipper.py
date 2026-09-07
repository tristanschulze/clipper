#!/usr/bin/env python3
"""
CLIPPER — minimal region screen recorder for Windows.

  video  : ffmpeg gdigrab
  system : WASAPI loopback (PyAudioWPatch) piped to ffmpeg over a local socket
  mic    : ffmpeg dshow
  pause  : records segments, concatenated losslessly on stop

Requirements:
    Windows, Python 3.9+
    ffmpeg.exe on PATH or next to this script
    pip install PyAudioWPatch        <- only needed for system sound

Usage:
    python clipper.py
"""

import ctypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from pathlib import Path

FFMPEG_PATH = None  # e.g. r"C:\ffmpeg\bin\ffmpeg.exe"
CREATE_NO_WINDOW = 0x08000000

try:
    import pyaudiowpatch as pyaudio
    HAVE_WASAPI = True
    WASAPI_IMPORT_ERROR = None
except Exception as _e:
    HAVE_WASAPI = False
    WASAPI_IMPORT_ERROR = str(_e)


# ----------------------------------------------------------------------------
# Windows helpers
# ----------------------------------------------------------------------------

def make_dpi_aware():
    """Before any Tk window exists, so Tk reports physical pixels.
    Otherwise gdigrab and Tk disagree at any display scaling != 100%."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def virtual_screen():
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
            u.GetSystemMetrics(78), u.GetSystemMetrics(79))


def find_ffmpeg():
    if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
        return FFMPEG_PATH
    local = Path(__file__).parent / "ffmpeg.exe"
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg")


def list_dshow_mics(ffmpeg):
    try:
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=15,
            creationflags=CREATE_NO_WINDOW, errors="replace")
    except Exception:
        return []
    out, seen = [], set()
    for line in (p.stderr or "").splitlines():
        if "(audio)" not in line:
            continue
        m = re.search(r'"([^"]+)"', line)
        if m and not m.group(1).startswith("@device") and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append(m.group(1))
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def peak_dbfs(pcm_s16):
    """Peak level of a raw s16le buffer, in dBFS. -inf when digitally silent."""
    peak = 0
    # stride over the buffer; full precision is unnecessary for a level check
    for i in range(0, len(pcm_s16) - 1, 16):
        v = int.from_bytes(pcm_s16[i:i + 2], "little", signed=True)
        if v < 0:
            v = -v
        if v > peak:
            peak = v
    if peak == 0:
        return None
    import math
    return 20 * math.log10(peak / 32768.0)


# ----------------------------------------------------------------------------
# System audio: WASAPI loopback -> TCP -> ffmpeg
# ----------------------------------------------------------------------------

class LoopbackFeeder:
    """Reads whatever the default output device is playing and streams raw
    s16le PCM to ffmpeg. No virtual cable, no Stereo Mix needed."""

    def __init__(self):
        self.pa = None
        self.info = None
        self.rate = 48000
        self.channels = 2
        self.thread = None
        self.stop_flag = threading.Event()
        self.error = None
        self.bytes_sent = 0

    def _pa(self):
        if self.pa is None:
            self.pa = pyaudio.PyAudio()
        return self.pa

    def probe(self):
        """Find the loopback device belonging to the current default speakers."""
        if not HAVE_WASAPI:
            raise RuntimeError("PyAudioWPatch not installed")
        pa = self._pa()
        dev = None
        try:
            dev = pa.get_default_wasapi_loopback()
        except Exception:
            pass
        if dev is None:
            wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if spk.get("isLoopbackDevice"):
                dev = spk
            else:
                for lb in pa.get_loopback_device_info_generator():
                    if spk["name"] in lb["name"]:
                        dev = lb
                        break
        if dev is None:
            raise RuntimeError("no WASAPI loopback device found")
        self.info = dev
        self.rate = int(dev["defaultSampleRate"])
        self.channels = min(2, int(dev["maxInputChannels"]) or 2)
        return dev["name"]

    def open_stream(self):
        return self._pa().open(format=pyaudio.paInt16,
                               channels=self.channels,
                               rate=self.rate,
                               input=True,
                               input_device_index=self.info["index"],
                               frames_per_buffer=1024)

    def measure(self, seconds=1.5):
        """Capture briefly and return peak dBFS — used by the Test button."""
        if self.info is None:
            self.probe()
        stream = self.open_stream()
        chunks = []
        try:
            n = int(self.rate / 1024 * seconds)
            for _ in range(max(1, n)):
                chunks.append(stream.read(1024, exception_on_overflow=False))
        finally:
            try:
                stream.stop_stream(); stream.close()
            except Exception:
                pass
        return peak_dbfs(b"".join(chunks))

    def start(self, port):
        self.stop_flag.clear()
        self.error = None
        self.bytes_sent = 0
        self.thread = threading.Thread(target=self._run, args=(port,), daemon=True)
        self.thread.start()

    def _run(self, port):
        sock = None
        stream = None
        for _ in range(120):                    # wait for ffmpeg's listening socket
            if self.stop_flag.is_set():
                return
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                break
            except OSError:
                time.sleep(0.05)
        if sock is None:
            self.error = "could not reach ffmpeg's audio port"
            return
        try:
            stream = self.open_stream()
            while not self.stop_flag.is_set():
                data = stream.read(1024, exception_on_overflow=False)
                sock.sendall(data)
                self.bytes_sent += len(data)
        except Exception as e:
            if not self.stop_flag.is_set():
                self.error = f"audio capture: {e}"
        finally:
            for fn in (lambda: stream.stop_stream(), lambda: stream.close(),
                       lambda: sock.shutdown(socket.SHUT_WR), lambda: sock.close()):
                try:
                    fn()
                except Exception:
                    pass

    def stop(self):
        self.stop_flag.set()
        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None

    def close(self):
        self.stop()
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None


# ----------------------------------------------------------------------------
# Region picker
# ----------------------------------------------------------------------------

class RegionPicker:
    def __init__(self, master):
        self.master = master
        self.result = None

    def pick(self):
        vx, vy, vw, vh = virtual_screen()
        top = tk.Toplevel(self.master)
        top.overrideredirect(True)
        top.geometry(f"{vw}x{vh}+{vx}+{vy}")
        top.attributes("-topmost", True)
        top.attributes("-alpha", 0.28)
        top.configure(bg="white")

        cv = tk.Canvas(top, bg="white", highlightthickness=0, cursor="crosshair")
        cv.pack(fill="both", expand=True)
        st = {"x0": 0, "y0": 0, "rect": None, "label": None, "on": False}

        def press(e):
            st["x0"], st["y0"], st["on"] = e.x, e.y, True
            if st["rect"]:
                cv.delete(st["rect"])
            st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#111111", width=2)

        def drag(e):
            if not st["on"]:
                return
            x0, y0 = st["x0"], st["y0"]
            cv.coords(st["rect"], x0, y0, e.x, e.y)
            if st["label"]:
                cv.delete(st["label"])
            st["label"] = cv.create_text(min(x0, e.x) + 6, max(min(y0, e.y) - 14, 8),
                                         text=f"{abs(e.x - x0)} x {abs(e.y - y0)}",
                                         anchor="w", fill="#111111",
                                         font=("Segoe UI", 11, "bold"))

        def release(e):
            if not st["on"]:
                return
            st["on"] = False
            x0, y0 = st["x0"], st["y0"]
            w, h = abs(e.x - x0), abs(e.y - y0)
            if w < 16 or h < 16:
                cancel()
                return
            self.result = (min(x0, e.x) + vx, min(y0, e.y) + vy, w, h)
            top.destroy()

        def cancel(_=None):
            self.result = None
            top.destroy()

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", drag)
        cv.bind("<ButtonRelease-1>", release)
        top.bind("<Escape>", cancel)
        top.bind("<ButtonPress-3>", cancel)
        top.focus_force()
        top.grab_set()
        self.master.wait_window(top)
        return self.result


# ----------------------------------------------------------------------------
# Styling
# ----------------------------------------------------------------------------

QUALITY = {"High (crf 15)": 15, "Good (crf 18)": 18, "Small (crf 23)": 23}

BG, FG, MUTED, LINE = "#ffffff", "#111111", "#8a8a8a", "#e2e2e2"
FONT, FONT_S = ("Segoe UI", 10), ("Segoe UI", 9)


def apply_style(root):
    s = ttk.Style(root)
    try:
        s.theme_use("clam")
    except Exception:
        pass
    s.configure(".", background=BG, foreground=FG, font=FONT)
    s.configure("TFrame", background=BG)
    s.configure("TLabel", background=BG, foreground=FG, font=FONT)
    s.configure("Muted.TLabel", foreground=MUTED, font=FONT_S)
    s.configure("Head.TLabel", foreground=MUTED, font=("Segoe UI", 8))
    s.configure("TButton", background=BG, foreground=FG, font=FONT, borderwidth=1,
                focusthickness=0, padding=(10, 5), relief="solid", bordercolor=LINE)
    s.map("TButton", background=[("active", "#f5f5f5"), ("disabled", BG)],
          foreground=[("disabled", MUTED)])
    s.configure("Rec.TButton", font=("Segoe UI", 11), padding=(10, 9))
    s.configure("Mini.TButton", font=("Segoe UI", 9), padding=(9, 4))
    s.configure("Small.TButton", font=FONT_S, padding=(8, 3))
    s.configure("TCheckbutton", background=BG, foreground=FG, font=FONT)
    s.map("TCheckbutton", background=[("active", BG)],
          foreground=[("disabled", MUTED)])
    s.configure("TCombobox", fieldbackground=BG, background=BG, foreground=FG,
                arrowcolor=FG, bordercolor=LINE, padding=4)
    s.configure("TSeparator", background=LINE)
    return s


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------

class Clipper(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Clipper")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)          # always on top

        self.ffmpeg = find_ffmpeg()
        self.region = None
        self.proc = None
        self.stderr_buf = []
        self.loop = LoopbackFeeder()
        self.sys_ok = False

        self.segments = []
        self.outfile = None
        self.elapsed = 0.0
        self.seg_start = None
        self.paused = False
        self.mini = None

        apply_style(self)
        self._build_ui()

        if not self.ffmpeg:
            self.status.set("ffmpeg not found — put ffmpeg.exe on PATH or next to this script")
            self.btn_rec.state(["disabled"])
        else:
            self.after(150, self.refresh_audio)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # -- ui ------------------------------------------------------------------

    def _head(self, parent, text):
        ttk.Label(parent, text=text.upper(), style="Head.TLabel").pack(anchor="w", pady=(14, 3))

    def _build_ui(self):
        pad = ttk.Frame(self, padding=(22, 18, 22, 18))
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, text="Clipper", font=("Segoe UI", 15)).pack(anchor="w")
        ttk.Label(pad, text="region screen recorder", style="Muted.TLabel").pack(anchor="w")

        self._head(pad, "Region")
        rw = ttk.Frame(pad); rw.pack(fill="x")
        self.region_lbl = tk.StringVar(value="none selected")
        ttk.Label(rw, textvariable=self.region_lbl).pack(side="left")
        ttk.Button(rw, text="Full screen", command=self.use_fullscreen).pack(side="right")
        ttk.Button(rw, text="Draw…", command=self.pick_region).pack(side="right", padx=(0, 6))

        self._head(pad, "Audio")
        sr = ttk.Frame(pad); sr.pack(fill="x")
        self.sys_var = tk.BooleanVar(value=False)
        self.sys_chk = ttk.Checkbutton(sr, text="system sound (what you hear)",
                                       variable=self.sys_var, onvalue=True, offvalue=False)
        self.sys_chk.pack(side="left")
        self.btn_test = ttk.Button(sr, text="Test", style="Small.TButton",
                                   command=self.test_system_audio)
        self.btn_test.pack(side="right")
        self.sys_note = tk.StringVar(value="")
        ttk.Label(pad, textvariable=self.sys_note, style="Muted.TLabel",
                  wraplength=420).pack(anchor="w", pady=(2, 8))

        mw = ttk.Frame(pad); mw.pack(fill="x")
        ttk.Label(mw, text="mic").pack(side="left", padx=(0, 8))
        self.mic_var = tk.StringVar(value="none")
        self.mic_cb = ttk.Combobox(mw, textvariable=self.mic_var, state="readonly",
                                   values=["none"], width=32)
        self.mic_cb.pack(side="left")
        ttk.Button(mw, text="↻", width=3, command=self.refresh_audio).pack(side="left", padx=(6, 0))

        self._head(pad, "Settings")
        sw = ttk.Frame(pad); sw.pack(fill="x")
        self.fps_var = tk.StringVar(value="30")
        self.qual_var = tk.StringVar(value="Good (crf 18)")
        self.fmt_var = tk.StringVar(value="mp4")
        ttk.Label(sw, text="fps").grid(row=0, column=0, sticky="w")
        ttk.Combobox(sw, textvariable=self.fps_var, state="readonly", width=5,
                     values=["24", "30", "60"]).grid(row=0, column=1, padx=(6, 16))
        ttk.Label(sw, text="quality").grid(row=0, column=2, sticky="w")
        ttk.Combobox(sw, textvariable=self.qual_var, state="readonly", width=14,
                     values=list(QUALITY)).grid(row=0, column=3, padx=(6, 16))
        ttk.Label(sw, text="format").grid(row=0, column=4, sticky="w")
        ttk.Combobox(sw, textvariable=self.fmt_var, state="readonly", width=6,
                     values=["mp4", "mkv"]).grid(row=0, column=5, padx=(6, 0))

        self.cursor_var = tk.BooleanVar(value=False)      # cursor hidden by default
        ttk.Checkbutton(pad, text="show mouse cursor in recording",
                        variable=self.cursor_var).pack(anchor="w", pady=(10, 0))

        self._head(pad, "Save to")
        ow = ttk.Frame(pad); ow.pack(fill="x")
        self.outdir = tk.StringVar(value=str(Path.home() / "Videos"))
        ttk.Label(ow, textvariable=self.outdir, style="Muted.TLabel",
                  wraplength=320).pack(side="left")
        ttk.Button(ow, text="Change…", command=self.choose_dir).pack(side="right")

        ttk.Separator(pad).pack(fill="x", pady=(18, 14))

        bw = ttk.Frame(pad); bw.pack(fill="x")
        self.btn_rec = ttk.Button(bw, text="Record", style="Rec.TButton", command=self.start)
        self.btn_rec.pack(side="left")
        ttk.Button(bw, text="Open folder", command=self.open_folder).pack(side="right")

        self.status = tk.StringVar(value="ready — draw a region to start")
        ttk.Label(pad, textvariable=self.status, style="Muted.TLabel",
                  wraplength=420).pack(anchor="w", pady=(12, 0))

    def build_mini(self):
        m = tk.Toplevel(self)
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg=LINE)                       # 1px hairline border
        inner = tk.Frame(m, bg=BG)
        inner.pack(padx=1, pady=1, fill="both", expand=True)
        box = ttk.Frame(inner, padding=(12, 8))
        box.pack()

        self.dot = tk.Canvas(box, width=10, height=10, bg=BG, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill="#d92b2b", outline="")

        self.timer = tk.StringVar(value="0:00")
        ttk.Label(box, textvariable=self.timer,
                  font=("Consolas", 13)).pack(side="left", padx=(0, 12))
        self.btn_pause = ttk.Button(box, text="Pause", style="Mini.TButton",
                                    command=self.toggle_pause)
        self.btn_pause.pack(side="left", padx=(0, 6))
        ttk.Button(box, text="Stop", style="Mini.TButton",
                   command=self.stop).pack(side="left")

        m.update_idletasks()
        vx, vy, vw, _ = virtual_screen()
        m.geometry(f"+{vx + vw - m.winfo_width() - 24}+{vy + 24}")

        drag = {"x": 0, "y": 0}

        def down(e):
            drag["x"], drag["y"] = e.x_root - m.winfo_x(), e.y_root - m.winfo_y()

        def move(e):
            m.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

        for w in (m, inner, box):
            w.bind("<ButtonPress-1>", down)
            w.bind("<B1-Motion>", move)

        self.mini = m

    # -- actions -------------------------------------------------------------

    def pick_region(self):
        self.withdraw(); self.update(); time.sleep(0.15)
        r = RegionPicker(self).pick()
        self.deiconify(); self.lift()
        if r:
            self.set_region(*r)

    def use_fullscreen(self):
        self.set_region(*virtual_screen())

    def set_region(self, x, y, w, h):
        w -= w % 2      # h264 + yuv420p rejects odd dimensions
        h -= h % 2
        self.region = (x, y, w, h)
        self.region_lbl.set(f"{w} × {h}  at  {x}, {y}")
        self.status.set("ready")

    def set_sys_enabled(self, enabled, note):
        """configure(state=...) — .state(['!disabled']) does not reliably clear it."""
        self.sys_ok = enabled
        self.sys_chk.configure(state="normal" if enabled else "disabled")
        self.sys_chk.state(["!alternate"])
        self.btn_test.configure(state="normal" if enabled else "disabled")
        if not enabled:
            self.sys_var.set(False)
        self.sys_note.set(note)

    def refresh_audio(self):
        if not self.ffmpeg:
            return
        self.status.set("scanning audio…")
        self.update_idletasks()

        mics = list_dshow_mics(self.ffmpeg)
        self.mic_cb["values"] = ["none"] + mics
        if self.mic_var.get() not in ["none"] + mics:
            self.mic_var.set("none")

        if not HAVE_WASAPI:
            self.set_sys_enabled(False, "unavailable — run:  pip install PyAudioWPatch")
        else:
            try:
                name = self.loop.probe()
                was_on = self.sys_var.get()
                self.set_sys_enabled(True, f"WASAPI loopback · {name}")
                self.sys_var.set(True if not self.sys_ok else (was_on or True))
            except Exception as e:
                self.set_sys_enabled(False, f"unavailable — {e}")
        self.status.set("ready")

    def test_system_audio(self):
        """Play something, then press Test. Tells you if audio actually arrives."""
        self.status.set("listening for 1.5 s — make sure something is playing…")
        self.update_idletasks()
        try:
            db = self.loop.measure(1.5)
        except Exception as e:
            self.status.set(f"test failed — {e}")
            return
        if db is None:
            self.status.set(f"silent — nothing is playing on “{self.loop.info['name']}”, "
                            f"or the app you want to record outputs to a different device")
        elif db < -50:
            self.status.set(f"very quiet ({db:.0f} dBFS) — signal is there but barely")
        else:
            self.status.set(f"signal OK — peak {db:.0f} dBFS")

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d:
            self.outdir.set(d)

    def open_folder(self):
        p = Path(self.outdir.get())
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(str(p))

    # -- ffmpeg --------------------------------------------------------------

    def build_cmd(self, target, port):
        x, y, w, h = self.region
        fps = self.fps_var.get()
        crf = QUALITY[self.qual_var.get()]
        mic = self.mic_var.get()
        use_sys = bool(self.sys_var.get()) and self.sys_ok

        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y",
               "-thread_queue_size", "1024",
               "-f", "gdigrab", "-framerate", fps,
               "-draw_mouse", "1" if self.cursor_var.get() else "0",
               "-offset_x", str(x), "-offset_y", str(y),
               "-video_size", f"{w}x{h}", "-rtbufsize", "256M",
               "-i", "desktop"]

        idx, sys_i, mic_i = 1, None, None
        if use_sys:
            cmd += ["-thread_queue_size", "1024", "-f", "s16le",
                    "-ar", str(self.loop.rate), "-ac", str(self.loop.channels),
                    "-i", f"tcp://127.0.0.1:{port}?listen=1&listen_timeout=15000"]
            sys_i = idx; idx += 1
        if mic != "none":
            cmd += ["-thread_queue_size", "1024", "-f", "dshow",
                    "-audio_buffer_size", "80", "-i", f"audio={mic}"]
            mic_i = idx; idx += 1

        if sys_i is not None and mic_i is not None:
            cmd += ["-filter_complex",
                    f"[{sys_i}:a][{mic_i}:a]amix=inputs=2:duration=longest:"
                    f"dropout_transition=0:normalize=0,aresample=async=1[a]",
                    "-map", "0:v", "-map", "[a]"]
        elif sys_i is not None:
            cmd += ["-map", "0:v", "-map", f"{sys_i}:a", "-af", "aresample=async=1"]
        elif mic_i is not None:
            cmd += ["-map", "0:v", "-map", f"{mic_i}:a", "-af", "aresample=async=1"]
        else:
            cmd += ["-map", "0:v"]

        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", fps]
        if sys_i is not None or mic_i is not None:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        if str(target).lower().endswith(".mp4"):
            cmd += ["-movflags", "+faststart"]
        cmd.append(str(target))
        return cmd

    def drain_stderr(self, proc):
        for line in iter(proc.stderr.readline, b""):
            txt = line.decode("utf-8", "replace").strip()
            if txt:
                self.stderr_buf.append(txt)
                if len(self.stderr_buf) > 40:
                    self.stderr_buf.pop(0)

    def start_segment(self):
        fmt = self.fmt_var.get()
        seg = Path(self.outdir.get()) / f".clipper_part{len(self.segments):02d}_{os.getpid()}.{fmt}"
        use_sys = bool(self.sys_var.get()) and self.sys_ok
        port = free_port() if use_sys else 0
        try:
            self.proc = subprocess.Popen(self.build_cmd(seg, port),
                                         stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE,
                                         creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            messagebox.showerror("ffmpeg failed to start", str(e))
            self.proc = None
            return False
        threading.Thread(target=self.drain_stderr, args=(self.proc,), daemon=True).start()
        if use_sys:
            self.loop.start(port)
        self.segments.append(seg)
        self.seg_start = time.time()
        return True

    def stop_segment(self):
        """Graceful: 'q' lets ffmpeg write the moov atom. Kill only as fallback."""
        if self.proc is None:
            return
        try:
            self.proc.stdin.write(b"q\n")
            self.proc.stdin.flush()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.loop.stop()
        if self.seg_start:
            self.elapsed += time.time() - self.seg_start
            self.seg_start = None
        self.proc = None

    # -- transport -----------------------------------------------------------

    def start(self):
        if not self.region:
            self.status.set("draw a region first")
            return
        Path(self.outdir.get()).mkdir(parents=True, exist_ok=True)
        self.segments, self.elapsed, self.paused = [], 0.0, False
        self.stderr_buf = []
        self.outfile = str(Path(self.outdir.get()) /
                           f"clip_{datetime.now():%Y%m%d_%H%M%S}.{self.fmt_var.get()}")
        if not self.start_segment():
            return
        self.withdraw()
        self.build_mini()
        self.tick()

    def toggle_pause(self):
        if self.paused:
            if self.start_segment():
                self.paused = False
                self.btn_pause.config(text="Pause")
                self.dot.itemconfig(self.dot_id, fill="#d92b2b")
                self.tick()
        else:
            self.stop_segment()
            self.paused = True
            self.btn_pause.config(text="Resume")
            self.dot.itemconfig(self.dot_id, fill="#c9c9c9")

    def tick(self):
        if self.proc is None or self.paused:
            return
        if self.proc.poll() is not None:
            self.proc = None
            self.loop.stop()
            self.restore(" / ".join(self.stderr_buf[-2:]) or "ffmpeg exited unexpectedly")
            return
        el = self.elapsed + (time.time() - self.seg_start)
        self.timer.set(f"{int(el // 60)}:{int(el % 60):02d}")
        self.after(200, self.tick)

    def stop(self):
        self.stop_segment()
        parts = [p for p in self.segments if p.exists() and p.stat().st_size > 0]
        if not parts:
            self.restore(" / ".join(self.stderr_buf[-2:]) or
                         "nothing was written — check the region and audio settings")
            return
        try:
            if len(parts) == 1:
                parts[0].replace(Path(self.outfile))
            else:
                self.concat(parts)
        except Exception as e:
            self.restore(f"could not finalise: {e}")
            return
        for p in self.segments:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        f = Path(self.outfile)
        msg = (f"saved  {f.name}  ({f.stat().st_size / 1e6:.1f} MB)"
               if f.exists() else "finished, but no output file appeared")
        if self.sys_var.get() and self.sys_ok:
            if self.loop.error:
                msg += f"  ·  {self.loop.error}"
            elif self.loop.bytes_sent == 0:
                msg += "  ·  no system audio captured — press Test to check the device"
        self.restore(msg)

    def concat(self, parts):
        """Stream-copy join — every segment shares identical encoder settings."""
        lst = Path(self.outdir.get()) / f".clipper_list_{os.getpid()}.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        try:
            subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", self.outfile],
                           check=True, creationflags=CREATE_NO_WINDOW,
                           capture_output=True, timeout=180)
        finally:
            try:
                lst.unlink()
            except Exception:
                pass

    def restore(self, message):
        if self.mini is not None:
            self.mini.destroy()
            self.mini = None
        self.paused = False
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.status.set(message[:300])

    def on_close(self):
        if self.proc is not None:
            self.stop()
        self.loop.close()
        self.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This build targets Windows (gdigrab / dshow / WASAPI).")
    make_dpi_aware()
    Clipper().mainloop()