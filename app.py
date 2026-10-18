import os
import queue
import sys
import threading
import time

try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
    from processor import AudioProcessor, resolve_processing_config
    from config_utils import APP_VERSION, load_settings, save_settings
except Exception as exc:
    if "--self-test" in sys.argv:
        import traceback

        traceback.print_exc()
        raise SystemExit(1)
    try:
        import tkinter as _tk
        from tkinter import messagebox as _messagebox

        _root = _tk.Tk()
        _root.withdraw()
        _messagebox.showerror(
            "Audio Cleaner Pro",
            "Не удалось загрузить компоненты приложения:\n"
            f"{exc}\n\n"
            "Запустите install.bat из папки программы "
            "(или установите зависимости: pip install -r requirements.txt).",
        )
        _root.destroy()
    except Exception:
        pass
    raise SystemExit(1)

GLASS_ACCENT = "#5b7cff"
GLASS_ACCENT_HOVER = "#4b6bf8"
GLASS_DANGER = "#ef5d70"
GLASS_SURFACE_LIGHT = "#ffffff"
GLASS_SURFACE_DARK = "#1d2433"
GLASS_BORDER_LIGHT = "#dce5ff"
GLASS_BORDER_DARK = "#40506e"


class ColorLogTextBox(ctk.CTkTextbox):
    TAG_COLORS = {
        "✅ ": "#2ecc71", "💾 ": "#2ecc71", "🎉 ": "#2ecc71",
        "⚠ ": "#f39c12", "ℹ ": "#95a5a6",
        "❌ ": "#e74c3c", "⛔ ": "#e74c3c", "💥 ": "#e74c3c",
        "🐱 ": "#9b59b6", "📟 ": "#9b59b6",
        "📍 ": "#3498db", "📂 ": "#3498db", "🔇 ": "#3498db",
        "🔧 ": "#3498db", "🔍 ": "#3498db", "🎛 ": "#3498db",
        "⏱ ": "#f39c12", "🚫 ": "#e74c3c",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(
            state="disabled",
            font=("Consolas", 12),
            corner_radius=18,
            fg_color=(GLASS_SURFACE_LIGHT, GLASS_SURFACE_DARK),
            border_color=(GLASS_BORDER_LIGHT, GLASS_BORDER_DARK),
            border_width=1,
            text_color=("#243143", "#f8fafc"),
        )
        self._textbox.configure(padx=8, pady=6)
        for prefix, color in self.TAG_COLORS.items():
            self._textbox.tag_config(prefix, foreground=color)

    def append(self, text: str):
        self.configure(state="normal")
        tag = next((p for p in self.TAG_COLORS if text.startswith(p)), None)
        if tag:
            self._textbox.insert("end", text + "\n", tag)
        else:
            self.insert("end", text + "\n")
        self.see("end")
        self.configure(state="disabled")

    def clear(self):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")

    def get_all_text(self) -> str:
        self.configure(state="normal")
        text = self.get("1.0", "end").strip()
        self.configure(state="disabled")
        return text


class AudioCleanerApp(ctk.CTk):

    def _make_glass_frame(self, master, **kwargs):
        kwargs.setdefault("corner_radius", 22)
        kwargs.setdefault("border_width", 1)
        kwargs.setdefault("border_color", (GLASS_BORDER_LIGHT, GLASS_BORDER_DARK))
        kwargs.setdefault("fg_color", (GLASS_SURFACE_LIGHT, GLASS_SURFACE_DARK))
        return ctk.CTkFrame(master, **kwargs)

    def __init__(self):
        super().__init__()
        self.title(f"Audio Cleaner Pro v{APP_VERSION}")
        self.geometry("750x850")
        self.minsize(550, 550)
        self.configure(fg_color=("#f5f7ff", "#0f172a"))
        if os.name == "nt":
            try:
                self.attributes("-alpha", 0.97)
            except Exception:
                pass

        self.input_file = None
        self.processor = AudioProcessor()
        self.cancel_event = threading.Event()
        self.is_processing = False
        self.start_time = None

        self._timer_job = None
        self._timer_active = False
        self._current_progress = 0.0

        self.settings = load_settings()
        ctk.set_appearance_mode(self.settings.get("theme", "system"))

        self._ui_queue = queue.Queue()

        # === ВЕРХНЯЯ ПАНЕЛЬ ===
        top_frame = self._make_glass_frame(self)
        top_frame.pack(fill="x", padx=20, pady=(15, 8))
        top_frame.configure(height=78)
        top_frame.pack_propagate(False)

        ctk.CTkLabel(
            top_frame,
            text="🎙 Audio Cleaner Pro",
            font=ctk.CTkFont(size=20, weight="bold"),
            anchor="w"
        ).pack(side="left", padx=16, pady=12)

        ctk.CTkLabel(
            top_frame,
            text="Быстрая очистка аудио без лишних шагов",
            font=ctk.CTkFont(size=11),
            text_color=("#5f6b7a", "#9fb1c7")
        ).pack(side="right", padx=16, pady=12)

        # === СТАТУС-БАР ===
        status_bar = self._make_glass_frame(self, height=44)
        status_bar.pack(fill="x", padx=20, pady=5)
        status_bar.pack_propagate(False)

        self.status_label = ctk.CTkLabel(status_bar, text="Файл не выбран", text_color="gray", anchor="w")
        self.status_label.pack(side="left", padx=10, fill="x", expand=True)

        self.progress_timer_label = ctk.CTkLabel(
            status_bar, text="", font=ctk.CTkFont(size=13, weight="bold"), width=140, anchor="e"
        )
        self.progress_timer_label.pack(side="right", padx=10)

        # === КНОПКА ЗАГРУЗКИ ===
        self.btn_load = ctk.CTkButton(
            self, text="📂 Загрузить WAV файл", command=self.load_file, height=48,
            corner_radius=18, fg_color=(GLASS_ACCENT, GLASS_ACCENT),
            hover_color=(GLASS_ACCENT_HOVER, GLASS_ACCENT_HOVER),
            border_width=1, border_color=(GLASS_BORDER_LIGHT, GLASS_BORDER_DARK)
        )
        self.btn_load.pack(pady=5, padx=20, fill="x")

        # === ОСНОВНЫЕ ОПЦИИ ===
        options_frame = self._make_glass_frame(self)
        options_frame.pack(fill="x", padx=20, pady=2)

        self.auto_save_var = ctk.BooleanVar(value=self.settings.get("auto_save", False))
        ctk.CTkCheckBox(options_frame, text="💾 Сохранять рядом",
                        variable=self.auto_save_var, font=ctk.CTkFont(size=12)).pack(side="left", padx=12, pady=8)

        self.disable_censor_var = ctk.BooleanVar(
            value=bool(self.settings.get("disable_censor", False) or self.settings.get("censor_mode") == "off")
        )
        self.chk_disable_censor = ctk.CTkCheckBox(
            options_frame, text="🚫 Без цензуры",
            variable=self.disable_censor_var, font=ctk.CTkFont(size=12),
            command=self._on_censor_toggle
        )
        self.chk_disable_censor.pack(side="right", padx=12, pady=8)

        self.preset_var = ctk.StringVar(value=self.settings.get("processing_preset", "balanced"))
        ctk.CTkLabel(options_frame, text="⚙ Пресет:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(12, 6), pady=8)
        self.preset_menu = ctk.CTkOptionMenu(
            options_frame,
            values=["fast", "balanced", "quality"],
            variable=self.preset_var,
            command=lambda *_: self._on_preset_change(),
            width=110,
            height=28,
        )
        self.preset_menu.pack(side="left", pady=8)
        self.preset_hint = ctk.CTkLabel(
            options_frame, text="", font=ctk.CTkFont(size=11), text_color=("#5f6b7a", "#9fb1c7")
        )
        self.preset_hint.pack(side="left", padx=(8, 0), pady=8)

        # === ПРОГРЕСС-БАР ===
        self.progress = ctk.CTkProgressBar(self, height=10, corner_radius=999, progress_color=(GLASS_ACCENT, "#7dd3fc"))
        self.progress.pack(fill="x", padx=20, pady=5)
        self.progress.set(0)

        # === НАСТРОЙКИ ===
        settings_frame = self._make_glass_frame(self)
        settings_frame.pack(fill="x", padx=20, pady=5)

        mode_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        mode_row.pack(fill="x", pady=2)
        ctk.CTkLabel(mode_row, text="Режим:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.censor_mode_var = ctk.StringVar(
            value=self.settings.get("censor_mode") if self.settings.get("censor_mode") in {"beep", "meow"} else "beep"
        )
        self.mode_switch = ctk.CTkSegmentedButton(
            mode_row, values=["beep", "meow"], variable=self.censor_mode_var,
            font=ctk.CTkFont(size=12), width=160, height=28
        )
        self.mode_switch.pack(side="left", padx=(10, 0))

        nr_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        nr_row.pack(fill="x", pady=2)
        ctk.CTkLabel(nr_row, text="Шумодав:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.nr_value_label = ctk.CTkLabel(nr_row, text="0.50", width=35, anchor="e", font=ctk.CTkFont(size=12))
        self.nr_value_label.pack(side="right")
        self.nr_slider = ctk.CTkSlider(nr_row, from_=0.0, to=1.0, number_of_steps=20,
                                        command=self._on_noise_change, height=16)
        self.nr_slider.set(self.settings.get("noise_reduction", 0.5))
        self.nr_value_label.configure(text=f"{self.nr_slider.get():.2f}")
        self.nr_slider.pack(side="right", padx=(10, 5), fill="x", expand=True)

        thresh_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        thresh_row.pack(fill="x", pady=2)
        ctk.CTkLabel(thresh_row, text="Паддинг (мс):", font=ctk.CTkFont(size=12)).pack(side="left")
        self.pad_entry = ctk.CTkEntry(thresh_row, width=60, height=26, font=ctk.CTkFont(size=12))
        self.pad_entry.insert(0, str(self.settings.get("padding_ms", 100)))
        self.pad_entry.pack(side="left", padx=(10, 0))
        self.pad_entry.bind("<KeyRelease>", lambda _event: self._update_preset_hint())
        self.pad_entry.bind("<FocusOut>", lambda _event: self._save_settings())

        # === КНОПКИ ===
        btn_frame = self._make_glass_frame(self)
        btn_frame.pack(fill="x", padx=20, pady=5)
        self.btn_save = ctk.CTkButton(
            btn_frame, text="⚙ Обработать", command=self.save_processed,
            height=42, state="disabled", corner_radius=16,
            fg_color=(GLASS_ACCENT, GLASS_ACCENT), hover_color=(GLASS_ACCENT_HOVER, GLASS_ACCENT_HOVER),
            border_width=1, border_color=(GLASS_BORDER_LIGHT, GLASS_BORDER_DARK)
        )
        self.btn_save.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_export_srt = ctk.CTkButton(
            btn_frame, text="📄 SRT", command=self.export_srt_standalone,
            height=42, width=88, state="disabled", fg_color=("#3b82f6", "#2563eb"),
            hover_color=("#2563eb", "#3b82f6"), corner_radius=16,
            border_width=1, border_color=(GLASS_BORDER_LIGHT, GLASS_BORDER_DARK)
        )
        self.btn_export_srt.pack(side="right", padx=(5, 0))
        self.btn_cancel = ctk.CTkButton(
            btn_frame, text="⛔ Отмена", command=self.cancel_processing,
            height=42, width=100, fg_color=(GLASS_DANGER, GLASS_DANGER), state="disabled",
            hover_color=("#dc4f61", "#f15b74"), corner_radius=16,
            border_width=1, border_color=(GLASS_BORDER_LIGHT, GLASS_BORDER_DARK)
        )
        self.btn_cancel.pack(side="right")

        # === ЛОГ ===
        log_header = self._make_glass_frame(self)
        log_header.pack(fill="x", padx=20, pady=(10, 2))
        ctk.CTkLabel(log_header, text="Журнал:", anchor="w", font=ctk.CTkFont(size=13)).pack(side="left")
        self.btn_copy_log = ctk.CTkButton(log_header, text="📋 Копировать", command=self.copy_log,
                                           height=28, width=110, font=ctk.CTkFont(size=12))
        self.btn_copy_log.pack(side="right")

        self.log_box = ColorLogTextBox(self, height=200)
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        if self.processor.dict_warning:
            self._log(self.processor.dict_warning)
        if self.processor.vad_warning:
            self._log(self.processor.vad_warning)
        if getattr(self.processor, "vosk_warning", None):
            self._log(self.processor.vosk_warning)

        # Применяем начальное состояние чекбокса цензуры
        self._on_censor_toggle()
        self._update_preset_hint()
        self._save_settings()
        self.after(50, self._drain_ui_queue)

    # === НАСТРОЙКИ ===
    def _pad_value(self) -> int:
        try:
            return max(0, int(self.pad_entry.get()))
        except (TypeError, ValueError):
            return 100

    def _on_noise_change(self, value):
        self.nr_value_label.configure(text=f"{value:.2f}")
        self._update_preset_hint()
        self._save_settings()

    def _on_preset_change(self):
        self._update_preset_hint()
        self._save_settings()

    def _update_preset_hint(self):
        try:
            config = resolve_processing_config(
                self.preset_var.get(), self.nr_slider.get(), self._pad_value()
            )
        except (TypeError, ValueError):
            self.preset_hint.configure(text="")
            return
        self.preset_hint.configure(text=f"факт. шумодав: {config['noise_reduction']:.2f}")

    def _save_settings(self):
        data = {
            "theme": self.settings.get("theme", "system"),
            "auto_save": self.auto_save_var.get(),
            "censor_mode": self._get_effective_censor_mode(),
            "noise_reduction": round(self.nr_slider.get(), 2),
            "padding_ms": self._pad_value(),
            "disable_censor": self.disable_censor_var.get(),
            "processing_preset": self.preset_var.get(),
        }
        save_settings(data)

    # === УПРАВЛЕНИЕ СОСТОЯНИЕМ ЦЕНЗУРЫ ===
    def _on_censor_toggle(self):
        disabled = self.disable_censor_var.get()
        state = "disabled" if disabled else "normal"
        self.mode_switch.configure(state=state)
        self._save_settings()

    def _get_effective_censor_mode(self) -> str:
        """Возвращает 'off' если цензура отключена, иначе выбранный режим."""
        if self.disable_censor_var.get():
            return "off"
        return self.censor_mode_var.get()

    # === ТАЙМЕР ===
    def _format_mm_ss(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def _start_timer(self):
        self.start_time = time.time()
        self._timer_active = True
        self._current_progress = 0.0
        self.progress_timer_label.configure(text="0% | 00:00")
        self._tick_timer()

    def _tick_timer(self):
        if not self._timer_active or self.start_time is None:
            return
        elapsed = time.time() - self.start_time
        pct = int(self._current_progress * 100)
        self.progress_timer_label.configure(text=f"{pct}% | {self._format_mm_ss(elapsed)}")
        self._timer_job = self.after(500, self._tick_timer)

    def _update_progress(self, value: float):
        self._current_progress = max(0.0, min(1.0, float(value)))
        self.progress.set(self._current_progress)
        if self.start_time is not None:
            elapsed = time.time() - self.start_time
            self.progress_timer_label.configure(
                text=f"{int(self._current_progress * 100)}% | {self._format_mm_ss(elapsed)}"
            )

    def _stop_timer(self, success=True):
        self._timer_active = False
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None

        if self.start_time:
            elapsed = time.time() - self.start_time
            icon = "✅" if success else "⛔"
            self.progress_timer_label.configure(text=f"{icon} {self._format_mm_ss(elapsed)}")
        self.start_time = None

    # === UI-ОЧЕРЕДЬ И ЛОГ ===
    def _post(self, func, *args):
        """Безопасно передаёт действие из рабочего потока в главный."""
        self._ui_queue.put((func, args))

    def _flush_ui_queue(self):
        try:
            while True:
                func, args = self._ui_queue.get_nowait()
                try:
                    func(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass

    def _drain_ui_queue(self):
        self._flush_ui_queue()
        self.after(50, self._drain_ui_queue)

    def _log(self, message: str):
        self._post(self.log_box.append, message)

    def copy_log(self):
        self._flush_ui_queue()
        text = self.log_box.get_all_text()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            orig = self.btn_copy_log.cget("text")
            self.btn_copy_log.configure(text="✅ Скопировано!")
            self.after(1500, lambda: self.btn_copy_log.configure(text=orig))

    # === ЗАГРУЗКА ФАЙЛА ===
    def _set_input_file(self, path: str):
        if self.is_processing:
            return
        self.input_file = path
        self.processor.last_entries = []
        self.btn_export_srt.configure(state="disabled")
        self.status_label.configure(text=f"✅ {os.path.basename(path)}", text_color="green")
        self.btn_save.configure(state="normal")
        self.progress.set(0)
        self.progress_timer_label.configure(text="")
        self.log_box.clear()
        self._log(f"📂 Файл загружен: {os.path.basename(path)}")

    def load_file(self):
        if self.is_processing:
            return
        path = filedialog.askopenfilename(title="Выберите WAV", filetypes=[("WAV files", "*.wav")])
        if path:
            self._set_input_file(path)

    # === ОТМЕНА ===
    def cancel_processing(self):
        if self.is_processing:
            self.cancel_event.set()
            self.btn_cancel.configure(state="disabled")
            self._log("⏳ Отмена запрошена...")

    # === ОБРАБОТКА ===
    def save_processed(self):
        if not self.input_file or self.is_processing:
            return
        input_abs = os.path.abspath(self.input_file)
        if self.auto_save_var.get():
            base, _ = os.path.splitext(self.input_file)
            output_path = f"{base}_cleaned.wav"
        else:
            output_path = filedialog.asksaveasfilename(
                title="Сохранить", defaultextension=".wav", filetypes=[("WAV files", "*.wav")],
                initialfile=f"cleaned_{os.path.basename(self.input_file)}"
            )
            if not output_path:
                return
            if not output_path.lower().endswith(".wav"):
                output_path += ".wav"

        if os.path.normcase(os.path.abspath(output_path)) == os.path.normcase(input_abs):
            messagebox.showerror("Ошибка", "Нельзя перезаписать исходный файл — выберите другое имя.")
            return

        self._start_processing(output_path)

    def _start_processing(self, output_path: str):
        self.is_processing = True
        self.cancel_event.clear()
        for w in [self.mode_switch, self.btn_load, self.btn_save, self.btn_export_srt,
                   self.nr_slider, self.pad_entry, self.chk_disable_censor, self.preset_menu]:
            w.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.log_box.clear()
        self.progress.set(0)
        self._current_progress = 0.0
        self._start_timer()
        self._save_settings()

        effective_mode = self._get_effective_censor_mode()
        padding_ms = self._pad_value()
        noise_reduction = round(self.nr_slider.get(), 2)
        preset = self.preset_var.get()

        def worker():
            ok, msg, stats = self.processor.process(
                self.input_file, output_path, censor_mode=effective_mode,
                noise_reduction=noise_reduction,
                padding_ms=padding_ms,
                progress_cb=lambda v: self._post(self._update_progress, v),
                log_cb=self._log, cancel_event=self.cancel_event,
                processing_preset=preset,
            )
            self._post(self._on_complete, ok, msg, stats)

        threading.Thread(target=worker, daemon=True).start()

    def _on_complete(self, success: bool, message: str, stats: dict):
        self.is_processing = False
        for w in [self.mode_switch, self.btn_load, self.btn_save, self.nr_slider,
                   self.pad_entry, self.chk_disable_censor, self.preset_menu]:
            w.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self._stop_timer(success=success)
        # Восстанавливаем состояние переключателя режима
        self._on_censor_toggle()

        cancelled = (not success) and (self.cancel_event.is_set() or "прервана" in message.lower())

        if success:
            pc = stats.get('profanity_count', 0)
            muted = stats.get('muted_sec', 0)
            uw = stats.get('unique_words', [])
            elapsed = stats.get('elapsed', 0)

            st_m, st_s = divmod(int(elapsed), 60)
            st_h, st_m = divmod(st_m, 60)
            st_str = f"{st_h}:{st_m:02d}:{st_s:02d}" if st_h > 0 else f"{st_m}:{st_s:02d}"

            mt_m, mt_s = divmod(int(muted), 60)
            mt_h, mt_m = divmod(mt_m, 60)
            mt_str = f"{mt_h}:{mt_m:02d}:{mt_s:02d}" if mt_h > 0 else f"{mt_m}:{mt_s:02d}"

            self._log("")
            self._log("📊 Итог:")
            if uw:
                self._log(f"   • Найдено мата: {pc} слов ({', '.join(uw)})")
            else:
                self._log("   • Мат не найден")
            self._log(f"   • Заменено тишиной: {mt_str}")
            self._log(f"   • Время обработки: {st_str}")
            self._log(f"\n🎉 {message}")
            self.btn_export_srt.configure(state="normal" if pc > 0 else "disabled")
            messagebox.showinfo("Готово", message)
        elif cancelled:
            self._log(f"\n⛔ {message}")
            self.btn_export_srt.configure(state="disabled")
        else:
            self._log(f"\n💥 {message}")
            self.btn_export_srt.configure(state="disabled")
            messagebox.showerror("Ошибка", message)

    # === ЭКСПОРТ SRT ===
    def export_srt_standalone(self):
        if not self.processor.last_entries:
            messagebox.showinfo("Экспорт SRT", "Нет маркеров для экспорта")
            return
        path = filedialog.asksaveasfilename(
            title="Экспорт SRT", defaultextension=".srt", filetypes=[("SRT files", "*.srt")],
            initialfile=f"markers_{os.path.basename(self.input_file or 'audio.wav')}.srt"
        )
        if path:
            self.processor.export_srt(path, censor_mode=self._get_effective_censor_mode(), log_cb=self._log)
            messagebox.showinfo("Экспорт SRT", f"Маркеры сохранены:\n{path}")


def self_test() -> int:
    """Проверка упакованной сборки без GUI: Vosk, Silero, полный process()."""
    import tempfile

    import numpy as np
    import soundfile as sf

    from config_utils import APP_VERSION
    from processor import AudioProcessor

    print(f"AudioCleanerPro {APP_VERSION} self-test")
    processor = AudioProcessor()
    if processor.vosk_model is None:
        print(f"vosk: FAIL ({processor.vosk_warning})")
        return 1
    if processor.vad_model is None:
        print(f"silero: FAIL ({processor.vad_warning})")
        return 1
    print(f"vosk: {processor.vosk_model_name}; words: {len(processor.profanity_set)}")

    sample_rate = 16000
    seconds = 3
    t = np.arange(sample_rate * seconds) / sample_rate
    signal = (0.05 * np.sin(2 * np.pi * 180 * t) * (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 1.5 * t)))).astype(np.float32)

    with tempfile.TemporaryDirectory() as work_dir:
        input_path = os.path.join(work_dir, "self_test_in.wav")
        output_path = os.path.join(work_dir, "self_test_out.wav")
        sf.write(input_path, signal, sample_rate)
        ok, message, stats = processor.process(
            input_path, output_path, censor_mode="beep", noise_reduction=0.5,
            padding_ms=100, processing_preset="fast", log_cb=lambda _message: None,
        )
        if not ok or not os.path.exists(output_path):
            print(f"process: FAIL ({message})")
            return 1
        result, result_sr = sf.read(output_path, dtype="float32")
        if result_sr != sample_rate or len(result) != len(signal):
            print("output: FAIL (sample rate or length mismatch)")
            return 1
        print(f"process: OK ({len(result)} samples, {stats.get('elapsed', 0):.2f}s)")

    print("self-test OK")
    return 0


def main() -> int:
    try:
        application = AudioCleanerApp()
    except Exception as exc:  # модель/словарь не найдены и т.п.
        root = ctk.CTk()
        root.withdraw()
        messagebox.showerror("Audio Cleaner Pro", f"Не удалось запустить приложение:\n{exc}")
        root.destroy()
        return 1
    application.mainloop()
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    raise SystemExit(main())
