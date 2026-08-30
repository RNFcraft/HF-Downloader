from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .downloader import (
    DownloadEvent, DownloadManager, FileProgress, configure_hub_environment, format_bytes, format_duration,
    list_repository_files, TRANSPORT_LABELS,
)
from .models import InvalidHuggingFaceURL, destination_for, parse_huggingface_source

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DESTINATION = Path(r"D:\projects\aiagent\hf_models")
SETTINGS_FILE = APP_DIR / "settings.json"
TRANSPORT_KEYS_BY_LABEL = {label: key for key, label in TRANSPORT_LABELS.items()}
COLORS = {
    "bg": "#0b0f14", "panel": "#111821", "panel2": "#151e29", "border": "#263445",
    "text": "#e7edf5", "muted": "#8fa1b5", "accent": "#65d6ad",
    "danger": "#ff8994", "input": "#0d141d",
}


def currently_downloading(files: tuple[FileProgress, ...]) -> tuple[FileProgress, ...]:
    """Return only files that have not reached their known final size yet."""
    return tuple(item for item in files if item.total <= 0 or item.downloaded < item.total)


class HfDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        configure_hub_environment()
        self.title("HF Downloader — модели и датасеты")
        window_width = min(1060, self.winfo_screenwidth() - 40)
        window_height = min(930, self.winfo_screenheight() - 80)
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(760, 620)
        self.configure(bg=COLORS["bg"])
        self.events: queue.Queue[DownloadEvent] = queue.Queue()
        self.manager = DownloadManager(self.events)
        self.file_events: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.available_files: dict[str, int] = {}
        self.selected_files: set[str] = set()
        self.file_load_generation = 0
        self.file_load_job: str | None = None
        self.loaded_source_key: tuple[str, str, str, str | None] | None = None
        self.filtered_file_count = 0
        self.file_display_suffix = ""
        self._load_variables()
        self._configure_styles()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(120, self._poll_events)

    def _load_variables(self) -> None:
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            stored = {}
        settings_version = int(stored.get("settings_version", 0))
        self.url_var = tk.StringVar()
        self.destination_var = tk.StringVar(value=str(stored.get("destination", DEFAULT_DESTINATION)))
        self.type_var = tk.StringVar(value=str(stored.get("repo_type", "auto")))
        self.subfolder_var = tk.BooleanVar(value=bool(stored.get("create_subfolder", True)))
        self.workers_var = tk.IntVar(value=max(1, min(int(stored.get("workers", 4)) if settings_version >= 2 else 4, 8)))
        self.retries_var = tk.IntVar(value=max(0, min(int(stored.get("retries", 6)) if settings_version >= 2 else 6, 10)))
        self.timeout_var = tk.IntVar(value=max(60, min(int(stored.get("stall_timeout", 600)) if settings_version >= 2 else 600, 1800)))
        stored_transport = str(stored.get("transport", "auto")) if settings_version >= 2 else "auto"
        self.transport_var = tk.StringVar(value=TRANSPORT_LABELS.get(stored_transport, "Auto"))
        self.workers_warning_var = tk.StringVar(value="")
        self.token_var = tk.StringVar()
        self.exclude_var = tk.StringVar(value=str(stored.get("exclude", "")))
        self.status_var = tk.StringVar(value="Готов к работе")
        self.source_info_var = tk.StringVar(value="Вставьте ссылку — тип определится автоматически.")
        self.target_var = tk.StringVar(value="—")
        self.progress_var = tk.DoubleVar()
        self.progress_text_var = tk.StringVar(value="0 Б из —")
        self.speed_var = tk.StringVar(value="—")
        self.average_speed_var = tk.StringVar(value="—")
        self.eta_var = tk.StringVar(value="—")
        self.files_var = tk.StringVar(value="—")
        self.attempt_var = tk.StringVar(value="—")
        self.active_transport_var = tk.StringVar(value="—")
        self.current_file_var = tk.StringVar(value="—")
        self.heartbeat_var = tk.StringVar(value="—")
        self.active_files_var = tk.StringVar(value="Файлы ещё не запущены.")
        self.file_search_var = tk.StringVar()
        self.file_status_var = tk.StringVar(value="Введите ссылку, чтобы получить список файлов.")
        self.workers_var.trace_add("write", lambda *_args: self._update_workers_warning())

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["panel"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("Segoe UI Semibold", 21))
        style.configure("Subtitle.TLabel", background=COLORS["bg"], foreground=COLORS["muted"])
        style.configure("Section.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI Semibold", 12))
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("TEntry", fieldbackground=COLORS["input"], foreground=COLORS["text"], insertcolor=COLORS["text"], bordercolor=COLORS["border"], padding=8)
        style.configure("TCombobox", fieldbackground=COLORS["input"], foreground=COLORS["text"], arrowcolor=COLORS["muted"], bordercolor=COLORS["border"], padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", COLORS["input"])], foreground=[("readonly", COLORS["text"])])
        style.configure("TCheckbutton", background=COLORS["panel"], foreground=COLORS["muted"])
        style.map("TCheckbutton", background=[("active", COLORS["panel"])], foreground=[("active", COLORS["text"])])
        style.configure("TButton", background=COLORS["panel2"], foreground=COLORS["text"], bordercolor=COLORS["border"], padding=(12, 8), font=("Segoe UI Semibold", 9))
        style.map("TButton", background=[("active", "#1b2836"), ("disabled", COLORS["panel"])], foreground=[("disabled", "#546273")])
        style.configure("Accent.TButton", background=COLORS["accent"], foreground="#07120e", bordercolor=COLORS["accent"], padding=(18, 10), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#7be3bd"), ("disabled", "#25483d")])
        style.configure("Danger.TButton", background="#332029", foreground=COLORS["danger"], bordercolor="#61313c")
        style.configure("Horizontal.TProgressbar", troughcolor=COLORS["input"], background=COLORS["accent"], bordercolor=COLORS["input"])
        style.configure("Repo.Treeview", background=COLORS["input"], fieldbackground=COLORS["input"],
                        foreground=COLORS["text"], bordercolor=COLORS["border"], rowheight=25, font=("Segoe UI", 9))
        style.configure("Repo.Treeview.Heading", background=COLORS["panel2"], foreground=COLORS["muted"],
                        bordercolor=COLORS["border"], font=("Segoe UI Semibold", 9))
        style.map("Repo.Treeview", background=[("selected", "#1d3850")], foreground=[("selected", COLORS["text"])])

    def _build_ui(self) -> None:
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell, padding=(24, 16, 24, 14))
        header.pack(fill="x")
        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="HF Downloader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Надёжная загрузка моделей и датасетов Hugging Face с продолжением после сбоев", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 0))
        header_actions = ttk.Frame(header)
        header_actions.pack(side="right", padx=(15, 0))
        button_row = ttk.Frame(header_actions)
        button_row.pack(anchor="e")
        self.start_button = ttk.Button(
            button_row, text="Скачать выбранное (0)", style="Accent.TButton",
            command=self._start, state="disabled",
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            button_row, text="Остановить", style="Danger.TButton",
            command=self._stop, state="disabled",
        )
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(header_actions, textvariable=self.status_var, style="Subtitle.TLabel").pack(anchor="e", pady=(5, 0))

        body = ttk.Frame(shell)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=COLORS["bg"], highlightthickness=0, bd=0)
        self.page_canvas = canvas
        page_scroll = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(canvas, padding=(24, 2, 16, 20))
        page_window = canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(page_window, width=event.width))
        canvas.bind_all("<MouseWheel>", self._scroll_page)

        source = self._card(outer, "1 · Что скачать", "Ссылка, hf:// URI или ID author/repository")
        url_row = ttk.Frame(source, style="Card.TFrame")
        url_row.pack(fill="x", pady=(11, 7))
        self.url_entry = ttk.Entry(url_row, textvariable=self.url_var, font=("Segoe UI", 10))
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<KeyRelease>", lambda _event: self._on_source_changed())
        ttk.Button(url_row, text="Вставить", command=self._paste).pack(side="left", padx=(8, 0))
        opts = ttk.Frame(source, style="Card.TFrame")
        opts.pack(fill="x")
        ttk.Label(opts, text="Тип", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        type_box = ttk.Combobox(opts, textvariable=self.type_var, state="readonly", width=13,
                                values=("auto", "model", "dataset", "space"))
        type_box.pack(side="left")
        type_box.bind("<<ComboboxSelected>>", lambda _event: self._on_source_changed(immediate=True))
        ttk.Label(opts, textvariable=self.source_info_var, style="Muted.TLabel", wraplength=660).pack(side="left", padx=(14, 0))

        files = self._card(outer, "2 · Выбор файлов", "Галочка означает, что файл будет загружен")
        file_tools = ttk.Frame(files, style="Card.TFrame")
        file_tools.pack(fill="x", pady=(10, 7))
        search = ttk.Entry(file_tools, textvariable=self.file_search_var)
        search.pack(side="left", fill="x", expand=True)
        search.insert(0, "")
        self.file_search_var.trace_add("write", lambda *_args: self._render_file_list())
        ttk.Button(file_tools, text="Обновить", command=lambda: self._schedule_repository_load(0)).pack(side="left", padx=(7, 0))
        ttk.Button(file_tools, text="Выбрать всё", command=self._select_all_files).pack(side="left", padx=(7, 0))
        ttk.Button(file_tools, text="Снять всё", command=self._clear_file_selection).pack(side="left", padx=(7, 0))
        tree_wrap = ttk.Frame(files, style="Card.TFrame")
        tree_wrap.pack(fill="both", expand=True)
        self.file_tree = ttk.Treeview(
            tree_wrap, columns=("checked", "path", "size"), show="headings",
            style="Repo.Treeview", height=6, selectmode="browse",
        )
        self.file_tree.heading("checked", text="СКАЧАТЬ")
        self.file_tree.heading("path", text="ФАЙЛ В РЕПОЗИТОРИИ")
        self.file_tree.heading("size", text="РАЗМЕР")
        self.file_tree.column("checked", width=75, minwidth=70, stretch=False, anchor="center")
        self.file_tree.column("path", width=720, minwidth=250)
        self.file_tree.column("size", width=110, minwidth=90, stretch=False, anchor="e")
        file_scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        self.file_tree.pack(side="left", fill="both", expand=True)
        file_scroll.pack(side="right", fill="y")
        self.file_tree.bind("<Button-1>", self._toggle_file)
        ttk.Label(files, textvariable=self.file_status_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        target = self._card(outer, "3 · Куда установить", r"По умолчанию: D:\projects\aiagent\hf_models")
        dest_row = ttk.Frame(target, style="Card.TFrame")
        dest_row.pack(fill="x", pady=(11, 7))
        ttk.Entry(dest_row, textvariable=self.destination_var).pack(side="left", fill="x", expand=True)
        ttk.Button(dest_row, text="Выбрать папку", command=self._choose_folder).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(target, text="Создавать отдельную папку author--repository", variable=self.subfolder_var,
                        command=self._preview_source).pack(anchor="w")
        ttk.Label(target, textvariable=self.target_var, style="Muted.TLabel", wraplength=850).pack(anchor="w", pady=(5, 0))

        advanced = self._card(outer, "4 · Надёжность и доступ", "Token нужен только для приватных репозиториев и не сохраняется")
        transport_row = ttk.Frame(advanced, style="Card.TFrame")
        transport_row.pack(fill="x", pady=(10, 2))
        ttk.Label(transport_row, text="ТРАНСПОРТ", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        transport_box = ttk.Combobox(
            transport_row,
            textvariable=self.transport_var,
            state="readonly",
            width=20,
            values=("Auto", "Xet Adaptive", "Xet Conservative", "Plain HTTP"),
        )
        transport_box.pack(side="left")
        ttk.Label(
            transport_row,
            text="Auto: Xet Adaptive, после двух подтверждённых stall — продолжение через Plain HTTP",
            style="Muted.TLabel",
        ).pack(side="left", padx=(12, 0))
        grid = ttk.Frame(advanced, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        for column in range(4):
            grid.columnconfigure(column, weight=1)
        self._labeled_entry(grid, "HF TOKEN (НЕОБЯЗАТЕЛЬНО)", self.token_var, 0, show="•")
        self._labeled_spin(grid, "ПАРАЛЛЕЛЬНЫЕ ФАЙЛЫ", self.workers_var, 1, 1, 8)
        self._labeled_spin(grid, "ПОВТОРЫ ПОСЛЕ СБОЯ", self.retries_var, 2, 0, 10)
        self._labeled_spin(grid, "ТАЙМАУТ БЕЗ ПРОГРЕССА, С", self.timeout_var, 3, 60, 1800)
        ttk.Label(advanced, text="Исключить (маски через запятую)", style="Muted.TLabel").pack(anchor="w", pady=(10, 3))
        ttk.Entry(advanced, textvariable=self.exclude_var).pack(fill="x")
        ttk.Label(advanced, textvariable=self.workers_warning_var, style="Muted.TLabel").pack(anchor="w", pady=(5, 0))

        progress = self._card(outer, "", "")
        progress.pack_configure(fill="both", expand=True)
        top = ttk.Frame(progress, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, textvariable=self.status_var, style="Section.TLabel").pack(side="left")
        ttk.Label(top, textvariable=self.progress_text_var, style="Muted.TLabel").pack(side="right")
        ttk.Progressbar(progress, variable=self.progress_var, maximum=100).pack(fill="x", pady=(10, 12))
        metrics = ttk.Frame(progress, style="Card.TFrame")
        metrics.pack(fill="x")
        for column, (name, variable) in enumerate((("СКОРОСТЬ", self.speed_var), ("ОСТАЛОСЬ", self.eta_var), ("ФАЙЛЫ", self.files_var), ("ПОПЫТКА", self.attempt_var))):
            inner = tk.Frame(metrics, bg=COLORS["panel2"], highlightbackground=COLORS["border"], highlightthickness=1, padx=10, pady=7)
            inner.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            tk.Label(inner, text=name, bg=COLORS["panel2"], fg=COLORS["muted"], font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(inner, textvariable=variable, bg=COLORS["panel2"], fg=COLORS["text"], font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(2, 0))
            metrics.columnconfigure(column, weight=1)
        metrics_extra = ttk.Frame(progress, style="Card.TFrame")
        metrics_extra.pack(fill="x", pady=(6, 0))
        for column, (name, variable) in enumerate((
            ("ТРАНСПОРТ", self.active_transport_var),
            ("СРЕДНЯЯ 60 С", self.average_speed_var),
            ("HEARTBEAT", self.heartbeat_var),
            ("ТЕКУЩИЙ ФАЙЛ", self.current_file_var),
        )):
            inner = tk.Frame(metrics_extra, bg=COLORS["panel2"], highlightbackground=COLORS["border"], highlightthickness=1, padx=10, pady=7)
            inner.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0))
            tk.Label(inner, text=name, bg=COLORS["panel2"], fg=COLORS["muted"], font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(inner, textvariable=variable, bg=COLORS["panel2"], fg=COLORS["text"], font=("Segoe UI Semibold", 9), anchor="w").pack(anchor="w", fill="x", pady=(2, 0))
            metrics_extra.columnconfigure(column, weight=1)
        active_files_box = tk.Frame(
            progress, bg=COLORS["input"], highlightbackground=COLORS["border"],
            highlightthickness=1, padx=10, pady=8,
        )
        active_files_box.pack(fill="x", pady=(8, 0))
        tk.Label(
            active_files_box, text="АКТИВНЫЕ ФАЙЛЫ", bg=COLORS["input"],
            fg=COLORS["muted"], font=("Segoe UI", 8),
        ).pack(anchor="w")
        tk.Label(
            active_files_box, textvariable=self.active_files_var, bg=COLORS["input"],
            fg=COLORS["text"], font=("Cascadia Mono", 9), justify="left",
            anchor="w", wraplength=900,
        ).pack(anchor="w", fill="x", pady=(4, 0))
        self.log = tk.Text(progress, height=3, bg=COLORS["input"], fg=COLORS["muted"], insertbackground=COLORS["text"],
                           relief="flat", bd=0, padx=10, pady=8, font=("Cascadia Mono", 9), wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, pady=(12, 10))
        actions = ttk.Frame(progress, style="Card.TFrame")
        actions.pack(fill="x")
        ttk.Button(actions, text="Открыть папку", command=self._open_folder).pack(side="right")
        ttk.Button(actions, text="Как это работает?", command=self._show_help).pack(side="right", padx=(0, 8))
        self.url_entry.focus_set()
        self._preview_source()
        self._update_workers_warning()

    def _card(self, parent: ttk.Frame, title: str, detail: str) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=17)
        frame.pack(fill="x", pady=(0, 12))
        if title:
            row = ttk.Frame(frame, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Label(row, text=title, style="Section.TLabel").pack(side="left")
            ttk.Label(row, text=detail, style="Muted.TLabel").pack(side="right")
        return frame

    def _scroll_page(self, event: tk.Event) -> str | None:
        if hasattr(self, "file_tree") and event.widget == self.file_tree:
            return None
        self.page_canvas.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _labeled_entry(self, parent: ttk.Frame, title: str, variable: tk.Variable, column: int, **kwargs: object) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 5 if column < 3 else 0))
        ttk.Label(frame, text=title, style="Muted.TLabel", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 3))
        ttk.Entry(frame, textvariable=variable, **kwargs).pack(fill="x")

    def _labeled_spin(self, parent: ttk.Frame, title: str, variable: tk.Variable, column: int, start: int, end: int) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 5 if column < 3 else 0))
        ttk.Label(frame, text=title, style="Muted.TLabel", font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 3))
        tk.Spinbox(frame, from_=start, to=end, textvariable=variable, bg=COLORS["input"], fg=COLORS["text"],
                   buttonbackground=COLORS["panel2"], insertbackground=COLORS["text"], relief="flat", bd=0,
                   highlightthickness=1, highlightbackground=COLORS["border"], font=("Segoe UI", 10)).pack(fill="x", ipady=7)

    def _update_workers_warning(self) -> None:
        try:
            workers = int(self.workers_var.get())
        except (tk.TclError, ValueError):
            workers = 4
        self.workers_warning_var.set(
            "Внимание: больше 4 workers на HDD может снизить скорость из-за одновременной записи."
            if workers > 4
            else "HDD-профиль: последовательная Xet-реконструкция, рекомендуются 2–4 workers."
        )

    def _paste(self) -> None:
        try:
            self.url_var.set(self.clipboard_get().strip())
            self._on_source_changed(immediate=True)
        except tk.TclError:
            self.bell()

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.destination_var.get() or str(DEFAULT_DESTINATION), mustexist=False)
        if selected:
            self.destination_var.set(selected)
            self._preview_source()

    def _source(self):
        return parse_huggingface_source(self.url_var.get(), self.type_var.get())

    @staticmethod
    def _source_key(source) -> tuple[str, str, str, str | None]:
        return source.repo_id, source.repo_type, source.revision, source.path

    def _destination(self) -> Path:
        return destination_for(self.destination_var.get(), self._source(), self.subfolder_var.get())

    def _preview_source(self) -> None:
        try:
            source = self._source()
            scope = "весь репозиторий" if not source.path else (
                ("файл " if source.path_kind == "file" else "папка ") + source.path
            )
            self.source_info_var.set(f"{source.repo_type} · {source.repo_id} · revision {source.revision} · {scope}")
            self.target_var.set("Будет установлено в: " + str(
                destination_for(self.destination_var.get(), source, self.subfolder_var.get())
            ))
        except (InvalidHuggingFaceURL, OSError):
            self.source_info_var.set("Вставьте ссылку — тип определится автоматически.")
            self.target_var.set("—")

    def _on_source_changed(self, immediate: bool = False) -> None:
        self._preview_source()
        self.loaded_source_key = None
        self.available_files.clear()
        self.selected_files.clear()
        self._render_file_list()
        self._schedule_repository_load(0 if immediate else 750)

    def _schedule_repository_load(self, delay_ms: int = 0) -> None:
        if self.file_load_job:
            self.after_cancel(self.file_load_job)
            self.file_load_job = None
        try:
            self._source()
        except InvalidHuggingFaceURL:
            self.file_status_var.set("Введите полную ссылку или ID репозитория.")
            return
        self.file_load_generation += 1
        generation = self.file_load_generation
        self.file_status_var.set("Подготовка запроса списка файлов…")
        if hasattr(self, "start_button") and not self.manager.active:
            self.start_button.configure(state="disabled")
        self.file_load_job = self.after(delay_ms, lambda: self._begin_repository_load(generation))

    def _begin_repository_load(self, generation: int) -> None:
        self.file_load_job = None
        try:
            source = self._source()
        except InvalidHuggingFaceURL:
            return
        token = self.token_var.get().strip() or None
        self.file_status_var.set("Получение дерева репозитория без скачивания содержимого…")

        def load() -> None:
            try:
                result = list_repository_files(source, token)
                self.file_events.put((generation, "complete", (source, result)))
            except Exception as exc:
                self.file_events.put((generation, "error", str(exc)))

        threading.Thread(target=load, daemon=True, name="hf-repository-browser").start()

    def _handle_file_event(self, event: tuple[int, str, object]) -> None:
        generation, kind, payload = event
        if generation != self.file_load_generation:
            return
        if kind == "error":
            self.available_files.clear()
            self.selected_files.clear()
            self.loaded_source_key = None
            self.file_status_var.set("Не удалось получить список: " + str(payload)[:500])
            self._render_file_list()
            return
        source, files = payload
        self.available_files = {item.path: item.size for item in files}
        self.selected_files = set(self.available_files)
        self.loaded_source_key = self._source_key(source)
        self._render_file_list()

    def _render_file_list(self) -> None:
        if not hasattr(self, "file_tree"):
            return
        self.file_tree.delete(*self.file_tree.get_children())
        query = self.file_search_var.get().strip().casefold()
        matches = [
            (path, size) for path, size in self.available_files.items()
            if not query or query in path.casefold()
        ]
        display_limit = 5000
        for index, (path, size) in enumerate(matches[:display_limit]):
            checked = "☑" if path in self.selected_files else "☐"
            self.file_tree.insert("", "end", iid=f"file-{index}", values=(checked, path, format_bytes(size)))
        suffix = f" · показаны первые {display_limit}" if len(matches) > display_limit else ""
        self.filtered_file_count = len(matches)
        self.file_display_suffix = suffix
        self._update_file_summary(len(matches), suffix)

    def _update_file_summary(self, matches: int | None = None, suffix: str = "") -> None:
        selected_size = sum(self.available_files[path] for path in self.selected_files if path in self.available_files)
        if self.available_files:
            visible = matches if matches is not None else self.filtered_file_count
            detail_suffix = suffix if matches is not None else self.file_display_suffix
            self.file_status_var.set(
                f"Выбрано {len(self.selected_files)} из {len(self.available_files)} · "
                f"{format_bytes(selected_size)} · найдено по фильтру {visible}{detail_suffix}"
            )
        elif self.loaded_source_key is not None:
            self.file_status_var.set("В выбранной области репозитория файлы не найдены.")
        if hasattr(self, "start_button") and not self.manager.active:
            self.start_button.configure(state="normal" if self.selected_files else "disabled")
            self.start_button.configure(text=f"Скачать выбранное ({len(self.selected_files)})")

    def _toggle_file(self, event: tk.Event) -> None:
        if self.file_tree.identify_column(event.x) != "#1":
            return
        item_id = self.file_tree.identify_row(event.y)
        if not item_id:
            return
        values = self.file_tree.item(item_id, "values")
        if len(values) < 2:
            return
        path = str(values[1])
        if path in self.selected_files:
            self.selected_files.remove(path)
        else:
            self.selected_files.add(path)
        changed = list(values)
        changed[0] = "☑" if path in self.selected_files else "☐"
        self.file_tree.item(item_id, values=changed)
        self._update_file_summary()
        return "break"

    def _select_all_files(self) -> None:
        self.selected_files = set(self.available_files)
        self._render_file_list()

    def _clear_file_selection(self) -> None:
        self.selected_files.clear()
        self._render_file_list()

    def _start(self) -> None:
        try:
            if not self.destination_var.get().strip():
                raise ValueError("Выберите папку назначения.")
            source = self._source()
            if self.loaded_source_key != self._source_key(source):
                raise ValueError("Дождитесь загрузки актуального списка файлов репозитория.")
            if not self.selected_files:
                raise ValueError("Отметьте хотя бы один файл для скачивания.")
            destination = self._destination()
            workers = max(1, min(int(self.workers_var.get()), 8))
            retries = max(0, min(int(self.retries_var.get()), 10))
            timeout = max(60, min(int(self.timeout_var.get()), 1800))
            transport = TRANSPORT_KEYS_BY_LABEL.get(self.transport_var.get(), "auto")
            excludes = [part.strip() for part in self.exclude_var.get().split(",") if part.strip()]
            self._save_settings(workers, retries, timeout, transport)
            self._reset_progress()
            self._append_log(f"Источник: {source.repo_type}/{source.repo_id} ({source.revision})")
            self._append_log(f"Папка: {destination}")
            self._append_log(f"Транспорт: {self.transport_var.get()} · workers {workers} · stall {timeout} с")
            self.manager.start(
                source, destination, token=self.token_var.get().strip() or None, workers=workers,
                retries=retries, stall_timeout=timeout, ignore_patterns=excludes or None,
                selected_files=sorted(self.selected_files), transport=transport,
            )
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.url_entry.configure(state="disabled")
        except (InvalidHuggingFaceURL, ValueError, OSError) as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)

    def _stop(self) -> None:
        if self.manager.active:
            self.stop_button.configure(state="disabled")
            threading.Thread(target=self.manager.cancel, daemon=True, name="hf-gui-cancel").start()

    def _poll_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                self._handle_file_event(self.file_events.get_nowait())
        except queue.Empty:
            pass
        self.after(120, self._poll_events)

    def _handle_event(self, event: DownloadEvent) -> None:
        if event.message:
            self.status_var.set(event.message)
        if event.kind in {"status", "plan", "retry", "fallback", "complete", "cancelled", "error"} and event.message:
            self._append_log(event.message)
        if event.total:
            percent = min(100.0, event.downloaded / event.total * 100)
            self.progress_var.set(percent)
            self.progress_text_var.set(
                f"{format_bytes(event.downloaded)} из {format_bytes(event.total)} · {percent:.1f}%"
            )
        if event.kind == "progress":
            self.speed_var.set(format_bytes(event.speed) + "/с")
            self.average_speed_var.set(format_bytes(event.average_speed) + "/с")
            self.current_file_var.set(event.current_file[-80:] if event.current_file else "activity без имени файла")
            downloading_files = currently_downloading(event.active_files)
            if downloading_files:
                lines = []
                for item in downloading_files[:8]:
                    percent = (item.downloaded / item.total * 100) if item.total else 0.0
                    lines.append(
                        f"• {item.path} — {format_bytes(item.downloaded)} из "
                        f"{format_bytes(item.total)} · {percent:.1f}%"
                    )
                if len(downloading_files) > 8:
                    lines.append(f"…и ещё {len(downloading_files) - 8}")
                self.active_files_var.set("\n".join(lines))
            else:
                self.active_files_var.set("Нет файлов, загружаемых прямо сейчас.")
        if event.transport:
            self.active_transport_var.set(event.transport)
        if event.heartbeat_age is not None:
            state = "жив" if event.worker_alive else "устарел"
            self.heartbeat_var.set(f"{event.heartbeat_age:.1f} с · {state}")
        if event.eta is not None:
            self.eta_var.set(format_duration(event.eta))
        if event.files_total:
            self.files_var.set(f"{event.files_done} / {event.files_total}")
        if event.attempt:
            self.attempt_var.set(str(event.attempt))
        if event.kind == "plan":
            percent = (event.downloaded / event.total * 100) if event.total else 0
            self.progress_text_var.set(
                f"{format_bytes(event.downloaded)} из {format_bytes(event.total)} · {percent:.1f}%"
            )
            self.files_var.set(f"{event.files_done} / {event.files_total}")
        if event.kind == "complete":
            self.progress_var.set(100)
            self.speed_var.set("Готово")
            self.eta_var.set("0 с")
            self._finish_controls()
            messagebox.showinfo("Загрузка завершена", event.message, parent=self)
        elif event.kind in {"cancelled", "error"}:
            self._finish_controls()
            if event.kind == "error":
                messagebox.showerror("Не удалось скачать", event.message, parent=self)

    def _finish_controls(self) -> None:
        self.start_button.configure(state="normal" if self.selected_files else "disabled")
        self.stop_button.configure(state="disabled")
        self.url_entry.configure(state="normal")

    def _reset_progress(self) -> None:
        self.progress_var.set(0)
        self.progress_text_var.set("Подготовка…")
        self.speed_var.set("—")
        self.average_speed_var.set("—")
        self.eta_var.set("—")
        self.files_var.set("—")
        self.attempt_var.set("—")
        self.active_transport_var.set("—")
        self.current_file_var.set("—")
        self.heartbeat_var.set("—")
        self.active_files_var.set("Ожидание запуска worker…")

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_folder(self) -> None:
        try:
            path = Path(self.destination_var.get()).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def _show_help(self) -> None:
        messagebox.showinfo(
            "Как работает HF Downloader",
            "1. Вставьте ссылку на модель, датасет, отдельный файл или папку Hugging Face.\n\n"
            "2. Дождитесь списка файлов. Нажимайте на квадратик в столбце «Скачать», чтобы включить или исключить файл.\n\n"
            "3. Поиск фильтрует длинный список. «Выбрать всё» и «Снять всё» применяются ко всему репозиторию.\n\n"
            "4. Выберите папку. Программа проверит размер только отмеченных файлов и свободное место.\n\n"
            "5. После сетевого сбоя выполняются повторы. Готовые и частичные данные сохраняются.\n\n"
            "6. Остановка безопасна. Для продолжения используйте ту же ссылку, выбор файлов и папку.\n\n"
            "Token нужен для private/gated репозиториев. Он не записывается в настройки.",
            parent=self,
        )

    def _save_settings(self, workers: int, retries: int, timeout: int, transport: str) -> None:
        data = {
            "settings_version": 2,
            "destination": self.destination_var.get(), "repo_type": self.type_var.get(),
            "create_subfolder": self.subfolder_var.get(), "workers": workers,
            "retries": retries, "stall_timeout": timeout, "transport": transport,
            "exclude": self.exclude_var.get(),
        }
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _close(self) -> None:
        if self.manager.active:
            if not messagebox.askyesno(
                "Загрузка выполняется",
                "Остановить загрузку и закрыть приложение? Частичные данные сохранятся.",
                parent=self,
            ):
                return
            self.status_var.set("Остановка worker перед закрытием…")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self.update_idletasks()
            self.manager.shutdown(timeout=12)
        self.destroy()


def run() -> None:
    HfDownloaderApp().mainloop()
