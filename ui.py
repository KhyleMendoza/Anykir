import json
import os
import tkinter as tk
from tkinter import ttk

FONT = "Segoe UI"
SEMI = "Segoe UI Semibold"
MONO = "Consolas"

LIGHT = {"primary": "#4E5B92", "onPrimary": "#FFFFFF", "primaryHover": "#43507F", "primaryContainer": "#DCE1FF",
         "onPrimaryContainer": "#364479", "secondaryContainer": "#DEE1F9", "onSecondaryContainer": "#424659",
         "surface": "#FAF8FF", "onSurface": "#1A1B21", "onSurfaceVariant": "#45464F", "low": "#F4F2FA",
         "container": "#EAE8F2", "high": "#E9E7EF", "highest": "#E3E1E9", "outline": "#767680",
         "outlineVariant": "#C6C5D0", "error": "#BA1A1A", "okBg": "#DDF1E1", "okFg": "#1E6B33",
         "warnBg": "#FFE9C7", "warnFg": "#7A4A00", "badBg": "#FFDAD6", "badFg": "#93000A", "chrome": "#EFEDF4"}
DARK = {"primary": "#B7C4FF", "onPrimary": "#1E2D61", "primaryHover": "#C9D3FF", "primaryContainer": "#364479",
        "onPrimaryContainer": "#DCE1FF", "secondaryContainer": "#424659", "onSecondaryContainer": "#DEE1F9",
        "surface": "#121318", "onSurface": "#E3E1E9", "onSurfaceVariant": "#C6C5D0", "low": "#1A1B21",
        "container": "#23242B", "high": "#292A2F", "highest": "#34343A", "outline": "#90909A",
        "outlineVariant": "#45464F", "error": "#FFB4AB", "okBg": "#1B3323", "okFg": "#8FD8A0",
        "warnBg": "#3A2A0C", "warnFg": "#F5C26B", "badBg": "#5C1A15", "badFg": "#FFDAD6", "chrome": "#1E1F25"}
CHIPS = {"info": ("secondaryContainer", "onSecondaryContainer"), "ok": ("okBg", "okFg"), "off": ("high", "onSurfaceVariant"),
         "bad": ("badBg", "badFg"), "warn": ("warnBg", "warnFg"), "use": ("primaryContainer", "onPrimaryContainer")}
C = dict(LIGHT)
_mode = {"pref": None, "dark": False}
_roots = []
_painted = []
_redraws = []


def _settings_file():
    try:
        from common import config_dir
        return os.path.join(config_dir(), "ui.json")
    except Exception:
        return None


def preference():
    if _mode["pref"] is None:
        _mode["pref"] = "system"
        try:
            with open(_settings_file(), encoding="utf-8") as f:
                value = json.load(f).get("theme")
            if value in ("system", "light", "dark"):
                _mode["pref"] = value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return _mode["pref"]


def windows_uses_dark():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def is_dark():
    pref = preference()
    return windows_uses_dark() if pref == "system" else pref == "dark"


def _load_palette():
    _mode["dark"] = is_dark()
    C.clear()
    C.update(DARK if _mode["dark"] else LIGHT)


def set_preference(pref):
    _mode["pref"] = pref
    path = _settings_file()
    if path:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"theme": pref}, f)
        except OSError:
            pass
    refresh()


def refresh():
    _load_palette()
    for root in list(_roots):
        try:
            if not getattr(root, "_anykir_classic", False):
                _style(root)
            _title_bar(root)
        except tk.TclError:
            _roots.remove(root)
    alive = []
    for widget, roles in _painted:
        try:
            if widget.winfo_exists():
                widget.configure(**{opt: _colour(role) for opt, role in roles.items()})
                alive.append((widget, roles))
        except tk.TclError:
            pass
    _painted[:] = alive
    for w in list(_redraws):
        try:
            if w.winfo_exists():
                w._render()
                continue
        except tk.TclError:
            pass
        _redraws.remove(w)


def _colour(role):
    return C[role] if role in C else role


def paint(widget, **roles):
    widget.configure(**{opt: _colour(role) for opt, role in roles.items()})
    _painted.append((widget, roles))
    return widget


def _title_bar(win):
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        value = ctypes.c_int(1 if _mode["dark"] and not getattr(win, "_anykir_classic", False) else 0)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                         ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def _title_bar_when_shown(win):
    _title_bar(win)
    win.bind("<Map>", lambda e: _title_bar(win) if e.widget is win else None, add="+")


def _rgb(hexc):
    return tuple(int(hexc[i:i + 2], 16) for i in (1, 3, 5))


def _images(root):
    if not hasattr(root, "_anykir_images"):
        root._anykir_images = {}
    return root._anykir_images


MIDDLE = 64


def _shape(root, fill, radius, outline=None, width=0, behind=None):
    key = ("shape", fill, radius, outline, width, behind)
    cache = _images(root)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    k = 3
    size = (radius + 1) * 2 + MIDDLE
    big = size * k
    img = Image.new("RGBA", (big, big), (_rgb(behind) + (255,)) if behind else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    box = (0, 0, big - 1, big - 1)
    if outline and width:
        d.rounded_rectangle(box, radius=radius * k, fill=_rgb(outline) + (255,))
        inset = width * k
        d.rounded_rectangle((inset, inset, big - 1 - inset, big - 1 - inset), radius=max(1, (radius - width) * k),
                            fill=_rgb(fill) + (255,))
    else:
        d.rounded_rectangle(box, radius=radius * k, fill=_rgb(fill) + (255,))
    img = img.reduce(k)
    if behind:
        img = img.convert("RGB")
    photo = ImageTk.PhotoImage(img, master=root)
    cache[key] = photo
    return photo


def _switch_image(root, on, behind):
    key = ("switch", on, behind, _mode["dark"])
    cache = _images(root)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    k = 4
    w, h = 44, 24
    img = Image.new("RGBA", (w * k, h * k), _rgb(behind) + (255,))
    d = ImageDraw.Draw(img)
    track = C["primary"] if on else C["highest"]
    d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=h * k // 2, fill=_rgb(track) + (255,),
                        outline=None if on else _rgb(C["outline"]) + (255,), width=0 if on else 2 * k)
    knob = (h - 6) if on else (h - 10)
    cx = (w - h / 2) if on else h / 2
    cy = h / 2
    colour = C["onPrimary"] if on else C["outline"]
    d.ellipse(((cx - knob / 2) * k, (cy - knob / 2) * k, (cx + knob / 2) * k, (cy + knob / 2) * k),
              fill=_rgb(colour) + (255,))
    photo = ImageTk.PhotoImage(img.reduce(k).convert("RGB"), master=root)
    cache[key] = photo
    return photo


def _check_image(root, on, behind):
    key = ("check", on, behind, _mode["dark"])
    cache = _images(root)
    if key in cache:
        return cache[key]
    from PIL import Image, ImageDraw, ImageTk
    k, n, gap = 4, 18, 8
    img = Image.new("RGBA", ((n + gap) * k, n * k), _rgb(behind) + (255,))
    d = ImageDraw.Draw(img)
    if on:
        d.rounded_rectangle((k, k, (n - 1) * k, (n - 1) * k), radius=3 * k, fill=_rgb(C["primary"]) + (255,))
        d.line([(5 * k, 9.5 * k), (8 * k, 12.5 * k), (13.5 * k, 6 * k)], fill=_rgb(C["onPrimary"]) + (255,),
               width=int(2.2 * k), joint="curve")
    else:
        d.rounded_rectangle((k, k, (n - 1) * k, (n - 1) * k), radius=3 * k, outline=_rgb(C["onSurfaceVariant"]) + (255,),
                            width=2 * k)
    photo = ImageTk.PhotoImage(img.reduce(k).convert("RGB"), master=root)
    cache[key] = photo
    return photo


def _element(root, s, name, radius, states):
    created = getattr(root, "_anykir_elements", None)
    if created is None:
        created = root._anykir_elements = set()
    if name not in created:
        normal, *rest = states
        args = [_shape(root, *normal)] + [(state, _shape(root, *spec)) for state, spec in rest]
        s.element_create(name, "image", *args, border=radius + 1, padding=0, sticky="nsew",
                         width=2 * radius + 2, height=2 * radius + 2)
        created.add(name)
    return name


def apply(root):
    hide(root)
    if not _roots:
        _load_palette()
    if root not in _roots:
        _roots.append(root)
    root._anykir_classic = False
    _style(root)
    _title_bar_when_shown(root)
    return ttk.Style(root)


def hide(win):
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        pass


def reveal(win, delay=120):
    def show():
        try:
            win.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
    win.after(delay, show)


def classic(root, on=True):
    root._anykir_classic = on
    s = ttk.Style(root)
    if on:
        s.theme_use("vista" if "vista" in s.theme_names() else "default")
        root.configure(bg="SystemButtonFace")
    else:
        _style(root)
    _title_bar(root)


def themed_window(win):
    paint(win, bg="surface")
    hide(win)
    reveal(win, 150)
    _title_bar_when_shown(win)
    if win not in _roots:
        _roots.append(win)


def _style(root):
    if isinstance(root, tk.Toplevel):
        return
    c = C
    m = "d" if _mode["dark"] else "l"
    root.configure(bg=c["surface"])
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=c["surface"], foreground=c["onSurface"], font=(FONT, 10),
                bordercolor=c["outlineVariant"], lightcolor=c["surface"], darkcolor=c["surface"],
                troughcolor=c["low"], focuscolor=c["primary"], selectbackground=c["secondaryContainer"],
                selectforeground=c["onSurface"], fieldbackground=c["surface"], insertcolor=c["onSurface"])

    for name, bg in (("TFrame", "surface"), ("Card.TFrame", "low"), ("Ok.TFrame", "okBg"), ("Warn.TFrame", "warnBg")):
        s.configure(name, background=c[bg])
    for prefix, bg in (("", "surface"), ("Card.", "low"), ("Hover.", "container"), ("Ok.", "okBg"), ("Warn.", "warnBg")):
        s.configure(f"{prefix}TLabel", background=c[bg], foreground=c["onSurface"])
        s.configure(f"{prefix}Muted.TLabel", background=c[bg], foreground=c["onSurfaceVariant"])
        s.configure(f"{prefix}Small.TLabel", background=c[bg], foreground=c["onSurfaceVariant"], font=(FONT, 9))
        s.configure(f"{prefix}Label.TLabel", background=c[bg], foreground=c["onSurfaceVariant"], font=(SEMI, 9))
        s.configure(f"{prefix}Title.TLabel", background=c[bg], foreground=c["onSurface"], font=(SEMI, 11))
        s.configure(f"{prefix}Big.TLabel", background=c[bg], foreground=c["onSurface"], font=(MONO, 17, "bold"))
        s.configure(f"{prefix}Error.TLabel", background=c[bg], foreground=c["error"])
        s.configure(f"{prefix}Chevron.TLabel", background=c[bg], foreground=c["onSurfaceVariant"], font=(FONT, 16))
    s.configure("Ok.Title.TLabel", foreground=c["okFg"])
    s.configure("Warn.Title.TLabel", foreground=c["warnFg"])
    s.configure("Headline.TLabel", background=c["surface"], foreground=c["onSurface"], font=(FONT, 17))
    s.configure("AppTitle.TLabel", background=c["surface"], foreground=c["onSurface"], font=(FONT, 16))

    for style, fill in (("Card.TFrame", "low"), ("Hover.TFrame", "container"), ("Ok.TFrame", "okBg"),
                        ("Warn.TFrame", "warnBg")):
        el = _element(root, s, f"{style}.{m}", 12, [(c[fill], 12, None, 0, c["surface"])])
        s.layout(style, [(el, {"sticky": "nsew"})])
        s.configure(style, background=c[fill])
    s.configure("Plain.TFrame", background=c["low"])
    s.configure("HoverPlain.TFrame", background=c["container"])
    s.configure("OkPlain.TFrame", background=c["okBg"])
    s.configure("WarnPlain.TFrame", background=c["warnBg"])

    def button(style, el, fg, off_fg, font=(SEMI, 10), padding=(16, 8)):
        s.layout(style, [(el, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        s.configure(style, foreground=fg, font=font, padding=padding, anchor="center", relief="flat")
        s.map(style, foreground=[("disabled", off_fg)])

    for behind, sfx in (("surface", ""), ("low", "Card"), ("okBg", "Ok"), ("warnBg", "Warn")):
        b = c[behind]
        banner = sfx in ("Ok", "Warn")
        if not banner:
            el = _element(root, s, f"filled.{m}.{behind}", 18, [
                (c["primary"], 18, None, 0, b), ("disabled", (c["highest"], 18, None, 0, b)),
                ("pressed", (c["primaryHover"], 18, None, 0, b)), ("active", (c["primaryHover"], 18, None, 0, b))])
            button(f"{sfx}Filled.TButton", el, c["onPrimary"], c["outline"], font=(SEMI, 11), padding=(22, 10))
        el = _element(root, s, f"tonal.{m}.{behind}", 16, [
            (c["secondaryContainer"], 16, None, 0, b), ("disabled", (c["high"], 16, None, 0, b)),
            ("pressed", (c["highest"], 16, None, 0, b)), ("active", (c["highest"], 16, None, 0, b))])
        button(f"{sfx}Tonal.TButton", el, c["onSecondaryContainer"], c["outline"], padding=(16, 7))
        el = _element(root, s, f"danger.{m}.{behind}", 16, [
            (c["badBg"], 16, None, 0, b), ("pressed", (c["highest"], 16, None, 0, b)), ("active", (c["highest"], 16, None, 0, b))])
        button(f"{sfx}Danger.TButton", el, c["badFg"], c["outline"], padding=(16, 7))
        if banner:
            continue
        el = _element(root, s, f"text.{m}.{behind}", 16, [
            (b, 16, None, 0, b), ("pressed", (c["highest"], 16, None, 0, b)), ("active", (c["high"], 16, None, 0, b))])
        button(f"{sfx}Text.TButton", el, c["primary"], c["outline"], padding=(12, 7))
        s.configure(f"{sfx}Text.TButton", width=-5)
        el = _element(root, s, f"icon.{m}.{behind}", 18, [
            (b, 18, None, 0, b), ("pressed", (c["highest"], 18, None, 0, b)), ("active", (c["high"], 18, None, 0, b))])
        button(f"{sfx}Icon.TButton", el, c["onSurface"], c["outline"], font=(FONT, 14), padding=(8, 1))
        el = _element(root, s, f"seg.{m}.{behind}", 15, [
            (b, 15, c["outline"], 1, b), ("active", (c["high"], 15, c["outline"], 1, b))])
        button(f"{sfx}Seg.TButton", el, c["onSurface"], c["outline"], font=(FONT, 9), padding=(12, 5))
        el = _element(root, s, f"segon.{m}.{behind}", 15, [
            (c["secondaryContainer"], 15, c["outline"], 1, b), ("active", (c["highest"], 15, c["outline"], 1, b))])
        button(f"{sfx}SegOn.TButton", el, c["onSecondaryContainer"], c["outline"], font=(SEMI, 9), padding=(12, 5))
        field = _element(root, s, f"field.{m}.{behind}", 5, [
            (c["surface"], 5, c["outline"], 1, b), ("focus", (c["surface"], 5, c["primary"], 2, b)),
            ("disabled", (c["low"], 5, c["outlineVariant"], 1, b))])
        s.layout(f"{sfx}Field.TEntry", [(field, {"sticky": "nsew", "children": [
            ("Entry.padding", {"sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]})]})])
        s.configure(f"{sfx}Field.TEntry", foreground=c["onSurface"], fieldbackground=c["surface"], padding=(12, 10),
                    insertcolor=c["onSurface"], selectbackground=c["secondaryContainer"], selectforeground=c["onSurface"])
        sw = f"switch.{m}.{behind}"
        created = getattr(root, "_anykir_elements", set())
        if sw not in created:
            s.element_create(sw, "image", _switch_image(root, False, b), ("selected", _switch_image(root, True, b)),
                             sticky="e")
            root._anykir_elements = created | {sw}
        s.layout(f"{sfx}Switch.TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
            (sw, {"side": "right", "sticky": ""}), ("Checkbutton.label", {"side": "left", "sticky": "w"})]})])
        s.configure(f"{sfx}Switch.TCheckbutton", background=b, foreground=c["onSurface"], padding=(0, 4))
        s.map(f"{sfx}Switch.TCheckbutton", background=[("active", b)])
        ck = f"check.{m}.{behind}"
        if ck not in root._anykir_elements:
            s.element_create(ck, "image", _check_image(root, False, b), ("selected", _check_image(root, True, b)),
                             sticky="")
            root._anykir_elements = root._anykir_elements | {ck}
        s.layout(f"{sfx}TCheckbutton", [("Checkbutton.padding", {"sticky": "nsew", "children": [
            (ck, {"side": "left", "sticky": ""}), ("Checkbutton.label", {"side": "left", "sticky": "w"})]})])
        s.configure(f"{sfx}TCheckbutton", background=b, foreground=c["onSurface"], padding=(0, 4, 0, 4))
        s.map(f"{sfx}TCheckbutton", background=[("active", b)])
        s.layout(f"{sfx}Field.TCombobox", [(field, {"sticky": "nsew", "children": [
            ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
        s.configure(f"{sfx}Field.TCombobox", foreground=c["onSurface"], fieldbackground=c["surface"], background=c["surface"],
                    arrowcolor=c["onSurfaceVariant"], padding=(10, 6), arrowsize=14)
        s.map(f"{sfx}Field.TCombobox", fieldbackground=[("readonly", c["surface"])], foreground=[("readonly", c["onSurface"])],
              selectbackground=[("readonly", c["surface"])], selectforeground=[("readonly", c["onSurface"])],
              background=[("active", c["surface"])])
    s.layout("Address.TCombobox", [("Combobox.field", {"sticky": "nsew", "children": [
        ("Combobox.downarrow", {"side": "right", "sticky": "ns"}),
        ("Combobox.padding", {"sticky": "nsew", "children": [("Combobox.textarea", {"sticky": "nsew"})]})]})])
    s.configure("Address.TCombobox", fieldbackground=c["low"], background=c["low"], foreground=c["onSurface"],
                bordercolor=c["low"], lightcolor=c["low"], darkcolor=c["low"], arrowcolor=c["onSurfaceVariant"],
                padding=(0, 0), arrowsize=16)
    s.map("Address.TCombobox", fieldbackground=[("readonly", c["low"])], foreground=[("readonly", c["onSurface"])],
          selectbackground=[("readonly", c["low"])], selectforeground=[("readonly", c["onSurface"])],
          background=[("active", c["low"])], bordercolor=[("focus", c["low"])])

    for kind, (bg, fg) in CHIPS.items():
        for behind, sfx in (("surface", ""), ("low", "Card")):
            el = _element(root, s, f"chip.{kind}.{m}.{behind}", 8, [(c[bg], 8, None, 0, c[behind])])
            style = f"{sfx}Chip{kind}.TLabel"
            s.layout(style, [(el, {"sticky": "nsew", "children": [
                ("Label.padding", {"sticky": "nsew", "children": [("Label.label", {"sticky": "nsew"})]})]})])
            s.configure(style, foreground=c[fg], background=c[bg], font=(SEMI, 9), padding=(10, 4))

    s.configure("TButton", background=c["secondaryContainer"], foreground=c["onSecondaryContainer"],
                bordercolor=c["secondaryContainer"], lightcolor=c["secondaryContainer"],
                darkcolor=c["secondaryContainer"], padding=(10, 5), relief="flat")
    s.map("TButton", background=[("disabled", c["low"]), ("active", c["highest"])],
          foreground=[("disabled", c["outline"])], lightcolor=[("active", c["highest"])],
          darkcolor=[("active", c["highest"])], bordercolor=[("active", c["highest"])])
    s.configure("TEntry", fieldbackground=c["surface"], foreground=c["onSurface"], bordercolor=c["outline"],
                lightcolor=c["outline"], darkcolor=c["outline"], padding=(6, 4))
    s.map("TEntry", bordercolor=[("focus", c["primary"])], lightcolor=[("focus", c["primary"])])
    s.configure("TCombobox", fieldbackground=c["surface"], foreground=c["onSurface"], background=c["high"],
                bordercolor=c["outline"], arrowcolor=c["onSurfaceVariant"])
    s.map("TCombobox", fieldbackground=[("readonly", c["surface"])], foreground=[("readonly", c["onSurface"])])
    s.configure("TSeparator", background=c["outlineVariant"])
    s.configure("Treeview", background=c["low"], fieldbackground=c["low"], foreground=c["onSurface"], rowheight=38,
                bordercolor=c["low"], lightcolor=c["low"], darkcolor=c["low"], font=(FONT, 10))
    s.map("Treeview", background=[("selected", c["secondaryContainer"])],
          foreground=[("selected", c["onSecondaryContainer"])])
    s.configure("Treeview.Heading", background=c["low"], foreground=c["onSurfaceVariant"], relief="flat",
                font=(SEMI, 9), bordercolor=c["outlineVariant"], lightcolor=c["low"], darkcolor=c["low"],
                padding=(8, 8))
    s.map("Treeview.Heading", background=[("active", c["container"])])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=c["high"], bordercolor=c["low"],
                    arrowcolor=c["onSurfaceVariant"], troughcolor=c["low"], lightcolor=c["high"],
                    darkcolor=c["high"], gripcount=0)
        s.map(f"{orient}.TScrollbar", background=[("active", c["highest"])])
    s.configure("TProgressbar", background=c["primary"], troughcolor=c["high"], bordercolor=c["high"],
                lightcolor=c["primary"], darkcolor=c["primary"])
    for key, value in (("*TCombobox*Listbox.font", (FONT, 10)), ("*TCombobox*Listbox.background", c["low"]),
                       ("*TCombobox*Listbox.foreground", c["onSurface"]),
                       ("*TCombobox*Listbox.selectBackground", c["secondaryContainer"]),
                       ("*TCombobox*Listbox.selectForeground", c["onSurface"]),
                       ("*Menu.background", c["low"]), ("*Menu.foreground", c["onSurface"]),
                       ("*Menu.activeBackground", c["secondaryContainer"]),
                       ("*Menu.activeForeground", c["onSurface"])):
        root.option_add(key, value)


def logo_image(parent, size, server=False):
    try:
        from PIL import ImageTk
        import logo
        photo = ImageTk.PhotoImage((logo.server_image if server else logo.image)(size), master=parent)
        parent._anykir_logo = getattr(parent, "_anykir_logo", []) + [photo]
        return photo
    except Exception:
        return None


def appbar(parent, version=None, back=None, title="AnyKir", chip_text=None, chip_kind="ok", server=False):
    bar = ttk.Frame(parent, padding=(8 if back else 20, 10, 20, 4))
    bar.pack(fill="x")
    if back:
        b = ttk.Button(bar, text="←", style="Icon.TButton", command=back, width=2)
        b.pack(side="left", padx=(0, 6))
        bar.back = b
    photo = logo_image(bar, 30, server)
    if photo:
        ttk.Label(bar, image=photo).pack(side="left", padx=(0, 10))
    ttk.Label(bar, text=title, style="AppTitle.TLabel").pack(side="left")
    if version:
        chip(bar, f"v{version}", "info").pack(side="right")
    bar.status = None
    if chip_text:
        bar.status = chip(bar, chip_text, chip_kind)
        bar.status.pack(side="right", padx=(0, 8))
    return bar


def body(parent, padx=20, pady=(4, 18)):
    f = ttk.Frame(parent)
    f.pack(fill="both", expand=True, padx=padx, pady=pady)
    return f


def headline(parent, text, pady=(6, 10)):
    lbl = ttk.Label(parent, text=text, style="Headline.TLabel")
    lbl.pack(anchor="w", pady=pady)
    return lbl


def card(parent, padding=(16, 14), style="Card.TFrame", **pack):
    f = ttk.Frame(parent, style=style, padding=padding)
    f.pack(**({"fill": "x", "pady": (0, 10)} | pack))
    return f


def field(parent, label, textvariable, show="", on_enter=None, on_card=False, font=(FONT, 12), width=30):
    sfx = "Card" if on_card else ""
    box = ttk.Frame(parent, style="Plain.TFrame" if on_card else "TFrame")
    box.pack(fill="x", pady=(4, 4))
    entry = ttk.Entry(box, textvariable=textvariable, show=show, style=f"{sfx}Field.TEntry", font=font, width=width)
    entry.pack(fill="x", pady=(8, 0))
    tag = ttk.Label(box, text=f" {label} ", style="Card.Small.TLabel" if on_card else "Small.TLabel")
    tag.place(x=10, y=0)
    if on_enter:
        entry.bind("<Return>", lambda _e: on_enter())
    entry.tag = tag
    entry.box = box
    return entry


def chip(parent, text="", kind="info", textvariable=None, on_card=False):
    lbl = ttk.Label(parent, text=text, textvariable=textvariable, style=f"{'Card' if on_card else ''}Chip{kind}.TLabel")
    lbl._on_card = on_card
    return lbl


def set_chip(lbl, text, kind):
    lbl.configure(text=text, style=f"{'Card' if getattr(lbl, '_on_card', False) else ''}Chip{kind}.TLabel")


def divider(parent, bg="low", pady=8):
    line = paint(tk.Frame(parent, height=1), bg="outlineVariant")
    line.pack(fill="x", pady=pady)
    return line


def circle_icon(parent, kind, size=44, bg="low"):
    lbl = paint(tk.Label(parent, bd=0, highlightthickness=0), bg=bg)

    def render():
        try:
            from PIL import ImageTk
            lbl._photo = ImageTk.PhotoImage(_badge(kind, size, C[bg]), master=lbl)
            lbl.configure(image=lbl._photo, bg=C[bg])
        except Exception:
            pass
    lbl._render = render
    render()
    _redraws.append(lbl)
    return lbl


def _badge(kind, size, behind):
    from PIL import Image, ImageDraw
    k = 4
    s = size * k
    img = Image.new("RGBA", (s, s), _rgb(behind) + (255,))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=_rgb(C["primaryContainer"]) + (255,))
    col = _rgb(C["primary"]) + (255,)
    w = max(2, int(s * 0.045))
    if kind == "phone":
        d.rounded_rectangle((s * 0.38, s * 0.24, s * 0.62, s * 0.76), radius=s * 0.04, outline=col, width=w)
        d.line((s * 0.47, s * 0.68, s * 0.53, s * 0.68), fill=col, width=w)
    else:
        x0, y0, x1, y1 = (s * 0.26, s * 0.3, s * 0.74, s * 0.6) if kind == "pc" else (s * 0.22, s * 0.3, s * 0.6, s * 0.58)
        if kind == "connect":
            x0, x1 = s * 0.4, s * 0.78
        d.rounded_rectangle((x0, y0, x1, y1), radius=s * 0.03, outline=col, width=w)
        mid = (x0 + x1) / 2
        d.line((mid, y1, mid, y1 + s * 0.1), fill=col, width=w)
        d.line((mid - s * 0.09, y1 + s * 0.1, mid + s * 0.09, y1 + s * 0.1), fill=col, width=w)
        ay = (y0 + y1) / 2
        if kind == "share":
            d.line((s * 0.64, ay, s * 0.8, ay), fill=col, width=w)
            d.polygon([(s * 0.84, ay), (s * 0.77, ay - s * 0.05), (s * 0.77, ay + s * 0.05)], fill=col)
        elif kind == "connect":
            d.line((s * 0.18, ay, s * 0.34, ay), fill=col, width=w)
            d.polygon([(s * 0.38, ay), (s * 0.31, ay - s * 0.05), (s * 0.31, ay + s * 0.05)], fill=col)
    return img.resize((size, size), Image.LANCZOS).convert("RGB")


class ListCard:

    def __init__(self, parent, title, sub, on_pick, on_open=None, icon="pc", chip_text=None):
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10, 10, 10), cursor="hand2")
        self.frame.pack(fill="x", pady=(0, 8))
        self.icon = circle_icon(self.frame, icon, 40, bg="low")
        self.icon.pack(side="left", padx=(0, 14))
        self._kind = icon
        texts = ttk.Frame(self.frame, style="Plain.TFrame")
        texts.pack(side="left", fill="x", expand=True)
        self.title = ttk.Label(texts, text=title, style="Card.Title.TLabel")
        self.title.pack(anchor="w")
        self.sub = auto_wrap(ttk.Label(texts, text=sub, style="Card.Muted.TLabel"))
        self.sub.pack(anchor="w", fill="x")
        self.arrow = ttk.Label(self.frame, text="›", style="Card.Chevron.TLabel")
        self.arrow.pack(side="right", padx=(8, 4), before=texts)
        self.tag = None
        if chip_text:
            self.tag = chip(self.frame, chip_text, "info", on_card=True)
            self.tag.pack(side="right", before=texts, padx=(8, 0))
        self._texts = texts
        self.frame.bind("<Configure>", self._fit, add="+")
        widgets = [self.frame, self.icon, texts, self.title, self.sub, self.arrow] + ([self.tag] if self.tag else [])
        for w in widgets:
            w.bind("<Button-1>", lambda _e: on_pick(self), add="+")
            if on_open:
                w.bind("<Double-1>", lambda _e: on_open(self), add="+")
            w.bind("<Enter>", lambda _e: self._hover(True), add="+")
            w.bind("<Leave>", lambda _e: self._hover(False), add="+")

    def _fit(self, e):
        if self.tag is None or e.width < 50:
            return
        show = e.width >= 400
        if show != getattr(self, "_tag_shown", True):
            self._tag_shown = show
            if show:
                self.tag.pack(side="right", before=self._texts, padx=(8, 0))
            else:
                self.tag.pack_forget()

    def _hover(self, on):
        p = "Hover." if on else "Card."
        self.frame.configure(style="Hover.TFrame" if on else "Card.TFrame")
        self._texts.configure(style="HoverPlain.TFrame" if on else "Plain.TFrame")
        self.title.configure(style=p + "Title.TLabel")
        self.sub.configure(style=p + "Muted.TLabel")
        self.arrow.configure(style=p + "Chevron.TLabel")
        bg = "container" if on else "low"
        try:
            from PIL import ImageTk
            self.icon._photo = ImageTk.PhotoImage(_badge(self._kind, 40, C[bg]), master=self.icon)
            self.icon.configure(image=self.icon._photo, bg=C[bg])
        except Exception:
            pass

    def set(self, title, sub, chip_text=None):
        if self.title.cget("text") != title:
            self.title.configure(text=title)
        if self.sub.cget("text") != sub:
            self.sub.configure(text=sub)
        if self.tag is not None and chip_text is not None and self.tag.cget("text") != chip_text:
            self.tag.configure(text=chip_text)

    def destroy(self):
        self.frame.destroy()


def setting_row(parent, title, sub="", variable=None, command=None, last=False):
    row = ttk.Frame(parent, style="Plain.TFrame")
    row.pack(fill="x")
    inner = ttk.Frame(row, style="Plain.TFrame", padding=(0, 10))
    inner.pack(fill="x")
    texts = ttk.Frame(inner, style="Plain.TFrame")
    texts.pack(side="left", fill="x", expand=True)
    row.title = ttk.Label(texts, text=title, style="Card.Title.TLabel")
    row.title.pack(anchor="w")
    row.sub = auto_wrap(ttk.Label(texts, text=sub, style="Card.Small.TLabel"))
    if sub:
        row.sub.pack(anchor="w", fill="x")
    row.right = ttk.Frame(inner, style="Plain.TFrame")
    row.right.pack(side="right", before=texts, padx=(12, 0))
    row.switch = None
    if variable is not None:
        row.switch = ttk.Checkbutton(row.right, variable=variable, command=command, style="CardSwitch.TCheckbutton",
                                     takefocus=True)
        row.switch.pack(side="right")
    if not last:
        divider(row, pady=0)
    return row


def expander(parent, label, on_card=False):
    head = ttk.Button(parent, style=("Card" if on_card else "") + "Text.TButton")
    head.pack(fill="x", pady=(4, 0))
    content = ttk.Frame(parent, style="Plain.TFrame" if on_card else "TFrame")
    state = {"open": False}
    up, down = "▴", "▾"

    def toggle():
        state["open"] = not state["open"]
        head.configure(text=f"{label}   {up if state['open'] else down}")
        if state["open"]:
            content.pack(fill="x", after=head)
        else:
            content.pack_forget()
    head.configure(text=f"{label}   {down}", command=toggle)
    content.toggle = toggle
    content.head = head
    content.is_open = lambda: state["open"]
    return content


NAMES = {"system": "Same as Windows", "light": "Light", "dark": "Dark"}


def appearance(parent, on_card=False):
    row = ttk.Frame(parent, style="Plain.TFrame" if on_card else "TFrame")
    buttons = {}
    sfx = "Card" if on_card else ""

    def show():
        for key, b in buttons.items():
            b.configure(style=f"{sfx}{'SegOn' if preference() == key else 'Seg'}.TButton")

    def pick(key):
        set_preference(key)
        show()
    for key, text in NAMES.items():
        b = ttk.Button(row, text=text, command=lambda k=key: pick(k))
        b.pack(side="left", padx=(0, 6))
        buttons[key] = b
    show()
    row._render = show
    _redraws.append(row)
    return row


def scroll_area(parent, padx=20, pady=(4, 18)):
    outer = ttk.Frame(parent)
    outer.pack(fill="both", expand=True)
    canvas = paint(tk.Canvas(outer, highlightthickness=0, bd=0, yscrollincrement=24), bg="surface")
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    holder = ttk.Frame(canvas)
    inner = ttk.Frame(holder)
    inner.pack(fill="both", expand=True, padx=padx, pady=pady)
    item = canvas.create_window(0, 0, window=holder, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)

    shown = {"bar": False}

    def sync(_e=None):
        canvas.configure(scrollregion=(0, 0, holder.winfo_reqwidth(), holder.winfo_reqheight()))
        need = holder.winfo_reqheight() > canvas.winfo_height() + 1
        if need == shown["bar"]:
            return
        shown["bar"] = need
        if need:
            bar.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")
        else:
            bar.place_forget()
            canvas.yview_moveto(0)

    def wheel(e):
        if shown["bar"] and str(e.widget).startswith(str(outer)):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    holder.bind("<Configure>", sync)
    canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(item, width=e.width), sync()))
    outer.bind_all("<MouseWheel>", wheel)
    outer.bind("<Destroy>", lambda e: outer.unbind_all("<MouseWheel>") if e.widget is outer else None)
    inner.outer = outer
    inner.canvas = canvas
    inner.see_top = lambda: canvas.yview_moveto(0)
    return inner


def auto_wrap(label, reserve=8, minimum=60):
    label.configure(wraplength=minimum)
    state = {"w": None, "want": None, "job": None}

    def apply():
        state["job"] = None
        if state["want"] != state["w"]:
            state["w"] = state["want"]
            try:
                label.configure(wraplength=state["w"])
            except tk.TclError:
                pass

    def fit(e):
        state["want"] = max(e.width - reserve, minimum)
        if state["w"] is None or e.width < 2:
            apply()
        elif state["job"] is None:
            state["job"] = label.after(90, apply)
    label.master.bind("<Configure>", fit, add="+")
    return label


def fit_font(row, widgets, big, small, below=440):
    state = {"small": None}

    def fit(e):
        narrow = e.width < below
        if narrow != state["small"]:
            state["small"] = narrow
            for w in widgets:
                w.configure(font=small if narrow else big)
    row.bind("<Configure>", fit, add="+")


def window_size(root, width, height, min_width=340, min_height=360):
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w, h = min(width, sw - 40), min(height, sh - 90)
    root.minsize(min(min_width, w), min(min_height, h))
    root.resizable(True, True)
    root.geometry(f"{w}x{h}")


def size_to_content(root, width, content, extra=80):
    root.update_idletasks()
    h = content.winfo_reqheight() + extra
    window_size(root, width, h, min_height=min(h, 360))
