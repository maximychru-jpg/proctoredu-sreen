import io
import os
import sys
import time
import json
import asyncio
import threading
import winreg
import ctypes
from ctypes import wintypes
import tkinter as tk
from tkinter import messagebox
from datetime import datetime
import customtkinter as ctk
import aiohttp
from PIL import ImageGrab
from pynput import keyboard

# ==================== НАСТРОЙКИ ПУТЕЙ ====================
APP_NAME = "ScreenshotSenderApp"
CONFIG_FILE = os.path.join(os.getenv('APPDATA'), 'screenshot_config.json')

# ==================== ГЛОБАЛЬНЫЕ СОСТОЯНИЯ ====================
TOKEN = ""
CHAT_ID = ""
loop = None
app = None
log_box = None

current_keys = set()
ACTIVE_SCREENSHOT_KEYS = set()
ACTIVE_MENU_KEYS = set()

recording_widget = None
max_pressed_keys = set()

last_capture_time = 0

# ==================== СИСТЕМА ЛОГИРОВАНИЯ ====================
def log_msg(message):
    time_str = datetime.now().strftime("%H:%M:%S")
    full_msg = f"[{time_str}] {message}\n"
    print(full_msg.strip()) 
    if app and log_box:
        app.after(0, _append_log_gui, full_msg)

def _append_log_gui(msg):
    log_box.configure(state="normal")
    log_box.insert("end", msg)
    log_box.see("end")
    log_box.configure(state="disabled")

# ==================== WINDOWS API (ИСТИННАЯ НЕВИДИМОСТЬ) ====================
def apply_window_stealth(tk_app, enable):
    """Скрывает окно от OBS, Discord и других программ записи экрана."""
    if os.name != 'nt': return
    try:
        user32 = ctypes.windll.user32
        user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.SetWindowDisplayAffinity.restype = wintypes.BOOL

        # Надежный способ получить ИСТИННЫЙ HWND окна в Windows
        try:
            hwnd = int(tk_app.frame(), 16)
        except Exception:
            hwnd = user32.GetParent(tk_app.winfo_id()) or tk_app.winfo_id()

        WDA_NONE = 0
        WDA_EXCLUDEFROMCAPTURE = 0x00000011 # Полная невидимость (Win 10 версия 2004+)
        WDA_MONITOR = 0x00000001            # Запасной вариант: Черный квадрат (старые Win 10)

        val = WDA_EXCLUDEFROMCAPTURE if enable else WDA_NONE
        result = user32.SetWindowDisplayAffinity(hwnd, val)

        # Если система старая и новый флаг не сработал - используем запасной
        if not result and enable:
            user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR)
            log_msg("⚠️ Скрыто старым методом (чёрный квадрат на записи).")
        else:
            if enable: log_msg("👻 Режим невидимки активирован (окно пропало из OBS).")
            else: log_msg("👀 Окно снова видно на записях экрана.")
    except Exception as e:
        log_msg(f"Ошибка настройки невидимости: {e}")

# ==================== ЯДРО ГОРЯЧИХ КЛАВИШ ====================
def get_canonical_key(key):
    if key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r): return keyboard.Key.ctrl_l
    if key in (keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr): return keyboard.Key.alt_l
    if key in (keyboard.Key.shift_l, keyboard.Key.shift_r): return keyboard.Key.shift_l
    
    if hasattr(key, 'vk') and key.vk is not None:
        return keyboard.KeyCode(vk=key.vk)
    return key

def keys_to_config(key_set):
    res = []
    for k in key_set:
        if hasattr(k, 'name'): res.append(k.name)
        elif hasattr(k, 'vk') and k.vk: res.append(f"vk_{k.vk}")
        elif hasattr(k, 'char') and k.char: res.append(f"char_{k.char}")
    return res

def config_to_keys(str_list):
    res = set()
    if not isinstance(str_list, list): return res
    for s in str_list:
        if s.startswith("vk_"): res.add(keyboard.KeyCode(vk=int(s[3:])))
        elif s.startswith("char_"): res.add(keyboard.KeyCode(char=s[5:]))
        else:
            try: res.add(getattr(keyboard.Key, s))
            except AttributeError: pass
    return res

def format_key_set(key_set):
    if not key_set: return "Нажмите для настройки..."
    parts = []
    for k in key_set:
        if k == keyboard.Key.ctrl_l: parts.append("Ctrl")
        elif k == keyboard.Key.alt_l: parts.append("Alt")
        elif k == keyboard.Key.shift_l: parts.append("Shift")
        elif k == keyboard.Key.space: parts.append("Space")
        elif k == keyboard.Key.esc: parts.append("Esc")
        elif hasattr(k, 'vk') and k.vk:
            if 65 <= k.vk <= 90 or 48 <= k.vk <= 57:
                parts.append(chr(k.vk))
            elif k.vk == 32: parts.append("Space")
            else: parts.append(f"Key_{k.vk}")
        elif hasattr(k, 'char') and k.char:
            parts.append(k.char.upper())
        else:
            parts.append(str(k).replace("Key.", "").capitalize())
            
    order = {"Ctrl": 1, "Alt": 2, "Shift": 3}
    parts.sort(key=lambda x: (order.get(x, 5), x))
    return " + ".join(parts)

# ==================== РАБОТА С КОНФИГОМ ====================
def load_config():
    default = {
        "token": "", "chat_id": "", "autostart": False, "stealth_window": False,
        "screenshot_hk": ["ctrl_l", "alt_l"],  # Дефолт: Ctrl + Alt
        "menu_hk": ["ctrl_l", "shift_l"]       # Дефолт: Ctrl + Shift
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded.get("screenshot_hk"), str):
                    loaded["screenshot_hk"] = default["screenshot_hk"]
                    loaded["menu_hk"] = default["menu_hk"]
                default.update(loaded)
        except: pass
    return default

def save_config(data):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: json.dump(data, f)
    except Exception as e: log_msg(f"Ошибка сохранения: {e}")

def set_autostart(enable):
    exe_path = f'"{sys.executable}" --silent' if getattr(sys, 'frozen', False) else f'"{sys.executable}" "{os.path.abspath(__file__)}" --silent'
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable: winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, exe_path)
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass
        winreg.CloseKey(key)
    except Exception as e: log_msg(f"Ошибка реестра: {e}")

# ==================== СЕТЬ И ОКНА ====================
async def send_to_telegram_async(img_bytes):
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    data = aiohttp.FormData()
    data.add_field('chat_id', CHAT_ID)
    data.add_field('photo', img_bytes, filename='screenshot.png', content_type='image/png')
    
    log_msg("Отправка скриншота...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, timeout=15) as response:
                if response.status == 200: log_msg("✅ Успешно доставлено!")
                else: log_msg(f"❌ Ошибка Telegram ({response.status}): {await response.text()}")
    except Exception as e:
        log_msg(f"❌ Ошибка сети: {e}")

def capture_screen():
    global last_capture_time
    if time.time() - last_capture_time < 1.0: return 
    last_capture_time = time.time()

    try:
        log_msg("📸 Делаю снимок экрана...")
        screenshot = ImageGrab.grab()
        img_buffer = io.BytesIO()
        screenshot.save(img_buffer, format='PNG')
        img_buffer.seek(0)
        img_bytes = img_buffer.read()
        
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(send_to_telegram_async(img_bytes), loop)
    except Exception as e:
        log_msg(f"❌ Ошибка захвата: {e}")

def show_app_window():
    if app:
        app.deiconify()
        app.state('normal')
        app.attributes('-topmost', True)
        app.lift()
        app.focus_force()
        app.attributes('-topmost', False)

# ==================== СЛУШАТЕЛЬ КЛАВИАТУРЫ ====================
def on_press(key):
    global current_keys, recording_widget, max_pressed_keys
    can_key = get_canonical_key(key)
    
    if can_key in current_keys: return 
    current_keys.add(can_key)

    if recording_widget:
        if can_key == keyboard.Key.esc: 
            recording_widget.stop_recording(None)
            current_keys.clear()
            max_pressed_keys.clear()
            return
        max_pressed_keys.add(can_key)
        return

    if ACTIVE_MENU_KEYS and current_keys == ACTIVE_MENU_KEYS:
        log_msg("⚙️ Вызвано меню настроек.")
        if app: app.after(0, show_app_window)
        current_keys.clear()
        return

    if ACTIVE_SCREENSHOT_KEYS and current_keys == ACTIVE_SCREENSHOT_KEYS:
        capture_screen()
        current_keys.clear()
        return

def on_release(key):
    global current_keys, recording_widget, max_pressed_keys
    can_key = get_canonical_key(key)
    if can_key in current_keys:
        current_keys.remove(can_key)

    if recording_widget and not current_keys:
        if max_pressed_keys:
            recording_widget.stop_recording(max_pressed_keys)
            max_pressed_keys.clear()

def start_keyboard_listener():
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()

def start_async_loop(async_loop):
    asyncio.set_event_loop(async_loop)
    async_loop.run_forever()

# ==================== GUI (ИНТЕРФЕЙС) ====================
class HotkeyRecorder(ctk.CTkButton):
    def __init__(self, master, keys_list, **kwargs):
        self.current_keys = config_to_keys(keys_list)
        super().__init__(master, text=format_key_set(self.current_keys), command=self.start_recording, **kwargs)

    def start_recording(self):
        global recording_widget, max_pressed_keys
        if recording_widget: return 
        recording_widget = self
        max_pressed_keys.clear()
        self.configure(text="Слушаю... (Зажмите и отпустите. Esc - отмена)", fg_color="#b5432a", hover_color="#8a301c")

    def stop_recording(self, new_keys):
        global recording_widget
        recording_widget = None
        self.configure(fg_color=["#3a7ebf", "#1f538d"]) 
        if new_keys is not None and new_keys:
            self.current_keys = new_keys.copy()
        self.configure(text=format_key_set(self.current_keys))

def enable_clipboard(ctk_entry):
    tk_entry = ctk_entry._entry
    def on_paste():
        try: tk_entry.insert(tk.INSERT, tk_entry.clipboard_get())
        except tk.TclError: pass
        return "break"
    def on_copy():
        try:
            if tk_entry.select_present():
                tk_entry.clipboard_clear()
                tk_entry.clipboard_append(tk_entry.selection_get())
        except tk.TclError: pass
        return "break"
    def on_select_all():
        tk_entry.select_range(0, tk.END)
        tk_entry.icursor(tk.END)
        return "break"

    def on_ctrl_keypress(event):
        if event.keycode == 86 or event.keysym.lower() == 'v': return on_paste()
        elif event.keycode == 67 or event.keysym.lower() == 'c': return on_copy()
        elif event.keycode == 65 or event.keysym.lower() == 'a': return on_select_all()

    tk_entry.bind("<Control-KeyPress>", on_ctrl_keypress)
    menu = tk.Menu(tk_entry, tearoff=0, bg="#2b2b2b", fg="white", borderwidth=0)
    menu.add_command(label="Вставить", command=on_paste)
    menu.add_command(label="Копировать", command=on_copy)
    menu.add_command(label="Выделить всё", command=on_select_all)
    tk_entry.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))

def create_gui(config):
    global app, log_box
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

    app = ctk.CTk()
    app.title("Screenshot Sender PRO")
    app.geometry("880x590")
    app.resizable(False, False)
    
    app.protocol("WM_DELETE_WINDOW", lambda: app.withdraw())

    left_frame = ctk.CTkFrame(app, fg_color="transparent")
    left_frame.pack(side="left", fill="both", expand=True, padx=20, pady=20)

    ctk.CTkLabel(left_frame, text="Настройки Бота", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w", pady=(0, 10))

    ctk.CTkLabel(left_frame, text="Токен бота:", font=ctk.CTkFont(size=12)).pack(anchor="w")
    entry_token = ctk.CTkEntry(left_frame, width=350)
    entry_token.insert(0, config.get("token", ""))
    entry_token.pack(anchor="w", pady=(0, 10))
    enable_clipboard(entry_token) 

    ctk.CTkLabel(left_frame, text="Chat ID:", font=ctk.CTkFont(size=12)).pack(anchor="w")
    entry_chat_id = ctk.CTkEntry(left_frame, width=350)
    entry_chat_id.insert(0, config.get("chat_id", ""))
    entry_chat_id.pack(anchor="w", pady=(0, 15))
    enable_clipboard(entry_chat_id)

    ctk.CTkLabel(left_frame, text="Клавиши для Скриншота (Кликните для записи):", font=ctk.CTkFont(size=12)).pack(anchor="w")
    btn_hk_screen = HotkeyRecorder(left_frame, config.get("screenshot_hk"), width=350)
    btn_hk_screen.pack(anchor="w", pady=(0, 10))

    ctk.CTkLabel(left_frame, text="Клавиши вызова Меню (Кликните для записи):", font=ctk.CTkFont(size=12)).pack(anchor="w")
    btn_hk_menu = HotkeyRecorder(left_frame, config.get("menu_hk"), width=350)
    btn_hk_menu.pack(anchor="w", pady=(0, 10))

    var_autostart = ctk.BooleanVar(value=config.get("autostart", False))
    ctk.CTkCheckBox(left_frame, text="Запускать при старте Windows (в фоне)", variable=var_autostart).pack(anchor="w", pady=(5, 0))

    var_stealth_window = ctk.BooleanVar(value=config.get("stealth_window", False))
    
    def toggle_stealth():
        # Передаем сам объект app, чтобы внутри извлечь правильный HWND
        apply_window_stealth(app, var_stealth_window.get())
        
    ctk.CTkCheckBox(left_frame, text="Скрыть окно от стримов и записей (OBS, Discord)", variable=var_stealth_window, command=toggle_stealth).pack(anchor="w", pady=(10, 10))

    def on_save_and_run():
        global TOKEN, CHAT_ID, ACTIVE_SCREENSHOT_KEYS, ACTIVE_MENU_KEYS
        
        t, c = entry_token.get().strip(), entry_chat_id.get().strip()
        if not t or not c:
            messagebox.showerror("Ошибка", "Токен и Chat ID не могут быть пустыми!")
            return

        if btn_hk_screen.current_keys == btn_hk_menu.current_keys:
            messagebox.showerror("Ошибка", "Комбинации клавиш не могут быть одинаковыми!")
            return

        try:
            cfg = {
                "token": t, "chat_id": c, 
                "autostart": var_autostart.get(),
                "stealth_window": var_stealth_window.get(),
                "screenshot_hk": keys_to_config(btn_hk_screen.current_keys),
                "menu_hk": keys_to_config(btn_hk_menu.current_keys)
            }
            save_config(cfg)
            set_autostart(var_autostart.get())
            
            TOKEN, CHAT_ID = t, c
            ACTIVE_SCREENSHOT_KEYS = btn_hk_screen.current_keys
            ACTIVE_MENU_KEYS = btn_hk_menu.current_keys
            
            log_msg("✅ Настройки сохранены и применены.")
            log_msg("Программа работает. Чтобы скрыть окно в фон, нажмите крестик (X).")
        except Exception as e:
            log_msg(f"❌ Ошибка при сохранении: {e}")

    def on_exit_completely():
        if messagebox.askyesno("Выход", "Остановить программу полностью? (Скриншоты работать не будут)"):
            app.quit()
            os._exit(0)

    def on_factory_reset():
        if messagebox.askyesno("Сброс", "Вы уверены? Это очистит настройки и автозагрузку."):
            global TOKEN, CHAT_ID, ACTIVE_SCREENSHOT_KEYS, ACTIVE_MENU_KEYS
            if os.path.exists(CONFIG_FILE):
                try: os.remove(CONFIG_FILE)
                except: pass
            
            set_autostart(False)
            var_autostart.set(False)
            
            var_stealth_window.set(False)
            toggle_stealth()

            entry_token.delete(0, 'end')
            entry_chat_id.delete(0, 'end')
            
            default_screen = config_to_keys(["ctrl_l", "alt_l"])
            default_menu = config_to_keys(["ctrl_l", "shift_l"])
            btn_hk_screen.stop_recording(default_screen)
            btn_hk_menu.stop_recording(default_menu)

            TOKEN, CHAT_ID = "", ""
            ACTIVE_SCREENSHOT_KEYS = default_screen
            ACTIVE_MENU_KEYS = default_menu
            
            log_msg("🔄 Программа сброшена до заводских настроек.")

    ctk.CTkButton(left_frame, text="💾 Сохранить и запустить", font=ctk.CTkFont(weight="bold"), command=on_save_and_run, height=40).pack(pady=(15, 5), fill="x")
    ctk.CTkButton(left_frame, text="Остановить программу полностью", fg_color="#a83232", hover_color="#802323", command=on_exit_completely, height=35).pack(pady=(0, 5), fill="x")
    ctk.CTkButton(left_frame, text="Сбросить всё полностью", fg_color="#8c6c21", hover_color="#6e5519", command=on_factory_reset, height=35).pack(fill="x")

    right_frame = ctk.CTkFrame(app, fg_color="#1e1e1e", border_width=1, border_color="#333333")
    right_frame.pack(side="right", fill="both", expand=True, padx=(0, 20), pady=20)
    
    ctk.CTkLabel(right_frame, text="Логи работы (Telegram API):", font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))
    
    log_box = ctk.CTkTextbox(right_frame, state="disabled", wrap="word", fg_color="#0a0a0a", text_color="#00FF41")
    log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # Сразу применяем настройки невидимости окна после создания GUI
    app.update()
    apply_window_stealth(app, var_stealth_window.get())

    return app

# ==================== ТОЧКА ВХОДА ====================
if __name__ == "__main__":
    cfg = load_config()
    
    TOKEN = cfg.get("token", "")
    CHAT_ID = cfg.get("chat_id", "")
    ACTIVE_SCREENSHOT_KEYS = config_to_keys(cfg.get("screenshot_hk", []))
    ACTIVE_MENU_KEYS = config_to_keys(cfg.get("menu_hk", []))

    loop = asyncio.new_event_loop()
    threading.Thread(target=start_async_loop, args=(loop,), daemon=True).start()
    threading.Thread(target=start_keyboard_listener, daemon=True).start()

    app = create_gui(cfg)
    log_msg("Приложение запущено и ожидает горячих клавиш.")

    if "--silent" in sys.argv and TOKEN and CHAT_ID:
        log_msg("Автостарт: окно спрятано.")
        app.withdraw()

    app.mainloop()
    