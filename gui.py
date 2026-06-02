"""
Graphical user interface for the Bunkr Downloader.
"""
import asyncio
import io
import os
import platform
import sys
import threading
import webbrowser
from tkinter import filedialog

import customtkinter as ctk

from src.config import DOWNLOAD_FOLDER, MAX_RETRIES, MAX_WORKERS
from src.version import __version__ as BACKEND_VERSION

GUI_VERSION = "2025.11.22"
GITHUB_URL = "https://github.com/ZeroHackz/BunkrDownloader"

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

    def write(self, text):
        def _append():
            self.textbox.configure(state="normal")
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
        self._stop_requested = False

        try:
            icon_path = resource_path(os.path.join("misc", "gui", "icons", "icon.ico"))
            self.after(200, lambda: self.iconbitmap(icon_path))
        except Exception:
            pass

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Shared StringVar for download destination (Download tab + Settings tab stay in sync)
        self.dest_var = ctk.StringVar(value=DOWNLOAD_FOLDER)

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
                     placeholder_text="Choose a folder…").grid(
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
        section("Download", row); row += 1

        ctk.CTkLabel(tab, text="Default save folder:", anchor="w").grid(
            row=row, column=0, padx=(4, 8), pady=4, sticky="w")
        dest_row = ctk.CTkFrame(tab, fg_color="transparent")
        dest_row.grid(row=row, column=1, padx=(0, 4), pady=4, sticky="ew")
        dest_row.grid_columnconfigure(0, weight=1)
        ctk.CTkEntry(dest_row, textvariable=self.dest_var,
                     placeholder_text="e.g. C:\\Downloads\\Bunkr").grid(
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
        section("File filters", row); row += 1

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
        section("Advanced", row); row += 1

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
            row=row, column=0, columnspan=2, padx=4, pady=4, sticky="w"); row += 1

        self.opt_no_dl_folder = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(tab,
                        text='Save directly to folder (skip the "Downloads" subfolder)',
                        variable=self.opt_no_dl_folder).grid(
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

    def _on_workers_change(self, value):
        self.workers_label.configure(text=str(int(value)))

    def _on_retries_change(self, value):
        self.retries_label.configure(text=str(int(value)))

    def _log(self, text):
        """Append text to the log textbox — safe to call from any thread."""
        def _append():
            self.log.configure(state="normal")
            self.log.insert("end", text)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _append)

    def _set_running(self, running: bool):
        """Update button/progress states — safe to call from any thread."""
        def _update():
            if running:
                self.run_btn.configure(state="disabled")
                self.stop_btn.configure(state="normal")
                self.progress.start()
                self.progress.configure(mode="indeterminate")
            else:
                self.run_btn.configure(state="normal")
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
        self._set_running(True)
        self.status_var.set(f"Starting… (0 / {len(urls)})")
        threading.Thread(target=self._run_batch, args=(urls,), daemon=True).start()

    def _stop(self):
        self._stop_requested = True
        self._log("\n[Stop requested — finishing current download then stopping…]\n")
        self.status_var.set("Stopping…")

    def _run_batch(self, urls):
        from downloader import main as downloader_main

        total = len(urls)
        completed = 0
        failed = 0

        for i, url in enumerate(urls):
            if self._stop_requested:
                self._log(f"\n[Stopped — skipping {total - i} remaining URL(s)]\n")
                self.after(0, lambda: self.status_var.set(
                    f"Stopped — {completed} done, {failed} failed"))
                break

            self.after(0, lambda n=i: self.status_var.set(
                f"Downloading {n + 1} / {total}…"))
            self._log(f"\n{'─' * 60}\n")
            self._log(f"  [{i + 1}/{total}]  {url}\n")
            self._log(f"{'─' * 60}\n\n")

            try:
                argv = ["downloader.py", url, "--disable-ui"]

                dest = self.dest_var.get().strip()
                if dest:
                    argv += ["--custom-path", dest]

                include = self.settings_include.get().strip().split()
                exclude = self.settings_exclude.get().strip().split()
                if include:
                    argv += ["--include"] + include
                if exclude:
                    argv += ["--ignore"] + exclude

                argv += ["--max-retries", str(int(self.retries_slider.get()))]
                if self.opt_no_disk_check.get():
                    argv.append("--disable-disk-check")
                if self.opt_no_dl_folder.get():
                    argv.append("--no-download-folder")

                sys.argv = argv
                asyncio.run(downloader_main())
                completed += 1
                self._log(f"\n  [OK]  Finished: {url}\n")

            except Exception as e:
                failed += 1
                self._log(f"\n  [FAIL]  Error downloading {url}:\n     {e}\n")

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

        self._set_running(False)


if __name__ == "__main__":
    app = DownloaderUI()
    app.mainloop()
