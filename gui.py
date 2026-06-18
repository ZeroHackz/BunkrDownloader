"""
Graphical user interface for the Bunkr Downloader.
"""
#import asyncio
import io
import logging
import os
import platform
import sys
import threading
import webbrowser
from tkinter import filedialog

import customtkinter as ctk

from src.config import DOWNLOAD_FOLDER, MAX_RETRIES, MAX_WORKERS
from src.version import __version__ as BACKEND_VERSION

GUI_VERSION = "2026.06.18"
GITHUB_URL = "https://github.com/ZeroHackz/BunkrDownloader"

# When running from a PyInstaller bundle, sys.executable is the GUI's own .exe
# and there's no separate python.exe to invoke — instead we self-exec with the
# `--gui-runner` sentinel to enter runner mode (see __main__ at the bottom).
IS_FROZEN = getattr(sys, "frozen", False)


def _child_python_exe():
    """Prefer pythonw.exe so the child has no console window. Only used when
    running from source — never returns the bundled .exe."""
    exe = sys.executable
    base = os.path.dirname(exe)
    candidate = os.path.join(base, "pythonw.exe")
    if os.path.isfile(candidate):
        return candidate
    return exe

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


class IORedirector(io.StringIO):
    """Thread-safe stdout redirector that writes to a CTkTextbox via after()."""

    def __init__(self, textbox, root):
        super().__init__()
        self.textbox = textbox
        self.root = root

    def _classify(self, text):
        """Map backend log lines to a color tag based on keywords."""
        lower = text.lower()
        if "download failed" in lower or "error" in lower or "exceeded" in lower:
            return "fail"
        if "skipped" in lower or "already been downloaded" in lower:
            return "skip"
        if "completed" in lower or "success" in lower:
            return "ok"
        return None

    def write(self, text):
        tag = self._classify(text)

        def _append():
            self.textbox.configure(state="normal")
            if tag:
                self.textbox.insert("end", text, tag)
            else:
                self.textbox.insert("end", text)
            self.textbox.see("end")
            self.textbox.configure(state="disabled")
        self.root.after(0, _append)

    def flush(self):
        pass


class DownloaderUI(ctk.CTk):
    """Main application window for the Bunkr Downloader."""

    def __init__(self):
        super().__init__()

        self.title("Bunkr Downloader")
        self.geometry("700x720")
        self.minsize(600, 580)
        self._stop_requested = False      # Pause: skip remaining URLs after current
        self._force_stop = False          # Stop: hard-kill current download
        self._worker_thread = None        # Worker thread running _run_batch
        self._active_proc = None          # External console subprocess (if used)
        self._album_total = 0             # Files in the currently downloading album
        self._album_done = 0              # Files finished so far in the album
        self._active_files = []           # Filenames currently downloading
        self._active_lock = threading.Lock()
        self._url_index = 0               # 1-based URL index in the current batch
        self._url_total = 0               # Total URLs in the current batch

        try:
            icon_path = resource_path(os.path.join("misc", "gui", "icons", "icon.ico"))
            self.after(200, lambda: self.iconbitmap(icon_path))
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Shared StringVar for download destination (Download tab + Settings tab stay in sync).
        # Left blank by default so the backend uses its own './Downloads/' — pre-filling
        # this with "Downloads" would pass --custom-path Downloads, and the backend
        # would create Downloads/Downloads/<album>/.
        self.dest_var = ctk.StringVar(value="")

        # Tab view
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="nsew")

        self.tab_dl = self.tabs.add("  Download  ")
        self.tab_settings = self.tabs.add("  Settings  ")
        self.tab_about = self.tabs.add("  About  ")

        self._build_download_tab()
        self._build_settings_tab()
        self._build_about_tab()

        # Must call after tabs are built (accesses self.mode)
        self._toggle_mode()

        # Status bar
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(self, textvariable=self.status_var, anchor="w",
                     text_color="gray60").grid(
            row=1, column=0, padx=20, pady=(0, 10), sticky="ew")

        # Redirect stdout so downloader print() output flows into the log
        sys.stdout = IORedirector(self.log, self)

        # Route Python logging to the same textbox. The backend's log_manager calls
        # logging.info(...) when --disable-ui is on; without a handler, those messages
        # are silently swallowed and the GUI looks frozen during long downloads.
        gui_handler = logging.StreamHandler(sys.stdout)
        gui_handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(gui_handler)

    # ─────────────────────────────────────────────────────────────────────────
    # Tab builders
    # ─────────────────────────────────────────────────────────────────────────

    def _build_download_tab(self):
        tab = self.tab_dl
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(6, weight=1)

        # Mode toggle
        mode_frame = ctk.CTkFrame(tab)
        mode_frame.grid(row=0, column=0, padx=0, pady=(0, 10), sticky="ew")
        ctk.CTkLabel(mode_frame, text="Download mode:").pack(
            side="left", padx=(12, 8), pady=10)
        self.mode = ctk.StringVar(value="url")
        ctk.CTkRadioButton(mode_frame, text="Single URL",
                           variable=self.mode, value="url",
                           command=self._toggle_mode).pack(side="left", padx=8, pady=10)
        ctk.CTkRadioButton(mode_frame, text="Batch from file",
                           variable=self.mode, value="file",
                           command=self._toggle_mode).pack(side="left", padx=8, pady=10)

        # URL input frame
        self.url_frame = ctk.CTkFrame(tab)
        self.url_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.url_frame,
                     text="Bunkr URL:").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 2), sticky="w")
        self.url_entry = ctk.CTkEntry(
            self.url_frame, placeholder_text="Paste a Bunkr album or file URL here…")
        self.url_entry.grid(row=1, column=0, padx=(12, 6), pady=(0, 10), sticky="ew")
        ctk.CTkButton(self.url_frame, text="Paste", width=70,
                      fg_color="#c0392b", hover_color="#922b21",
                      command=self._paste_url).grid(
            row=1, column=1, padx=(0, 12), pady=(0, 10))

        # Batch file input frame
        self.file_frame = ctk.CTkFrame(tab)
        self.file_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(self.file_frame,
                     text="URL list file (.txt — one URL per line):").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 2), sticky="w")
        self.file_entry = ctk.CTkEntry(
            self.file_frame, placeholder_text="Click Browse to select a .txt file…")
        self.file_entry.grid(row=1, column=0, padx=(12, 6), pady=(0, 10), sticky="ew")
        ctk.CTkButton(self.file_frame, text="Browse", width=80,
                      command=self._browse_input_file).grid(
            row=1, column=1, padx=(0, 12), pady=(0, 10))

        # Download destination
        dest_frame = ctk.CTkFrame(tab)
        dest_frame.grid(row=2, column=0, padx=0, pady=(0, 10), sticky="ew")
        dest_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(dest_frame, text="Save downloads to:").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(10, 2), sticky="w")
        ctk.CTkEntry(dest_frame, textvariable=self.dest_var,
                     placeholder_text=f"Default: ./{DOWNLOAD_FOLDER}/  (click Browse to change)").grid(
            row=1, column=0, padx=(12, 6), pady=(0, 10), sticky="ew")
        ctk.CTkButton(dest_frame, text="Browse", width=80,
                      command=self._browse_dest_dir).grid(
            row=1, column=1, padx=(0, 12), pady=(0, 10))

        # Action buttons
        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.grid(row=3, column=0, padx=0, pady=(0, 8), sticky="ew")
        self.run_btn = ctk.CTkButton(btn_frame, text="▶  Download",
                                     height=40,
                                     font=ctk.CTkFont(size=15, weight="bold"),
                                     command=self._start_download)
        self.run_btn.pack(side="left", padx=(0, 10))
        self.pause_btn = ctk.CTkButton(btn_frame, text="❚❚  Pause",
                                       height=40, width=100,
                                       fg_color="#d97706", hover_color="#b45309",
                                       state="disabled",
                                       command=self._pause)
        self.pause_btn.pack(side="left", padx=(0, 10))
        self.stop_btn = ctk.CTkButton(btn_frame, text="■  Stop",
                                      height=40, width=100,
                                      fg_color="#c0392b", hover_color="#922b21",
                                      state="disabled",
                                      command=self._stop)
        self.stop_btn.pack(side="left", padx=(0, 10))
        self.open_btn = ctk.CTkButton(btn_frame, text="Open folder",
                                      height=40, width=130,
                                      fg_color="gray30", hover_color="gray40",
                                      command=self._open_dest)
        self.open_btn.pack(side="left")

        # Progress bar (indeterminate while running)
        self.progress = ctk.CTkProgressBar(tab, mode="indeterminate")
        self.progress.grid(row=4, column=0, padx=0, pady=(0, 6), sticky="ew")
        self.progress.set(0)

        # Log output — dark, monospace, expands to fill space
        self.log = ctk.CTkTextbox(tab,
                                  font=ctk.CTkFont(family="Consolas", size=10),
                                  fg_color="#1a1a1a", text_color="#d4d4d4",
                                  wrap="word")
        self.log.grid(row=6, column=0, padx=0, pady=(0, 4), sticky="nsew")
        self.log.configure(state="disabled")

        # Color tags for the log textbox (uses underlying Tk Text widget)
        self.log.tag_config("header", foreground="#7dd3fc")
        self.log.tag_config("ok", foreground="#86efac")
        self.log.tag_config("fail", foreground="#fca5a5")
        self.log.tag_config("skip", foreground="#b5f97a")
        self.log.tag_config("dim", foreground="#6b7280")

    def _build_settings_tab(self):
        tab = self.tab_settings
        tab.grid_columnconfigure(1, weight=1)

        row = 0

        def section(text, r):
            ctk.CTkLabel(tab, text=text,
                         font=ctk.CTkFont(size=13, weight="bold"),
                         text_color="gray70").grid(
                row=r, column=0, columnspan=2, padx=4, pady=(18, 6), sticky="w")

        # ── Download ──────────────────────────────────────────────────────────
        section("Download", row)
        row += 1

        ctk.CTkLabel(tab, text="Default save folder:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        dest_row = ctk.CTkFrame(tab, fg_color="transparent")
        dest_row.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        dest_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(dest_row, textvariable=self.dest_var,
                     placeholder_text=f"Default: ./{DOWNLOAD_FOLDER}/  —  e.g. C:\\MyDownloads").grid(
            row=0, column=0, padx=(0, 6), sticky="ew")
        ctk.CTkButton(dest_row, text="Browse", width=80,
                      command=self._browse_dest_dir).grid(row=0, column=1)
        row += 1

        ctk.CTkLabel(tab, text="Max concurrent downloads:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        workers_frame = ctk.CTkFrame(tab, fg_color="transparent")
        workers_frame.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        self.workers_label = ctk.CTkLabel(workers_frame,
                                          text=str(MAX_WORKERS), width=24)
        self.workers_label.pack(side="right")
        self.workers_slider = ctk.CTkSlider(workers_frame, from_=1, to=5,
                                            number_of_steps=4,
                                            command=self._on_workers_change)
        self.workers_slider.set(MAX_WORKERS)
        self.workers_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        row += 1

        # ── File filters ──────────────────────────────────────────────────────
        section("File filters", row)
        row += 1

        ctk.CTkLabel(tab, text="Include only (keywords):", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        self.settings_include = ctk.CTkEntry(
            tab, placeholder_text="e.g.  mp4  jpg  png  — leave blank for everything")
        self.settings_include.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Exclude (keywords):", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        self.settings_exclude = ctk.CTkEntry(
            tab, placeholder_text="e.g.  .thumb  preview  sample")
        self.settings_exclude.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        row += 1

        ctk.CTkLabel(tab,
                     text="Space-separated keywords. Leave both blank to download everything.",
                     text_color="gray50",
                     font=ctk.CTkFont(size=11)).grid(
            row=row, column=0, columnspan=2, padx=4, pady=(2, 0), sticky="w")
        row += 1

        # ── Advanced ──────────────────────────────────────────────────────────
        section("Advanced", row)
        row += 1

        ctk.CTkLabel(tab, text="Max retries per file:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        retries_frame = ctk.CTkFrame(tab, fg_color="transparent")
        retries_frame.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        self.retries_label = ctk.CTkLabel(retries_frame,
                                          text=str(MAX_RETRIES), width=24)
        self.retries_label.pack(side="right")
        self.retries_slider = ctk.CTkSlider(retries_frame, from_=1, to=10,
                                            number_of_steps=9,
                                            command=self._on_retries_change)
        self.retries_slider.set(MAX_RETRIES)
        self.retries_slider.pack(side="left", fill="x", expand=True, padx=(0, 8))
        row += 1

        self.opt_no_disk_check = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab, text="Skip disk space check before downloading",
                        variable=self.opt_no_disk_check).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        row += 1

        self.opt_no_dl_folder = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab,
                        text='Skip the "Downloads" subfolder when using the default path '
                             "(a custom save folder already skips it automatically)",
                        variable=self.opt_no_dl_folder).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")
        row += 1

        self.opt_external_console = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            tab,
            text="Show detailed CLI window during downloads "
                 "(opens a separate console with full progress bars)",
            variable=self.opt_external_console).grid(
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w")

    def _build_about_tab(self):
        tab = self.tab_about
        tab.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(tab, text="Bunkr Downloader",
                     font=ctk.CTkFont(size=26, weight="bold")).grid(
            row=0, column=0, pady=(30, 4))
        ctk.CTkLabel(tab, text=f"GUI v{GUI_VERSION}  ·  by ZeroHackz",
                     font=ctk.CTkFont(size=13), text_color="gray60").grid(
            row=1, column=0, pady=(0, 4))
        ctk.CTkLabel(tab, text=f"Backend v{BACKEND_VERSION}",
                     font=ctk.CTkFont(size=11), text_color="gray50").grid(
            row=2, column=0, pady=(0, 20))

        ctk.CTkLabel(tab,
                     text="A clean GUI for downloading Bunkr albums and files.\n"
                          "Supports both single URLs and batch downloads from a text file.",
                     wraplength=480, justify="center").grid(
            row=3, column=0, pady=(0, 28))

        ctk.CTkButton(tab, text="View on GitHub →",
                      fg_color="gray25", hover_color="gray35",
                      command=lambda: webbrowser.open(GITHUB_URL)).grid(
            row=4, column=0, pady=6)

        ctk.CTkLabel(tab, text="MIT License", text_color="gray50").grid(
            row=5, column=0, pady=(24, 0))

    # ─────────────────────────────────────────────────────────────────────────
    # UI helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _toggle_mode(self):
        if self.mode.get() == "url":
            self.url_frame.grid(row=1, column=0, padx=0, pady=(0, 10), sticky="ew")
            self.file_frame.grid_forget()
        else:
            self.file_frame.grid(row=1, column=0, padx=0, pady=(0, 10), sticky="ew")
            self.url_frame.grid_forget()

    def _paste_url(self):
        try:
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, self.clipboard_get())
        except Exception:
            self._log("Could not read from clipboard.\n")

    def _browse_input_file(self):
        path = filedialog.askopenfilename(
            title="Select URL list file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if path:
            self.file_entry.delete(0, "end")
            self.file_entry.insert(0, path)

    def _browse_dest_dir(self):
        d = filedialog.askdirectory(title="Select download folder")
        if d:
            self.dest_var.set(d)

    def _open_dest(self):
        d = self.dest_var.get().strip() or DOWNLOAD_FOLDER
        if platform.system() == "Windows" and os.path.isdir(d):
            os.startfile(d)

    def _refresh_album_status(self):
        """Update the bottom status bar — safe to call from any thread."""
        with self._active_lock:
            done = self._album_done
            total = self._album_total
            active = list(self._active_files)

        url_part = (f"URL {self._url_index}/{self._url_total}  ·  "
                    if self._url_total > 1 else "")
        if total > 0:
            files_part = "  ·  " + ", ".join(active) if active else ""
            text = f"{url_part}File {done}/{total}{files_part}"
        else:
            text = f"{url_part}Starting…" if url_part else "Starting…"
        self.after(0, lambda t=text: self.status_var.set(t))

    def _on_workers_change(self, value):
        self.workers_label.configure(text=str(int(value)))

    def _on_retries_change(self, value):
        self.retries_label.configure(text=str(int(value)))

    def _log(self, text, tag=None):
        """Append text to the log textbox — safe to call from any thread."""
        def _append():
            self.log.configure(state="normal")
            if tag:
                self.log.insert("end", text, tag)
            else:
                self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _append)

    def _set_running(self, running: bool):
        """Update button/progress states — safe to call from any thread."""
        def _update():
            if running:
                self.run_btn.configure(state="disabled")
                self.pause_btn.configure(state="normal")
                self.stop_btn.configure(state="normal")
                self.progress.start()
                self.progress.configure(mode="indeterminate")
            else:
                self.run_btn.configure(state="normal")
                self.pause_btn.configure(state="disabled")
                self.stop_btn.configure(state="disabled")
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress.set(0)
        self.after(0, _update)

    # ─────────────────────────────────────────────────────────────────────────
    # Download logic
    # ─────────────────────────────────────────────────────────────────────────

    def _start_download(self):
        # Clear log
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

        mode = self.mode.get()
        if mode == "url":
            url = self.url_entry.get().strip()
            if not url:
                self._log("Please enter a Bunkr URL before clicking Download.\n")
                return
            urls = [url]
        else:
            file_path = self.file_entry.get().strip()
            if not file_path:
                self._log("Please select a .txt file containing URLs.\n")
                return
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    urls = [line.strip() for line in f if line.strip()]
                if not urls:
                    self._log("The selected file is empty or contains no valid URLs.\n")
                    return
            except (IOError, OSError) as e:
                self._log(f"Could not read file: {e}\n")
                return

        self._stop_requested = False
        self._force_stop = False
        self._active_proc = None
        self._set_running(True)
        self._url_index = 0
        self._url_total = len(urls)
        self.status_var.set(f"Starting… ({len(urls)} URL{'s' if len(urls) > 1 else ''})")
        self._worker_thread = threading.Thread(
            target=self._run_batch, args=(urls,), daemon=True)
        self._worker_thread.start()

    def _pause(self):
        """Let the current download finish, skip the rest of the batch.

        Partial files are kept as `.temp`; already-completed files in the album
        are skipped automatically on the next run.
        """
        self._stop_requested = True
        self._log("\n[Pause requested — current download will finish, then stop.]\n",
                  tag="dim")
        self.status_var.set("Pausing…")

    def _stop(self):
        """Force-kill the current download immediately.

        The downloader always runs in a child process, so kill() ends it instantly,
        including any in-flight HTTP reads and thread-pool workers. Partial `.temp`
        files are left behind and will be re-attempted on the next run.
        """
        self._stop_requested = True
        self._force_stop = True
        self._log("\n[Force stop — killing current download.]\n", tag="fail")
        self.status_var.set("Stopping…")
        if self._active_proc is not None:
            try:
                self._active_proc.kill()
            except Exception:
                pass

    def _build_downloader_args(self, url, *, include_ui_flag):
        """Build the argv list passed to the downloader for a single URL."""
        args = [url]
        if include_ui_flag:
            args.append("--disable-ui")

        dest = self.dest_var.get().strip()
        if dest:
            args += ["--custom-path", dest]

        include = self.settings_include.get().strip().split()
        exclude = self.settings_exclude.get().strip().split()
        if include:
            args += ["--include"] + include
        if exclude:
            args += ["--ignore"] + exclude

        args += ["--max-retries", str(int(self.retries_slider.get()))]
        if self.opt_no_disk_check.get():
            args.append("--disable-disk-check")
        if dest or self.opt_no_dl_folder.get():
            args.append("--no-download-folder")

        return args

    def _stream_subprocess(self, proc):
        """Read child stdout line-by-line, parse GUI markers, write the rest to log."""
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            if not line:
                self._log("\n")
                continue

            if line.startswith("__GUI_TOTAL__:"):
                try:
                    total = int(line.split(":", 1)[1])
                except ValueError:
                    continue
                with self._active_lock:
                    self._album_total = total
                    self._album_done = 0
                    self._active_files = []
                self._refresh_album_status()
                continue

            if line.startswith("__GUI_START__:"):
                fname = line.split(":", 1)[1]
                with self._active_lock:
                    self._active_files.append(fname)
                self._refresh_album_status()
                self._log(f"  → {fname}\n")
                continue

            if line.startswith("__GUI_END__:"):
                fname = line.split(":", 1)[1]
                with self._active_lock:
                    if fname in self._active_files:
                        self._active_files.remove(fname)
                    self._album_done += 1
                self._refresh_album_status()
                continue

            # Plain log line from the backend — colorize via the IORedirector
            # classifier logic (same keywords).
            lower = line.lower()
            if "download failed" in lower or "error" in lower or "exceeded" in lower:
                tag = "fail"
            elif "skipped" in lower or "already been downloaded" in lower:
                tag = "skip"
            elif "completed" in lower or "success" in lower:
                tag = "ok"
            else:
                tag = None
            self._log(line + "\n", tag=tag)

        proc.stdout.close()

    def _self_invoke_cmd(self, extra_args):
        """Build the command line that re-launches this program in runner mode.

        Works for both the PyInstaller bundle (self-execs the .exe) and source
        mode (re-invokes python.exe with gui.py)."""
        sentinel = "--gui-runner"
        if IS_FROZEN:
            return [sys.executable, sentinel, *extra_args]
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else "gui.py"
        return [_child_python_exe(), script, sentinel, *extra_args]

    def _run_batch(self, urls):
        import subprocess
        external = self.opt_external_console.get()
        total = len(urls)
        completed = 0
        failed = 0

        for i, url in enumerate(urls):
            if self._stop_requested:
                self._log(f"\n[Stopped — skipping {total - i} remaining URL(s)]\n",
                          tag="dim")
                self.after(0, lambda: self.status_var.set(
                    f"Stopped — {completed} done, {failed} failed"))
                break

            # Reset per-album counters; the LiveManager patch will fill in the
            # totals once the album page is parsed.
            with self._active_lock:
                self._album_total = 0
                self._album_done = 0
                self._active_files = []
            self._url_index = i + 1
            self._url_total = total
            self._refresh_album_status()

            self._log(f"\n{'─' * 60}\n", tag="header")
            self._log(f"  [{i + 1}/{total}]  {url}\n", tag="header")
            self._log(f"{'─' * 60}\n\n", tag="header")

            self._force_stop = False
            try:
                if external:
                    # Detached console with the full Rich UI (progress bars +
                    # log table). No stdout capture, no GUI markers.
                    cmd = [sys.executable, "downloader.py",
                           *self._build_downloader_args(url, include_ui_flag=False)]
                    self._log("  (running in detached console window)\n", tag="dim")
                    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                    self._active_proc = subprocess.Popen(cmd, creationflags=flags)
                    rc = self._active_proc.wait()
                else:
                    # Hidden child — either the bundled .exe self-invoking in
                    # runner mode, or `python gui.py --gui-runner …` in source
                    # mode. Killable instantly via Stop.
                    cmd = self._self_invoke_cmd(
                        self._build_downloader_args(url, include_ui_flag=True))
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    self._active_proc = subprocess.Popen(
                        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace",
                        bufsize=1, creationflags=flags,
                    )
                    self._stream_subprocess(self._active_proc)
                    rc = self._active_proc.wait()

                self._active_proc = None
                if self._force_stop:
                    failed += 1
                    self._log(f"\n  [STOPPED]  {url}\n", tag="fail")
                elif rc == 0:
                    completed += 1
                    self._log(f"\n  [OK]  Finished: {url}\n", tag="ok")
                else:
                    failed += 1
                    self._log(f"\n  [FAIL]  {url} exited with code {rc}\n",
                              tag="fail")

            except Exception as e:
                failed += 1
                self._log(f"\n  [FAIL]  Error downloading {url}:\n     {e}\n",
                          tag="fail")

        else:
            # Loop completed without break
            summary = f"Done — {completed} succeeded"
            if failed:
                summary += f", {failed} failed"
            self._log(f"\n{'═' * 60}\n  {summary}\n{'═' * 60}\n")
            self.after(0, lambda s=summary: self.status_var.set(s))

            # Open destination folder after last download
            dest = self.dest_var.get().strip() or DOWNLOAD_FOLDER
            if platform.system() == "Windows" and os.path.isdir(dest):
                os.startfile(dest)

        # Clear the per-file label so it doesn't show stale info
        with self._active_lock:
            self._album_total = 0
            self._album_done = 0
            self._active_files = []
        self._refresh_album_status()

        self._set_running(False)


def _run_as_gui_runner():
    """Run the downloader inside this process, emitting GUI marker lines.

    Triggered when the executable is launched with `--gui-runner` as the first
    argument. The bundled .exe self-invokes in this mode so the GUI can spawn
    download children without needing a separate runner binary alongside.
    """
    import asyncio
    import logging

    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)

    from src.downloaders import media_downloader as _md
    from src.managers import live_manager as _lm

    _orig_add = _lm.LiveManager.add_overall_task

    def _wrap_add(self, description, num_tasks):
        print(f"__GUI_TOTAL__:{num_tasks}", flush=True)
        return _orig_add(self, description, num_tasks)

    _lm.LiveManager.add_overall_task = _wrap_add

    _orig_dl = _md.MediaDownloader.download

    def _safe_text(s: str) -> str:
        enc = sys.stdout.encoding or "utf-8"
        return s.encode(enc, errors="replace").decode(enc)

    def _wrap_dl(self):
        fname = _safe_text(self.download_info.filename)
        print(f"__GUI_START__:{fname}", flush=True)
        try:
            return _orig_dl(self)
        finally:
            print(f"__GUI_END__:{fname}", flush=True)

    _md.MediaDownloader.download = _wrap_dl

    import downloader as _d
    _d.clear_terminal = lambda: None
    asyncio.run(_d.main())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--gui-runner":
        # Strip the sentinel so the downstream argparse sees the real args.
        del sys.argv[1]
        _run_as_gui_runner()
        sys.exit(0)

    app = DownloaderUI()
    app.mainloop()
