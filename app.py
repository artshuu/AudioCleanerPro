import customtkinter as ctk
from tkinter import filedialog, messagebox
import threading
import os
import time
import json
from processor import AudioProcessor


class ColorLogTextBox(ctk.CTkTextbox):
    TAG_COLORS = {
        "✅": "#2ecc71", "💾": "#2ecc71", "🎉": "#2ecc71",
        "⚠": "#f39c12", "ℹ": "#95a5a6",
        "❌": "#e74c3c", "⛔": "#e74c3c", "💥": "#e74c3c",
        "🐱": "#9b59b6", "📟": "#9b59b6",
        "📍": "#3498db", "📂": "#3498db", "🔇": "#3498db",
        "🔧": "#3498db", "🔍": "#3498db", "🎛": "#3498db",
        "⏱": "#f39c12",
    }

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(state="disabled", font=("Consolas", 12))
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


HELP_TEXT = """📖 СПРАВКА ПО AUDIO CLEANER PRO

═══════════════════════════════════════
📂 ЗАГРУЗКА И СОХРАНЕНИЕ
═══════════════════════════════════════
📂 Загрузить WAV файл
  Открывает диалог выбора WAV-файла для обработки.
  Поддерживаются файлы любой длительности и частоты
  дискретизации (8000–48000 Гц). Стерео автоматически
  конвертируется в моно.

💾 Сохранять рядом с оригиналом
  Если включено — обработанный файл сохраняется
  автоматически в ту же папку с суффиксом _cleaned.
  Диалог сохранения не появляется.
  Если выключено — каждый раз запрашивается путь
  через диалог «Сохранить как».

═══════════════════════════════════════
⚙ НАСТРОЙКИ ОБРАБОТКИ
═══════════════════════════════════════
Режим (beep / meow)
  beep — заменяет мат на тональный сигнал 1000 Гц.
  meow — заменяет мат на синтезированный звук мяуканья
         с нисходящей частотой 800→400 Гц.

Шумодав (0.0 – 1.0)
  Сила шумоподавления. Определяет, насколько агрессивно
  удаляется фоновый шум.
  • 0.0–0.3  Мягкое: минимальное воздействие на голос,
             подходит для чистых студийных записей.
  • 0.3–0.6  Нормальное: баланс между чистотой и
             сохранением тембра. Рекомендуется по умолчанию.
  • 0.6–1.0  Агрессивное: сильное подавление шума,
             может слегка «роботизировать» голос.
             Используйте для записей с сильным фоном.

Чанк (размер блока цензуры)
  Размер порции аудио (в байтах), которая подаётся
  в модель распознавания за один раз.
  • 8000  — стандартное значение, оптимально для
            small-ru-0.22. Не меняйте без причины.
  • Меньше → точнее сегментация, но медленнее.
  • Больше → быстрее, но модель может пропускать
             короткие слова или выдавать неточные таймкоды.

Паддинг (мс)
  Отступ в миллисекундах, добавляемый к началу и концу
  каждого найденного мата перед заменой звуком.
  • 100 мс — стандарт: бип/мяу начинается чуть раньше
             и заканчивается чуть позже слова.
  • Увеличьте, если начало/конец мата не полностью
    перекрывается звуком цензуры.
  • Уменьшите, если цензура захватывает слишком много
    соседней речи.

═══════════════════════════════════════
🔘 КНОПКИ УПРАВЛЕНИЯ
═══════════════════════════════════════
⚙ Обработать
  Запускает полный цикл: предобработка → шумоподавление
  → восстановление динамики → удаление тишины (VAD)
  → поиск и цензура мата по словарю words.txt.

📄 SRT
  Экспортирует маркеры найденного мата в файл .srt
  отдельно от аудио. Доступна только после успешной
  обработки, если был найден хотя бы один мат.
  Файл можно открыть в Audacity, Premiere, DaVinci
  для ручной проверки и правки.

⛔ Отмена
  Прерывает текущую обработку. Активна только во время
  выполнения задачи.

📋 Копировать
  Копирует всё содержимое журнала в буфер обмена.

═══════════════════════════════════════
📊 СТАТУС-БАР И ЖУРНАЛ
═══════════════════════════════════════
Статус-бар (верхняя серая полоса)
  Слева: имя загруженного файла.
  Справа: прогресс в процентах и время работы в
          формате ММ:СС. Во время обработки обновляется
          каждые 500 мс в реальном времени.

Журнал обработки
  Цветной лог всех этапов. Цвета:
  🟢 Зелёный  — успешные операции
  🟡 Жёлтый   — предупреждения и тайминги
  🔴 Красный  — ошибки и прерывания
  🟣 Фиолетовый — найденные слова мата
  🔵 Синий    — информационные сообщения

Итоговая статистика (после обработки)
  • Найдено мата — количество слов и список уникальных.
  • Убрано тишины — объём удалённой тишины (VAD).
  • Время обработки — общее затраченное время.

═══════════════════════════════════════
🎨 ТЕМА ОФОРМЛЕНИЯ
═══════════════════════════════════════
light / dark / system
  Переключает визуальную тему приложения.
  Выбор сохраняется между запусками.

═══════════════════════════════════════
📁 ФАЙЛЫ ПРОЕКТА
═══════════════════════════════════════
words.txt           Словарь мата (одно слово на строку).
                    Редактируйте вручную для настройки
                    строгости цензуры.
vosk-model-small-ru-0.22/  Модель распознавания речи.
audio_cleaner.log   Технический журнал (ротация 5 МБ × 3).
audio_cleaner_settings.json  Сохранённые настройки UI.
"""


class AudioCleanerApp(ctk.CTk):
    SETTINGS_FILE = "audio_cleaner_settings.json"

    def __init__(self):
        super().__init__()
        self.title("Audio Cleaner Pro v3.5")
        self.geometry("750x800")
        self.minsize(550, 500)

        self.input_file = None
        self.processor = AudioProcessor()
        self.cancel_event = threading.Event()
        self.is_processing = False
        self.start_time = None

        # Поток таймера
        self._timer_thread = None
        self._timer_stop_event = threading.Event()

        self.settings = self._load_settings()

        # === ВЕРХНЯЯ ПАНЕЛЬ ===
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.pack(fill="x", padx=20, pady=(15, 5))
        ctk.CTkLabel(top_frame, text="🎙 Очистка аудио от шума и мата",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")

        right_top = ctk.CTkFrame(top_frame, fg_color="transparent")
        right_top.pack(side="right")

        self.btn_help = ctk.CTkButton(
            right_top, text="❓", width=32, height=28,
            font=ctk.CTkFont(size=14), command=self._show_help
        )
        self.btn_help.pack(side="left", padx=(0, 8))

        self.theme_var = ctk.StringVar(value=self.settings.get("theme", "system"))
        ctk.CTkSegmentedButton(
            right_top, values=["light", "dark", "system"], variable=self.theme_var,
            command=lambda v: [ctk.set_appearance_mode(v), self._save_settings()],
            font=ctk.CTkFont(size=11), width=180, height=28
        ).pack(side="left")
        ctk.set_appearance_mode(self.theme_var.get())

        # === ЕДИНЫЙ СТАТУС-БАР ===
        status_bar = ctk.CTkFrame(self, fg_color=("gray85", "gray20"), corner_radius=8, height=36)
        status_bar.pack(fill="x", padx=20, pady=5)
        status_bar.pack_propagate(False)

        self.status_label = ctk.CTkLabel(status_bar, text="Файл не выбран", text_color="gray", anchor="w")
        self.status_label.pack(side="left", padx=10, fill="x", expand=True)

        self.progress_timer_label = ctk.CTkLabel(
            status_bar, text="", font=ctk.CTkFont(size=13, weight="bold"), width=140, anchor="e"
        )
        self.progress_timer_label.pack(side="right", padx=10)

        # === КНОПКА ЗАГРУЗКИ ===
        self.btn_load = ctk.CTkButton(self, text="📂 Загрузить WAV файл", command=self.load_file, height=45)
        self.btn_load.pack(pady=5, padx=20, fill="x")

        # === АВТОСОХРАНЕНИЕ ===
        auto_frame = ctk.CTkFrame(self, fg_color="transparent")
        auto_frame.pack(fill="x", padx=20, pady=2)
        self.auto_save_var = ctk.BooleanVar(value=self.settings.get("auto_save", False))
        ctk.CTkCheckBox(auto_frame, text="💾 Сохранять рядом с оригиналом", variable=self.auto_save_var, font=ctk.CTkFont(size=12)).pack(side="left")

        # === ПРОГРЕСС-БАР ===
        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=20, pady=5)
        self.progress.set(0)

        # === НАСТРОЙКИ ===
        settings_frame = ctk.CTkFrame(self, fg_color="transparent")
        settings_frame.pack(fill="x", padx=20, pady=5)

        mode_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        mode_row.pack(fill="x", pady=2)
        ctk.CTkLabel(mode_row, text="Режим:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.censor_mode_var = ctk.StringVar(value=self.settings.get("censor_mode", "meow"))
        self.mode_switch = ctk.CTkSegmentedButton(mode_row, values=["beep", "meow"], variable=self.censor_mode_var, font=ctk.CTkFont(size=12), width=160, height=28)
        self.mode_switch.pack(side="left", padx=(10, 0))

        nr_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        nr_row.pack(fill="x", pady=2)
        ctk.CTkLabel(nr_row, text="Шумодав:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.nr_value_label = ctk.CTkLabel(nr_row, text="0.50", width=35, anchor="e", font=ctk.CTkFont(size=12))
        self.nr_value_label.pack(side="right")
        self.nr_slider = ctk.CTkSlider(nr_row, from_=0.0, to=1.0, number_of_steps=20, command=lambda v: self.nr_value_label.configure(text=f"{v:.2f}"), height=16)
        self.nr_slider.set(self.settings.get("noise_reduction", 0.5))
        self.nr_slider.pack(side="right", padx=(10, 5), fill="x", expand=True)

        thresh_row = ctk.CTkFrame(settings_frame, fg_color="transparent")
        thresh_row.pack(fill="x", pady=2)
        ctk.CTkLabel(thresh_row, text="Чанк:", font=ctk.CTkFont(size=12)).pack(side="left")
        self.chunk_entry = ctk.CTkEntry(thresh_row, width=70, height=26, font=ctk.CTkFont(size=12))
        self.chunk_entry.insert(0, str(self.settings.get("censor_chunk_size", 8000)))
        self.chunk_entry.pack(side="left", padx=(10, 0))
        ctk.CTkLabel(thresh_row, text="Паддинг (мс):", font=ctk.CTkFont(size=12)).pack(side="left", padx=(15, 0))
        self.pad_entry = ctk.CTkEntry(thresh_row, width=60, height=26, font=ctk.CTkFont(size=12))
        self.pad_entry.insert(0, str(self.settings.get("padding_ms", 100)))
        self.pad_entry.pack(side="left", padx=(10, 0))

        # === КНОПКИ ===
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=5)
        self.btn_save = ctk.CTkButton(btn_frame, text="⚙ Обработать", command=self.save_processed, height=40, state="disabled")
        self.btn_save.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.btn_export_srt = ctk.CTkButton(btn_frame, text="📄 SRT", command=self.export_srt_standalone, height=40, width=80, state="disabled", fg_color="#3498db")
        self.btn_export_srt.pack(side="right", padx=(5, 0))
        self.btn_cancel = ctk.CTkButton(btn_frame, text="⛔ Отмена", command=self.cancel_processing, height=40, width=100, fg_color="#d9534f", state="disabled")
        self.btn_cancel.pack(side="right")

        # === ЛОГ ===
        log_header = ctk.CTkFrame(self, fg_color="transparent")
        log_header.pack(fill="x", padx=20, pady=(10, 2))
        ctk.CTkLabel(log_header, text="Журнал:", anchor="w", font=ctk.CTkFont(size=13)).pack(side="left")
        self.btn_copy_log = ctk.CTkButton(log_header, text="📋 Копировать", command=self.copy_log, height=28, width=110, font=ctk.CTkFont(size=12))
        self.btn_copy_log.pack(side="right")

        self.log_box = ColorLogTextBox(self, height=200)
        self.log_box.pack(fill="both", expand=True, padx=20, pady=(0, 15))

        if self.processor.dict_warning:
            self._log(self.processor.dict_warning)
        if self.processor.vad_warning:
            self._log(self.processor.vad_warning)

    # === СПРАВКА ===
    def _show_help(self):
        win = ctk.CTkToplevel(self)
        win.title("Справка")
        win.geometry("620x700")
        win.resizable(True, True)
        win.transient(self)
        win.grab_set()

        help_box = ctk.CTkTextbox(win, font=("Consolas", 12), wrap="word")
        help_box.pack(fill="both", expand=True, padx=10, pady=10)
        help_box.insert("1.0", HELP_TEXT)
        help_box.configure(state="disabled")

        ctk.CTkButton(win, text="Закрыть", command=win.destroy, height=36, width=120).pack(pady=(0, 10))

    # === НАСТРОЙКИ ===
    def _load_settings(self) -> dict:
        try:
            with open(self.SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        try:
            chunk_sz = int(self.chunk_entry.get())
        except ValueError:
            chunk_sz = 8000
        try:
            pad_ms = int(self.pad_entry.get())
        except ValueError:
            pad_ms = 100
        data = {
            "theme": self.theme_var.get(),
            "auto_save": self.auto_save_var.get(),
            "censor_mode": self.censor_mode_var.get(),
            "noise_reduction": round(self.nr_slider.get(), 2),
            "censor_chunk_size": chunk_sz,
            "padding_ms": pad_ms,
        }
        try:
            with open(self.SETTINGS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # === ТАЙМЕР (независимый поток, не подвисает) ===
    def _format_mm_ss(self, seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m:02d}:{s:02d}"

    def _start_timer(self):
        self.start_time = time.time()
        self._timer_stop_event.clear()
        self.progress_timer_label.configure(text="0% | 00:00")

        def _timer_worker():
            while not self._timer_stop_event.is_set():
                elapsed = time.time() - self.start_time
                pct = int(self.progress.get() * 100)
                text = f"{pct}% | {self._format_mm_ss(elapsed)}"
                self.after(0, lambda t=text: self.progress_timer_label.configure(text=t))
                self._timer_stop_event.wait(0.5)

        self._timer_thread = threading.Thread(target=_timer_worker, daemon=True)
        self._timer_thread.start()

    def _update_progress(self, value: float):
        self.progress.set(value)

    def _stop_timer(self, success=True):
        self._timer_stop_event.set()
        if self._timer_thread and self._timer_thread.is_alive():
            self._timer_thread.join(timeout=2)
        self._timer_thread = None

        if self.start_time:
            elapsed = time.time() - self.start_time
            icon = "✅" if success else "⛔"
            self.progress_timer_label.configure(text=f"{icon} {self._format_mm_ss(elapsed)}")
        self.start_time = None

    # === ЛОГ ===
    def _log(self, message: str):
        self.after(0, lambda: self.log_box.append(message))

    def copy_log(self):
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
        self.status_label.configure(text=f"✅ {os.path.basename(path)}", text_color="green")
        self.btn_save.configure(state="normal")
        self.progress.set(0)
        self.progress_timer_label.configure(text="")
        self.log_box.clear()
        self._log(f"Файл загружен: {os.path.basename(path)}")

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
        if self.auto_save_var.get():
            base, ext = os.path.splitext(self.input_file)
            output_path = f"{base}_cleaned{ext}"
        else:
            output_path = filedialog.asksaveasfilename(
                title="Сохранить", defaultextension=".wav", filetypes=[("WAV files", "*.wav")],
                initialfile=f"cleaned_{os.path.basename(self.input_file)}"
            )
            if not output_path:
                return
        self._start_processing(output_path)

    def _start_processing(self, output_path: str):
        self.is_processing = True
        self.cancel_event.clear()
        for w in [self.mode_switch, self.btn_load, self.btn_save, self.btn_export_srt, self.nr_slider, self.chunk_entry, self.pad_entry]:
            w.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.log_box.clear()
        self.progress.set(0)
        self._start_timer()
        self._save_settings()

        def worker():
            ok, msg, stats = self.processor.process(
                self.input_file, output_path, censor_mode=self.censor_mode_var.get(),
                noise_reduction=round(self.nr_slider.get(), 2),
                censor_chunk_size=int(self.chunk_entry.get() or 8000),
                padding_ms=int(self.pad_entry.get() or 100),
                progress_cb=lambda v: self.after(0, lambda: self._update_progress(v)),
                log_cb=self._log, cancel_event=self.cancel_event
            )
            self.after(0, lambda: self._on_complete(ok, msg, stats))

        threading.Thread(target=worker, daemon=True).start()

    def _on_complete(self, success: bool, message: str, stats: dict):
        self.is_processing = False
        for w in [self.mode_switch, self.btn_load, self.btn_save, self.nr_slider, self.chunk_entry, self.pad_entry]:
            w.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self._stop_timer(success=success)

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
            self._log(f"   • Найдено мата: {pc} слов ({', '.join(uw)})" if uw else "   • Мат не найден")
            self._log(f"   • Убрано тишины: {mt_str}")
            self._log(f"   • Время обработки: {st_str}")
            self._log(f"\n🎉 {message}")
            self.btn_export_srt.configure(state="normal" if pc > 0 else "disabled")
            messagebox.showinfo("Готово", message)
        elif "прервана" in message.lower():
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
            initialfile=f"markers_{os.path.basename(self.input_file)}.srt"
        )
        if path:
            self.processor.export_srt(path, censor_mode=self.censor_mode_var.get(), log_cb=self._log)
            messagebox.showinfo("Экспорт SRT", f"Маркеры сохранены:\n{path}")


if __name__ == "__main__":
    app = AudioCleanerApp()
    app.mainloop()