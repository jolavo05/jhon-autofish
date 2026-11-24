"""
GPO Autofish - macro de pesca por leitura de tela (sem mexer na memória do jogo).

Teclas (padrão, mude em config.json):
  F6  liga/desliga o loop de pesca
  F7  abre a caixinha pra marcar onde fica a barra (Enter salva, Esc cancela)
  F8  testa a detecção (salva debug.png e mostra o que está vendo)
  F9  mostra a cor (RGB) do pixel debaixo do mouse
  F10 fecha o script

Minigame (pela referência): barra vertical com uma ZONA cinza que você controla
(segurar o clique sobe, soltar desce) e uma LINHA BRANCA (o peixe).
Manter a linha dentro da zona enche a barra verde. Um controlador PD decide
quando segurar/soltar o clique.
"""
import ctypes
import json
import os
import queue
import random
import threading
import time
import tkinter as tk

# Precisa vir antes de criar qualquer janela: coordenadas reais em telas com escala
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import mss
import mss.tools
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    # Área da caixinha: [esquerda, topo, largura, altura] (definida com F7)
    "area": None,
    "strip_half_width": 2,

    # Controlador PD: u = kp*erro + kd*(velocidade do erro em px/s)
    "kp": 1.0,
    "kd": 0.05,
    "deadband_px": 2,
    "invert": False,          # true se segurar o clique faz a zona DESCER

    # Tempos
    "cast_hold_s": 0.35,
    "scan_timeout_s": 10.0,
    "wait_after_s": 1.0,
    "bar_gone_s": 0.6,
    "click_on_bite": False,

    # Cores RGB medidas no jogo (ajuste com F9 / F8 se o GPO mudar)
    "bar_rgb": [85, 170, 255],    # azul-claro do corpo da barra
    "bar_tol": 22,
    "zone_rgb": [25, 25, 25],     # cinza escuro da zona que você controla
    "zone_tol": 8,
    "fish_white_min": 230,        # linha branca do peixe
    "min_bar_frac": 0.15,         # fração mínima da altura com azul-claro p/ considerar a barra visível

    "window_title": "Roblox",

    # Virtual-key codes: F6=0x75 F7=0x76 F8=0x77 F9=0x78 F10=0x79
    "key_toggle": 0x75,
    "key_area": 0x76,
    "key_debug": 0x77,
    "key_color": 0x78,
    "key_quit": 0x79,
}

# ---------------------------------------------------------------- Windows API
user32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_pos():
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def foreground_title():
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


_pressed = {}


def key_pressed(vk):
    """True uma vez por aperto."""
    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
    was = _pressed.get(vk, False)
    _pressed[vk] = down
    return down and not was


class Mouse:
    def __init__(self):
        self.down = False

    def press(self):
        if not self.down:
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            self.down = True

    def release(self):
        if self.down:
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.down = False

    def click(self, hold=0.05):
        self.press()
        time.sleep(hold)
        self.release()


# ---------------------------------------------------------------- config
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            old = json.load(f)
        cfg.update({k: v for k, v in old.items() if k in DEFAULT_CONFIG})
        # migra a calibração antiga (F7 topo/fundo) para a caixinha
        if cfg["area"] is None and old.get("bar_x") is not None:
            cfg["area"] = [old["bar_x"] - 20, old["bar_top"], 40,
                           old["bar_bottom"] - old["bar_top"] + 1]
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- visão
def longest_run(mask):
    best, start, best_len = None, None, 0
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > best_len:
                best_len, best = i - start, (start, i - 1)
            start = None
    return best


class Vision:
    """Acha, dentro da caixinha, a barra azul-claro; lê a zona escura e a linha branca."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.sct = None  # mss é por thread: criado no primeiro uso

    def grab(self):
        if self.sct is None:
            self.sct = mss.mss()
        l, t, w, h = self.cfg["area"]
        shot = self.sct.grab({"left": l, "top": t, "width": w, "height": h})
        return shot, np.asarray(shot)[:, :, :3].astype(np.int16)  # BGR

    def masks(self, img):
        c = self.cfg
        rgb = img[:, :, ::-1]  # BGR -> RGB
        bar = (np.abs(rgb - np.array(c["bar_rgb"])) <= c["bar_tol"]).all(axis=2)
        zone = (np.abs(rgb - np.array(c["zone_rgb"])) <= c["zone_tol"]).all(axis=2)
        white = (rgb >= c["fish_white_min"]).all(axis=2)
        return white, zone, bar

    def analyze(self, img):
        """Retorna (coluna_central ou None, linhas brancas, linhas zona, linhas barra)."""
        white, zone, bar = self.masks(img)
        h = img.shape[0]
        per_col = bar.sum(axis=0)
        if per_col.max() < self.cfg["min_bar_frac"] * h:
            return None, None, None, None
        # centro das colunas com bastante azul-claro (o meio da barra)
        cols = np.where(per_col >= 0.6 * per_col.max())[0]
        col = int(round(np.median(cols)))
        w = self.cfg["strip_half_width"]
        sl = slice(max(col - w, 0), col + w + 1)
        row_w = white[:, sl].mean(axis=1) > 0.5
        row_z = zone[:, sl].mean(axis=1) > 0.5
        row_b = bar[:, sl].mean(axis=1) > 0.5
        return col, row_w, row_z, row_b

    def read(self):
        """(visivel, fish_y, zona_centro_y)."""
        _, img = self.grab()
        col, row_w, row_z, _ = self.analyze(img)
        if col is None:
            return False, None, None
        zone = longest_run(row_z | row_w)   # a linha do peixe pode cortar a zona
        fish = longest_run(row_w)
        if zone is None or fish is None:
            return True, None, None
        return True, (fish[0] + fish[1]) / 2, (zone[0] + zone[1]) / 2

    def debug(self, log):
        shot, img = self.grab()
        path = os.path.join(HERE, "debug.png")
        mss.tools.to_png(shot.rgb, shot.size, output=path)
        col, row_w, row_z, row_b = self.analyze(img)
        if col is None:
            log(f"[debug] área salva em {path}; barra azul-claro NÃO encontrada na caixa")
            return
        log(f"[debug] área salva em {path}; barra achada na coluna {col} da caixa")
        line = "".join("W" if a else "Z" if b else "B" if c else "."
                       for a, b, c in zip(row_w, row_z, row_b))
        log("[debug] de cima pra baixo (W=peixe Z=zona B=barra .=outro):")
        for i in range(0, len(line), 80):
            log("    " + line[i:i + 80])
        log(f"[debug] leitura (visivel, peixe_y, zona_y): {self.read()}")


# ---------------------------------------------------------------- caixinha
class AreaSelector:
    """Janela transparente: arraste pra mover, bordas/cantos pra redimensionar."""
    EDGE = 10

    def __init__(self, root, area, on_done):
        self.on_done = on_done
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if area is None:
            area = [sw // 2 - 60, sh // 2 - 200, 120, 400]
        self.x, self.y, self.w, self.h = area

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.45)
        self.win.configure(bg="#1fa3a3", highlightthickness=2, highlightbackground="#2ee6d6")

        self.info = tk.Label(self.win, bg="#0d3b4a", fg="white", font=("Segoe UI", 9),
                             justify="left")
        self.info.pack(fill="x")
        btns = tk.Frame(self.win, bg="#1fa3a3")
        btns.pack(side="bottom", fill="x", pady=4)
        tk.Button(btns, text="Salvar (Enter)", command=self.save).pack(side="left", expand=True)
        tk.Button(btns, text="Cancelar (Esc)", command=self.cancel).pack(side="left", expand=True)

        self.win.bind("<ButtonPress-1>", self.start)
        self.win.bind("<B1-Motion>", self.drag)
        self.win.bind("<Motion>", self.hover)
        self.win.bind("<Return>", lambda e: self.save())
        self.win.bind("<Escape>", lambda e: self.cancel())
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)):
            self.win.bind(f"<{key}>", lambda e, dx=dx, dy=dy: self.nudge(dx, dy))
        self.apply()
        self.win.focus_force()

    def apply(self):
        self.w, self.h = max(self.w, 20), max(self.h, 60)
        self.win.geometry(f"{self.w}x{self.h}+{self.x}+{self.y}")
        self.info.config(text=f"Arraste p/ mover · bordas p/ redimensionar\n"
                              f"@ ({self.x}, {self.y})  {self.w}x{self.h}")

    def zone(self, ex, ey):
        e = self.EDGE
        return (("w" if ex < e else "e" if ex > self.w - e else "") +
                ("n" if ey < e else "s" if ey > self.h - e else ""))

    def hover(self, ev):
        z = self.zone(ev.x_root - self.x, ev.y_root - self.y)
        cursors = {"w": "sb_h_double_arrow", "e": "sb_h_double_arrow",
                   "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
                   "wn": "size_nw_se", "es": "size_nw_se",
                   "en": "size_ne_sw", "ws": "size_ne_sw"}
        self.win.config(cursor=cursors.get(z, "fleur"))

    def start(self, ev):
        self.mode = self.zone(ev.x_root - self.x, ev.y_root - self.y)
        self.ox, self.oy = ev.x_root, ev.y_root
        self.orig = (self.x, self.y, self.w, self.h)

    def drag(self, ev):
        dx, dy = ev.x_root - self.ox, ev.y_root - self.oy
        x, y, w, h = self.orig
        m = self.mode
        if not m:
            x, y = x + dx, y + dy
        if "e" in m:
            w += dx
        if "s" in m:
            h += dy
        if "w" in m:
            x, w = x + dx, w - dx
        if "n" in m:
            y, h = y + dy, h - dy
        self.x, self.y, self.w, self.h = x, y, w, h
        self.apply()

    def nudge(self, dx, dy):
        self.x += dx
        self.y += dy
        self.apply()

    def save(self):
        area = [self.x, self.y, self.w, self.h]
        self.win.destroy()
        self.on_done(area)

    def cancel(self):
        self.win.destroy()
        self.on_done(None)


# ---------------------------------------------------------------- macro (thread)
class Macro(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.cfg = app.cfg
        self.mouse = Mouse()
        self.vision = Vision(self.cfg)
        self.running = False
        self.paused = False   # enquanto a caixinha está aberta
        self.caught = 0
        self.quit = False

    def set_running(self, on):
        if on and self.cfg["area"] is None:
            self.app.log("Defina a área primeiro (F7).")
            return
        self.running = on
        self.mouse.release()
        self.app.status("Pescando" if on else "Desligado")

    def poll_keys(self):
        c = self.cfg
        if key_pressed(c["key_quit"]):
            self.app.post(self.app.close)
        if key_pressed(c["key_toggle"]):
            self.set_running(not self.running)
        if key_pressed(c["key_area"]):
            self.app.post(self.app.open_selector)
        if key_pressed(c["key_debug"]):
            self.app.post(self.app.debug)
        if key_pressed(c["key_color"]):
            x, y = cursor_pos()
            with mss.mss() as s:
                px = s.grab({"left": x, "top": y, "width": 1, "height": 1}).pixel(0, 0)
            self.app.log(f"Cor em ({x},{y}): RGB{px}")

    def active(self):
        return self.running and not self.paused and not self.quit

    def sleep(self, seconds):
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            self.poll_keys()
            if not self.active():
                return False
            time.sleep(0.01)
        return True

    def roblox_focused(self):
        return foreground_title() == self.cfg["window_title"]

    def cast(self):
        self.app.status("Lançando")
        self.mouse.press()
        ok = self.sleep(self.cfg["cast_hold_s"] + random.uniform(0, 0.05))
        self.mouse.release()
        return ok

    def wait_for_bar(self):
        self.app.status("Esperando peixe")
        end = time.perf_counter() + self.cfg["scan_timeout_s"]
        while time.perf_counter() < end:
            self.poll_keys()
            if not self.active():
                return False
            if self.vision.read()[0]:
                return True
            time.sleep(0.03)
        self.app.log("Nada mordeu (timeout), relançando.")
        return False

    def play_minigame(self):
        c = self.cfg
        if c["click_on_bite"]:
            self.mouse.click()
        self.app.status("Minigame!")
        prev_err, prev_t = None, time.perf_counter()
        last_seen = prev_t
        while self.active():
            self.poll_keys()
            if not self.roblox_focused():
                self.mouse.release()
                time.sleep(0.05)
                continue
            now = time.perf_counter()
            visible, fish_y, zone_c = self.vision.read()
            if not visible:
                self.mouse.release()
                if now - last_seen > c["bar_gone_s"]:
                    break
                continue
            last_seen = now
            if fish_y is None:
                continue
            err = zone_c - fish_y          # > 0: peixe acima da zona -> subir
            dt = max(now - prev_t, 1e-3)
            derr = 0.0 if prev_err is None else (err - prev_err) / dt
            prev_err, prev_t = err, now
            u = c["kp"] * err + c["kd"] * derr
            if c["invert"]:
                u = -u
            if u > c["deadband_px"]:
                self.mouse.press()
            elif u < -c["deadband_px"]:
                self.mouse.release()
            time.sleep(0.005)
        self.mouse.release()
        if self.active():
            self.caught += 1
            self.app.post(lambda: self.app.count_var.set(f"Peixes: {self.caught}"))

    def run(self):
        while not self.quit:
            self.poll_keys()
            if not self.active() or not self.roblox_focused():
                if self.active():
                    self.app.status("Esperando a janela do Roblox")
                time.sleep(0.05)
                continue
            if self.cast() and self.wait_for_bar():
                self.play_minigame()
            if self.active():
                self.app.status("Pescando")
                self.sleep(self.cfg["wait_after_s"] + random.uniform(0, 0.3))
        self.mouse.release()


# ---------------------------------------------------------------- janela
class App:
    def __init__(self):
        self.cfg = load_config()
        self.jobs = queue.Queue()
        self.root = tk.Tk()
        self.root.title("GPO Autofish")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.macro = Macro(self)
        self.macro.start()
        self.root.after(30, self.pump)
        if self.cfg["area"] is None:
            self.log("Abra o minigame e aperte F7 pra marcar a barra.")

    def build(self):
        pad = {"padx": 10, "pady": 3}
        tk.Label(self.root, text="GPO Autofish", font=("Segoe UI", 14, "bold")).pack(**pad)
        self.status_var = tk.StringVar(value="Desligado")
        tk.Label(self.root, textvariable=self.status_var, fg="#1a73e8").pack(**pad)
        self.count_var = tk.StringVar(value="Peixes: 0")
        tk.Label(self.root, textvariable=self.count_var).pack(**pad)

        row = tk.Frame(self.root)
        row.pack(**pad)
        tk.Button(row, text="Ligar/Desligar (F6)", width=18,
                  command=lambda: self.macro.set_running(not self.macro.running)).pack(side="left", padx=2)
        tk.Button(row, text="Definir área (F7)", width=18,
                  command=self.open_selector).pack(side="left", padx=2)

        pd = tk.LabelFrame(self.root, text="Controlador PD")
        pd.pack(fill="x", **pad)
        self.kp = self.spin(pd, "Kp", "kp", 0, 0.1)
        self.kd = self.spin(pd, "Kd", "kd", 1, 0.01)

        tm = tk.LabelFrame(self.root, text="Tempos (s)")
        tm.pack(fill="x", **pad)
        self.spin(tm, "Segurar p/ lançar", "cast_hold_s", 0, 0.05)
        self.spin(tm, "Timeout esperando", "scan_timeout_s", 1, 0.5)
        self.spin(tm, "Espera depois", "wait_after_s", 2, 0.1)

        self.inv = tk.BooleanVar(value=self.cfg["invert"])
        tk.Checkbutton(self.root, text="Inverter (segurar faz a zona descer)", variable=self.inv,
                       command=self.save_fields).pack(anchor="w", padx=10)

        tk.Button(self.root, text="Testar detecção (F8)", command=self.debug).pack(**pad)
        self.text = tk.Text(self.root, height=8, width=60, font=("Consolas", 8), state="disabled")
        self.text.pack(padx=10, pady=(3, 10))

    def spin(self, parent, label, key, r, step):
        tk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=6)
        var = tk.DoubleVar(value=self.cfg[key])
        sb = tk.Spinbox(parent, from_=0, to=100, increment=step, textvariable=var, width=8,
                        command=self.save_fields)
        sb.grid(row=r, column=1, padx=6, pady=2)
        sb.bind("<FocusOut>", lambda e: self.save_fields())
        sb.bind("<Return>", lambda e: self.save_fields())
        self.fields = getattr(self, "fields", {})
        self.fields[key] = var
        return var

    def save_fields(self):
        for key, var in self.fields.items():
            try:
                self.cfg[key] = float(var.get())
            except (tk.TclError, ValueError):
                pass
        self.cfg["invert"] = bool(self.inv.get())
        save_config(self.cfg)

    # chamadas vindas da thread do macro rodam aqui (Tk só na thread principal)
    def post(self, fn):
        self.jobs.put(fn)

    def pump(self):
        while not self.jobs.empty():
            self.jobs.get()()
        self.root.after(30, self.pump)

    def status(self, s):
        self.post(lambda: self.status_var.set(s))

    def log(self, msg):
        def do():
            self.text.config(state="normal")
            self.text.insert("end", msg + "\n")
            self.text.see("end")
            self.text.config(state="disabled")
        self.post(do)
        print(msg)

    def open_selector(self):
        if self.macro.paused:
            return
        self.macro.paused = True
        self.macro.mouse.release()

        def done(area):
            if area:
                self.cfg["area"] = area
                save_config(self.cfg)
                self.log(f"Área salva: {area}. Aperte F8 com o minigame aberto pra testar.")
            self.macro.paused = False
        AreaSelector(self.root, self.cfg["area"], done)

    def debug(self):
        if self.cfg["area"] is None:
            self.log("Defina a área primeiro (F7).")
            return
        threading.Thread(target=lambda: self.macro.vision.__class__(self.cfg).debug(self.log),
                         daemon=True).start()

    def close(self):
        self.macro.quit = True
        self.macro.mouse.release()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
