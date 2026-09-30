"""chatrepo-computer for Linux — protocol v1 (client: src/host/tools/computer/computer-driver.ts).

Long-lived helper: one JSON request per stdin line, exactly one JSON reply per request on stdout.
Standard library only; libX11/libXtst (ctypes) and gi/Atspi are loaded lazily at first use, so this
module imports on any machine (developer checks run on macOS without X11 or gi).

Layout:
  * pure logic — key/role/action tables, scroll math, parameter validation, .desktop Exec parsing;
  * X11 backend — ctypes on libX11.so.6 + libXtst.so.6 (XTEST input, EWMH windows);
  * AT-SPI backend — gi Atspi 2.0 (element tree), works on X11 and Wayland;
  * Driver — request dispatch; main() — stdio loop.
On Wayland input and capture belong to wayland_portal.py; here only elements and launch work.
"""
import base64
import contextlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zlib

PROTOCOL = 1
CAPTURE_MAX_EDGE = max(256, min(8192, int(os.getenv("COMPUTER_CAPTURE_MAX_EDGE", "1568"))))


class DriverError(Exception):
    """A request-level failure mapped to a protocol error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def bad_request(message):
    return DriverError("bad_request", message)


def optional_text(value):
    if value is None:
        return None
    value = str(value)
    return value


# --------------------------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------------------------

# Protocol key name (lower case, no spaces) → X keysym name (XStringToKeysym).
KEY_NAMES = {
    "enter": "Return", "return": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace",
    # «delete (=backspace на macOS, Delete на Windows/X11 — как в текущем коде), forwarddelete|del»
    "delete": "Delete", "forwarddelete": "Delete", "del": "Delete",
    "escape": "Escape", "esc": "Escape", "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "Prior", "pagedown": "Next",
    "left": "Left", "arrowleft": "Left", "right": "Right", "arrowright": "Right",
    "up": "Up", "arrowup": "Up", "down": "Down", "arrowdown": "Down",
    "capslock": "Caps_Lock", "numlock": "Num_Lock", "scrolllock": "Scroll_Lock",
    "printscreen": "Print", "pause": "Pause", "menu": "Menu", "apps": "Menu",
    "shift": "Shift_L", "control": "Control_L", "ctrl": "Control_L",
    "option": "Alt_L", "alt": "Alt_L",
    "command": "Super_L", "cmd": "Super_L", "meta": "Super_L", "win": "Super_L",
    "multiply": "KP_Multiply", "add": "KP_Add", "subtract": "KP_Subtract",
    "decimal": "KP_Decimal", "divide": "KP_Divide", "numpadenter": "KP_Enter",
    "volumeup": "XF86AudioRaiseVolume", "volumedown": "XF86AudioLowerVolume",
    "volumemute": "XF86AudioMute", "medianext": "XF86AudioNext", "mediaprev": "XF86AudioPrev",
    "mediaplay": "XF86AudioPlay", "mediastop": "XF86AudioStop",
}
KEY_NAMES.update({"f%d" % n: "F%d" % n for n in range(1, 21)})
KEY_NAMES.update({"numpad%d" % n: "KP_%d" % n for n in range(10)})

# Every key name the protocol lists (used by the developer check for coverage).
PROTOCOL_KEY_NAMES = (
    "enter return tab space backspace delete forwarddelete del escape esc insert home end "
    "pageup pagedown left arrowleft right arrowright up arrowup down arrowdown capslock numlock "
    "scrolllock printscreen pause menu apps shift control ctrl option alt command cmd meta win "
    "multiply add subtract decimal divide numpadenter volumeup volumedown volumemute medianext "
    "mediaprev mediaplay mediastop").split() + ["f%d" % n for n in range(1, 21)] + [
    "numpad%d" % n for n in range(10)]

MODIFIER_KEYSYMS = {
    "shift": "Shift_L", "control": "Control_L", "ctrl": "Control_L",
    "option": "Alt_L", "alt": "Alt_L",
    "command": "Super_L", "cmd": "Super_L", "meta": "Super_L", "win": "Super_L",
}
FN_MODIFIERS = ("fn", "function")

BUTTONS = {"left": 1, "middle": 2, "right": 3, "back": 8, "forward": 9}

KEYSYM_RETURN = 0xff0d
KEYSYM_TAB = 0xff09


def normalize_key_name(key):
    """Named key → lower case without spaces/'_'/'-'; a single character stays as is."""
    if not isinstance(key, str) or key == "":
        raise bad_request("Поле key должно быть непустой строкой (имя клавиши или один символ)")
    if len(key) == 1:
        return key
    return key.strip().lower().replace(" ", "").replace("_", "").replace("-", "")


def keysym_for_char(char):
    """Unicode character → X keysym (Latin-1 as is, otherwise 0x01000000 | codepoint)."""
    if char in ("\n", "\r"):
        return KEYSYM_RETURN
    if char == "\t":
        return KEYSYM_TAB
    code = ord(char)
    if 0x20 <= code <= 0x7e or 0xa0 <= code <= 0xff:
        return code
    if code < 0x20 or 0x7f <= code < 0xa0:
        return None  # control characters have no printable keysym
    return 0x01000000 | code


def normalize_modifiers(values):
    """Protocol modifier names → list of unique keysym names (in the given order)."""
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise bad_request("Поле modifiers должно быть списком строк, например [\"control\", \"shift\"]")
    result = []
    for value in values:
        name = value.strip().lower()
        if name in FN_MODIFIERS:
            raise DriverError("unsupported", "Модификатор fn существует только на macOS; в Linux его "
                              "нельзя нажать программно — уберите его из modifiers")
        keysym = MODIFIER_KEYSYMS.get(name)
        if keysym is None:
            raise bad_request("Неизвестный модификатор «%s»; допустимы shift, control|ctrl, option|alt, "
                              "command|cmd|meta|win" % value)
        if keysym not in result:
            result.append(keysym)
    return result


# --------------------------------------------------------------------------------------------
# Scroll
# --------------------------------------------------------------------------------------------



def scroll_notches(delta, unit):
    """Signed number of wheel notches: pixel → one per 120 (min 1), line → n."""
    if not delta:
        return 0
    if unit == "line":
        count = max(1, int(round(abs(delta))))
    else:
        count = max(1, int(math.ceil(abs(delta) / 120.0)))
    return count if delta > 0 else -count


def scroll_plan(delta_x, delta_y, unit):
    """[(button, clicks)]: 4 up, 5 down, 6 left, 7 right; positive deltaY scrolls down."""
    plan = []
    vertical = scroll_notches(delta_y, unit)
    if vertical:
        plan.append((5 if vertical > 0 else 4, abs(vertical)))
    horizontal = scroll_notches(delta_x, unit)
    if horizontal:
        plan.append((7 if horizontal > 0 else 6, abs(horizontal)))
    return plan


# --------------------------------------------------------------------------------------------
# Parameter validation
# --------------------------------------------------------------------------------------------

INPUT_ACTIONS = ("move", "click", "mouse_down", "mouse_up", "drag", "scroll", "type",
                 "key", "key_down", "key_up")


def _number(params, name, required=True, default=None, lo=None, hi=None, integer=True):
    value = params.get(name, None)
    if value is None:
        if required:
            raise bad_request("Не хватает числового поля %s" % name)
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise bad_request("Поле %s должно быть конечным числом, получено %r" % (name, value))
    if integer:
        value = int(round(value))
    if lo is not None and value < lo or hi is not None and value > hi:
        raise bad_request("Поле %s вне допустимого диапазона %s..%s: %r" % (name, lo, hi, value))
    return value


def _button(params):
    name = params.get("button", "left")
    if not isinstance(name, str) or name.lower() not in BUTTONS:
        raise bad_request("Поле button должно быть одним из: left, right, middle, back, forward")
    return BUTTONS[name.lower()]


def parse_input(params):
    """Validate an `input` action; returns a normalized dict (no OS access)."""
    if not isinstance(params, dict):
        raise bad_request("params должен быть объектом")
    action = params.get("action")
    if action not in INPUT_ACTIONS:
        raise bad_request("Неизвестное действие ввода «%s»; допустимы: %s" % (action, ", ".join(INPUT_ACTIONS)))
    out = {"action": action}
    if action != "type":
        out["modifiers"] = normalize_modifiers(params.get("modifiers"))
    if action in ("move", "click", "mouse_down", "mouse_up", "drag", "scroll"):
        out["x"] = _number(params, "x")
        out["y"] = _number(params, "y")
    if action in ("click", "mouse_down", "mouse_up", "drag"):
        out["button"] = _button(params)
    if action == "click":
        out["clicks"] = _number(params, "clicks", required=False, default=1, lo=1)
        out["hold_ms"] = _number(params, "hold_ms", required=False, default=0, lo=0)
    elif action == "drag":
        out["toX"] = _number(params, "toX")
        out["toY"] = _number(params, "toY")
        out["steps"] = _number(params, "steps", required=False, default=24, lo=2)
        out["duration_ms"] = _number(params, "duration_ms", required=False, default=400, lo=0)
    elif action == "scroll":
        out["deltaX"] = _number(params, "deltaX", required=False, default=0, integer=False)
        out["deltaY"] = _number(params, "deltaY", required=False, default=0, integer=False)
        unit = params.get("unit", "pixel")
        if unit not in ("pixel", "line"):
            raise bad_request("Поле unit должно быть \"pixel\" или \"line\"")
        out["unit"] = unit
        if not out["deltaX"] and not out["deltaY"]:
            raise bad_request("Для прокрутки нужен ненулевой deltaX или deltaY")
    elif action == "type":
        text = params.get("text")
        if not isinstance(text, str):
            raise bad_request("Для type нужно строковое поле text")
        for char in text:
            if keysym_for_char(char) is None:
                raise bad_request("Текст содержит управляющий символ U+%04X, который нельзя ввести с "
                                  "клавиатуры" % ord(char))
        out["text"] = text
    elif action in ("key", "key_down", "key_up"):
        key = normalize_key_name(params.get("key"))
        if key in FN_MODIFIERS:
            raise DriverError("unsupported", "Клавиша fn существует только на macOS; в Linux её нельзя нажать")
        if len(key) > 1 and key not in KEY_NAMES:
            raise bad_request("Неизвестная клавиша «%s». Допустимы имена из протокола (enter, tab, f1..f20, "
                              "pageup, numpad0…) или один печатный символ" % params.get("key"))
        if len(key) == 1 and keysym_for_char(key) is None:
            raise bad_request("Управляющий символ U+%04X нельзя нажать как клавишу" % ord(key))
        out["key"] = key
        out["repeat"] = _number(params, "repeat", required=False, default=1, lo=1) if action == "key" else 1
    return out


def parse_window_id(value):
    """Window id string ("0x3a0000b" or decimal) → int."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().lower()
        try:
            number = int(text, 16) if text.startswith("0x") else int(text)
        except ValueError:
            number = 0
        if number > 0:
            return number
    raise bad_request("Поле id окна должно быть строкой из ответа windows: \"0x3a0000b\" (X11) или "
                      "\"atspi:<pid>:<n>\" (Wayland)")


def parse_bounds(value):
    if not isinstance(value, dict):
        raise bad_request("Для set_bounds нужен объект bounds {x, y, width, height}")
    return {"x": _number(value, "x"), "y": _number(value, "y"),
            "width": _number(value, "width", lo=1, hi=100000),
            "height": _number(value, "height", lo=1, hi=100000)}


ELEMENT_ID = re.compile(r"^e(\d+)\.(\d+)$")


def parse_element_id(value):
    match = ELEMENT_ID.match(value) if isinstance(value, str) else None
    if not match:
        raise bad_request("Поле id должно быть идентификатором элемента вида \"e7.12\" из ответа elements")
    return int(match.group(1)), int(match.group(2))


ATSPI_WINDOW_ID = re.compile(r"^atspi:(\d+):(\d+)$")


def parse_any_window_id(value):
    """X11 id ("0x3a0000b") → int; AT-SPI frame id ("atspi:<pid>:<n>", Wayland) → (pid, n)."""
    if isinstance(value, str):
        match = ATSPI_WINDOW_ID.match(value.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return parse_window_id(value)


# --------------------------------------------------------------------------------------------
# Protocol 1.1 helpers: OCR (tesseract TSV), clipboard file lists, background input
# --------------------------------------------------------------------------------------------

TESSERACT_LANGS = {"ru": "rus", "en": "eng", "uk": "ukr", "be": "bel", "kk": "kaz", "de": "deu", "fr": "fra",
                   "es": "spa", "it": "ita", "pt": "por", "pl": "pol", "tr": "tur", "nl": "nld", "cs": "ces",
                   "zh": "chi_sim", "ja": "jpn", "ko": "kor", "ar": "ara", "he": "heb"}
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MODIFIER_MASKS = {"Shift_L": 1, "Control_L": 4, "Alt_L": 8, "Super_L": 64}
BACKGROUND_ACTIONS = ("type", "key", "click")


def tesseract_languages(requested):
    """Protocol language codes (["ru", "en"]) or tesseract codes → tesseract codes; default rus+eng."""
    if requested is None:
        return ["rus", "eng"]
    if not isinstance(requested, list) or not requested or \
            not all(isinstance(code, str) and code.strip() for code in requested):
        raise bad_request("Поле languages должно быть непустым списком кодов языков, например [\"ru\", \"en\"]")
    out = []
    for code in requested:
        key = code.strip().lower()
        base = re.split(r"[-_]", key)[0]
        if len(base) == 2:
            name = TESSERACT_LANGS.get(base)
            if name is None:
                raise bad_request("Неизвестный язык «%s»; укажите код tesseract (например rus, eng, chi_sim)" % code)
        elif re.match(r"^[a-z]{3}(_[a-z]+)?$", key):
            name = key
        else:
            raise bad_request("Неверный код языка «%s»" % code)
        if name not in out:
            out.append(name)
    return out


def parse_list_langs(text):
    """`tesseract --list-langs` output (old versions print it to stderr) → language codes."""
    langs, started = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("list of available languages"):
            started = True
        elif started and re.match(r"^[A-Za-z0-9_]+$", line) and line not in langs:
            langs.append(line)
    return langs


def decode_image(value):
    """image_b64 (PNG/JPEG, optional data: prefix) → (bytes, file suffix)."""
    if not isinstance(value, str) or not value.strip():
        raise bad_request("Для ocr нужно поле image_b64 — PNG или JPEG в base64")
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    value = "".join(value.split())
    if len(value) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise bad_request("Картинка для ocr больше %d МБ — передайте область поменьше" % (MAX_IMAGE_BYTES >> 20))
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        raise bad_request("image_b64 не является корректным base64")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, ".png"
    if data[:3] == b"\xff\xd8\xff":
        return data, ".jpg"
    raise bad_request("image_b64 должен содержать PNG или JPEG")


def order_lines(lines):
    """Top to bottom; lines sharing a row (vertical centre inside the row's first line) left to right."""
    rows = []
    for line in sorted(lines, key=lambda item: (item["bounds"]["y"], item["bounds"]["x"])):
        b = line["bounds"]
        centre = b["y"] + b["height"] / 2.0
        if rows and rows[-1][0] <= centre <= rows[-1][1]:
            rows[-1][2].append(line)
        else:
            rows.append((b["y"], b["y"] + b["height"], [line]))
    return [line for _top, _bottom, row in rows for line in sorted(row, key=lambda item: item["bounds"]["x"])]


def parse_tesseract_tsv(text):
    """tesseract TSV → protocol lines: words grouped by (page, block, par, line); bounds = union of
    word boxes; confidence = mean word conf / 100 (conf -1 skipped, None when no word has one)."""
    rows = text.splitlines()
    if not rows:
        return []
    header = rows[0].rstrip("\r").split("\t")
    column = {name: index for index, name in enumerate(header)}
    needed = ("level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height",
              "conf", "text")
    missing = [name for name in needed if name not in column]
    if missing:
        raise DriverError("failed", "tesseract вернул TSV без столбцов %s" % ", ".join(missing))
    groups, order = {}, []
    for row in rows[1:]:
        cells = row.rstrip("\r").split("\t")
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        try:
            if int(cells[column["level"]]) != 5:
                continue
            key = tuple(int(cells[column[name]]) for name in ("page_num", "block_num", "par_num", "line_num"))
            left, top, width, height = (int(cells[column[name]]) for name in ("left", "top", "width", "height"))
            conf = float(cells[column["conf"]])
        except ValueError:
            continue
        word = cells[column["text"]].strip()
        if not word:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((word, left, top, width, height, conf))
    lines = []
    for key in order:
        words = groups[key]
        x0, y0 = min(w[1] for w in words), min(w[2] for w in words)
        x1, y1 = max(w[1] + w[3] for w in words), max(w[2] + w[4] for w in words)
        confs = [w[5] for w in words if w[5] >= 0]
        confidence = round(min(max(sum(confs) / len(confs) / 100.0, 0.0), 1.0), 3) if confs else None
        lines.append({"text": " ".join(w[0] for w in words), "confidence": confidence,
                      "bounds": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}})
    return order_lines(lines)


def parse_uri_list(text):
    """text/uri-list (or x-special/gnome-copied-files) → absolute local paths, file:// only, decoded."""
    paths = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line in ("copy", "cut"):
            continue
        parts = urllib.parse.urlsplit(line)
        if parts.scheme != "file" or parts.netloc not in ("", "localhost"):
            continue
        path = urllib.parse.unquote(parts.path)
        if path.startswith("/") and path not in paths:
            paths.append(path)
    return paths


def build_uri_list(paths):
    """Absolute paths → text/uri-list (RFC 2483: CRLF line ends, percent-encoded UTF-8)."""
    return "".join("file://" + urllib.parse.quote(path) + "\r\n" for path in paths)


def clipboard_paths(value):
    if not isinstance(value, list) or not value or not all(isinstance(p, str) and p for p in value):
        raise bad_request("Для clipboard_files set нужен непустой список абсолютных путей paths")
    out = []
    for path in value:
        if not os.path.isabs(path):
            raise bad_request("Путь «%s» не абсолютный — укажите полный путь" % path)
        if not os.path.exists(path):
            raise DriverError("not_found", "Файл «%s» не найден" % path)
        path = os.path.normpath(path)
        if path not in out:
            out.append(path)
    return out


def parse_background(params):
    """background_input params → parsed input action + "window" (X11 id)."""
    if not isinstance(params, dict):
        raise bad_request("params должен быть объектом")
    window = parse_window_id(params.get("window_id"))
    action = params.get("action")
    if action not in BACKGROUND_ACTIONS:
        raise bad_request("Неизвестное действие фонового ввода «%s»; допустимы: %s"
                          % (action, ", ".join(BACKGROUND_ACTIONS)))
    spec = dict(params)
    spec.pop("window_id", None)
    parsed = parse_input(spec)
    parsed["window"] = window
    return parsed


# --------------------------------------------------------------------------------------------
# Roles and actions (AT-SPI → protocol)
# --------------------------------------------------------------------------------------------

NORMALIZED_ROLES = ("button", "checkbox", "radio", "switch", "text_field", "text_area", "link", "menu",
                    "menu_item", "menu_bar", "tab", "tab_list", "list", "list_item", "tree", "tree_item",
                    "table", "row", "cell", "combo_box", "slider", "spin_button", "scroll_bar",
                    "scroll_area", "image", "text", "heading", "window", "dialog", "group", "toolbar",
                    "progress", "web_area", "other")

# Atspi role name (get_role_name(), spaces) → normalized role. "text"/"entry"/"paragraph" are
# resolved in normalize_role because they depend on the editable/multi-line states.
ROLE_MAP = {
    "push button": "button", "button": "button", "toggle button": "button",
    "push button menu": "button", "split button": "button",
    "check box": "checkbox", "radio button": "radio", "switch": "switch",
    "password text": "text_field", "editbar": "text_field",
    "link": "link",
    "menu": "menu", "popup menu": "menu",
    "menu item": "menu_item", "check menu item": "menu_item", "radio menu item": "menu_item",
    "tearoff menu item": "menu_item", "menu bar": "menu_bar",
    "page tab": "tab", "page tab list": "tab_list",
    "list": "list", "list box": "list", "description list": "list",
    "list item": "list_item", "description term": "list_item", "description value": "list_item",
    "tree": "tree", "tree table": "tree", "tree item": "tree_item",
    "table": "table", "table row": "row",
    "table cell": "cell", "column header": "cell", "row header": "cell",
    "table column header": "cell", "table row header": "cell",
    "combo box": "combo_box", "slider": "slider", "spin button": "spin_button",
    "scroll bar": "scroll_bar", "scroll pane": "scroll_area", "viewport": "scroll_area",
    "image": "image", "icon": "image", "animation": "image", "image map": "image",
    "label": "text", "static": "text", "caption": "text", "accelerator label": "text",
    "heading": "heading",
    "frame": "window", "window": "window",
    "dialog": "dialog", "alert": "dialog", "file chooser": "dialog", "color chooser": "dialog",
    "font chooser": "dialog",
    "panel": "group", "filler": "group", "section": "group", "grouping": "group", "form": "group",
    "layered pane": "group", "split pane": "group", "root pane": "group", "glass pane": "group",
    "option pane": "group", "internal frame": "group", "landmark": "group", "article": "group",
    "block quote": "group", "footer": "group", "header": "group", "info bar": "group",
    "status bar": "group", "html container": "group", "embedded": "group", "page": "group",
    "tool bar": "toolbar",
    "progress bar": "progress", "level bar": "progress",
    "document web": "web_area", "document frame": "web_area",
}

TEXT_VALUE_ROLES = ("text_field", "text_area", "combo_box")
NUMERIC_VALUE_ROLES = ("slider", "spin_button", "progress", "scroll_bar")


def role_key(raw):
    return re.sub(r"[\s_\-]+", " ", str(raw or "")).strip().lower()


def normalize_role(raw, editable=False, multi_line=False):
    """Atspi role name (e.g. "push button") + states → protocol role."""
    key = role_key(raw)
    if key in ("text", "entry", "terminal", "paragraph", "document text", "rich text"):
        if editable or key == "entry":
            return "text_area" if multi_line or key in ("document text", "rich text") else "text_field"
        return "text"
    if key == "password text":
        return "text_field"
    return ROLE_MAP.get(key, "other")


ACTION_ORDER = ("press", "focus", "set_value", "show_menu", "increment", "decrement", "select",
                "expand", "collapse", "scroll_into_view")

# AT-SPI action name (lower, without spaces/'_'/'-') → normalized actions.
ACTION_NAMES = {
    "click": ("press",), "press": ("press",), "activate": ("press",), "toggle": ("press",),
    "jump": ("press",), "open": ("press",), "invoke": ("press",), "default": ("press",),
    "clickancestor": ("press",),
    "showmenu": ("show_menu",), "menu": ("show_menu",), "popup": ("show_menu",),
    "contextmenu": ("show_menu",), "showcontextmenu": ("show_menu",),
    "expand": ("expand",), "collapse": ("collapse",), "contract": ("collapse",),
    "expandorcontract": ("expand", "collapse"), "expandorcollapse": ("expand", "collapse"),
    "select": ("select",),
}


def normalize_action_name(name):
    key = re.sub(r"[\s_\-]+", "", str(name or "")).lower()
    return ACTION_NAMES.get(key, ())


def compose_actions(action_names=(), focusable=False, editable_text=False, value_iface=False,
                    component=False, selectable_parent=False):
    """Normalized, de-duplicated action list in protocol order."""
    found = set()
    for name in action_names:
        found.update(normalize_action_name(name))
    if focusable:
        found.add("focus")
    if editable_text or value_iface:
        found.add("set_value")
    if value_iface:
        found.update(("increment", "decrement"))
    if selectable_parent:
        found.add("select")
    if component:
        found.add("scroll_into_view")
    return [action for action in ACTION_ORDER if action in found]


def element_matches(element, query=None, role=None):
    """Protocol filters: role on the normalized role, query on name/value/description."""
    if role and element.get("role") != role:
        return False
    if query:
        needle = query.casefold()
        return any(needle in str(element.get(field) or "").casefold()
                   for field in ("name", "value", "description"))
    return True


def select_text_range(full, params):
    """Range for select_text in characters of `full`: a substring (n-th match), start/end offsets, or everything."""
    if params.get("all") is True:
        return 0, len(full)
    needle = params.get("text")
    if needle is not None:
        if not isinstance(needle, str) or not needle:
            raise bad_request("Поле text должно быть непустой строкой")
        wanted = _number(params, "occurrence", required=False, default=1, lo=1)
        position = found = 0
        while True:
            index = full.find(needle, position)
            if index < 0:
                raise DriverError("not_found", "Фрагмент «%s» (вхождение %d) не найден в тексте элемента; вхождений: %d"
                                  % (needle, wanted, found))
            found += 1
            if found == wanted:
                return index, index + len(needle)
            position = index + len(needle)
    start = _number(params, "start", lo=0)
    end = _number(params, "end", required=False, default=start, lo=start)
    if end > len(full):
        raise bad_request("end %d за пределами текста (длина %d)" % (end, len(full)))
    return start, end


def parse_elements_params(params):
    out = {"window_id": None, "pid": None, "point": None, "query": None, "role": None,
           "max": _number(params, "max", required=False, default=None, lo=1),
           "depth": _number(params, "depth", required=False, default=None, lo=0)}
    if params.get("window_id") not in (None, ""):
        out["window_id"] = parse_any_window_id(params["window_id"])
    if params.get("pid") is not None:
        out["pid"] = _number(params, "pid", lo=1)
    point = params.get("point")
    if point is not None:
        if not isinstance(point, dict):
            raise bad_request("Поле point должно быть объектом {x, y}")
        out["point"] = {"x": _number(point, "x"), "y": _number(point, "y")}
    for name in ("query", "role"):
        value = params.get(name)
        if value not in (None, ""):
            if not isinstance(value, str):
                raise bad_request("Поле %s должно быть строкой" % name)
            out[name] = value
    if out["role"] and out["role"] not in NORMALIZED_ROLES:
        raise bad_request("Неизвестная роль «%s»; допустимы: %s" % (out["role"], ", ".join(NORMALIZED_ROLES)))
    return out


# --------------------------------------------------------------------------------------------
# .desktop Exec parsing
# --------------------------------------------------------------------------------------------

FIELD_CODES = ("%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m")


def exec_to_argv(command, args=()):
    """Desktop Entry Exec line → argv without field codes; extra args are appended."""
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        raise DriverError("failed", "Строка Exec в .desktop-файле повреждена: %s" % command)
    argv = []
    for token in tokens:
        if token in FIELD_CODES:
            continue
        token = re.sub(r"%[fFuUdDnNickvm]", "", token.replace("%%", "\x00")).replace("\x00", "%")
        if token:
            argv.append(token)
    if not argv:
        raise DriverError("failed", "В .desktop-файле пустая команда Exec")
    return argv + list(args)


def desktop_dirs(env=None):
    env = os.environ if env is None else env
    home = os.path.expanduser("~")
    data_home = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    data_dirs = (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    roots = [data_home] + data_dirs + [os.path.join(home, ".local/share/flatpak/exports/share"),
                                       "/var/lib/flatpak/exports/share", "/var/lib/snapd/desktop"]
    seen, out = set(), []
    for root in roots:
        path = os.path.join(root, "applications")
        if root and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def read_desktop_entry(path):
    """Minimal [Desktop Entry] reader → dict of unlocalized keys."""
    entry, inside = {}, False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("["):
                    inside = line == "[Desktop Entry]"
                elif inside and "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    entry.setdefault(key.strip(), value.strip())
    except OSError:
        return None
    return entry




def _png_chunk(kind, payload):
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)


def encode_png_rgba(width, height, rgba):
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise ValueError("invalid RGBA buffer")
    rows = []
    stride = width * 4
    for y in range(height):
        rows.append(b"\x00" + rgba[y * stride:(y + 1) * stride])
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + _png_chunk(b"IEND", b"")

# --------------------------------------------------------------------------------------------
# X11 via ctypes (loaded lazily)
# --------------------------------------------------------------------------------------------

X_BAD_WINDOW = 3
X_BAD_DRAWABLE = 9
X_BAD_MATCH = 8
X_BAD_ACCESS = 10
X_ERROR_NAMES = {1: "BadRequest", 2: "BadValue", 3: "BadWindow", 4: "BadPixmap", 5: "BadAtom",
                 8: "BadMatch", 9: "BadDrawable", 10: "BadAccess", 11: "BadAlloc"}
SUBSTRUCTURE_MASK = (1 << 19) | (1 << 20)  # SubstructureNotifyMask | SubstructureRedirectMask
CLIENT_MESSAGE = 33
XKB_USE_CORE_KBD = 0x0100
LOCK_MASK = 1 << 1

_X_ERRORS = []        # errors recorded by the handler (never exit the process)
_X_HANDLER = None     # keeps the CFUNCTYPE callback alive for the process lifetime


class X11Unavailable(Exception):
    pass


def _load_xlib():
    """Load libX11 (+ libXtst when present) and declare every signature explicitly."""
    import ctypes
    from ctypes import POINTER, c_char_p, c_int, c_long, c_ubyte, c_uint, c_ulong, c_void_p

    class XErrorEvent(ctypes.Structure):
        _fields_ = [("type", c_int), ("display", c_void_p), ("resourceid", c_ulong),
                    ("serial", c_ulong), ("error_code", c_ubyte), ("request_code", c_ubyte),
                    ("minor_code", c_ubyte)]

    class XWindowAttributes(ctypes.Structure):
        _fields_ = [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int),
                    ("border_width", c_int), ("depth", c_int), ("visual", c_void_p),
                    ("root", c_ulong), ("class_", c_int), ("bit_gravity", c_int),
                    ("win_gravity", c_int), ("backing_store", c_int), ("backing_planes", c_ulong),
                    ("backing_pixel", c_ulong), ("save_under", c_int), ("colormap", c_ulong),
                    ("map_installed", c_int), ("map_state", c_int), ("all_event_masks", c_long),
                    ("your_event_mask", c_long), ("do_not_propagate_mask", c_long),
                    ("override_redirect", c_int), ("screen", c_void_p)]

    class XImage(ctypes.Structure):
        _fields_ = [("width", c_int), ("height", c_int), ("xoffset", c_int), ("format", c_int),
                    ("data", c_void_p), ("byte_order", c_int), ("bitmap_unit", c_int),
                    ("bitmap_bit_order", c_int), ("bitmap_pad", c_int), ("depth", c_int),
                    ("bytes_per_line", c_int), ("bits_per_pixel", c_int),
                    ("red_mask", c_ulong), ("green_mask", c_ulong), ("blue_mask", c_ulong),
                    ("obdata", c_void_p), ("funcs", c_void_p * 6)]

    class XClientMessageEvent(ctypes.Structure):
        _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int),
                    ("display", c_void_p), ("window", c_ulong), ("message_type", c_ulong),
                    ("format", c_int), ("data", c_long * 5)]

    # XKeyEvent / XButtonEvent on LP64: Window/Time are unsigned long, Display* a pointer.
    class XKeyEvent(ctypes.Structure):
        _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                    ("window", c_ulong), ("root", c_ulong), ("subwindow", c_ulong), ("time", c_ulong),
                    ("x", c_int), ("y", c_int), ("x_root", c_int), ("y_root", c_int),
                    ("state", c_uint), ("keycode", c_uint), ("same_screen", c_int)]

    class XButtonEvent(ctypes.Structure):
        _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
                    ("window", c_ulong), ("root", c_ulong), ("subwindow", c_ulong), ("time", c_ulong),
                    ("x", c_int), ("y", c_int), ("x_root", c_int), ("y_root", c_int),
                    ("state", c_uint), ("button", c_uint), ("same_screen", c_int)]

    class XEvent(ctypes.Union):
        _fields_ = [("type", c_int), ("xclient", XClientMessageEvent), ("xkey", XKeyEvent),
                    ("xbutton", XButtonEvent), ("pad", c_long * 24)]

    class XkbStateRec(ctypes.Structure):
        _fields_ = [("group", c_ubyte), ("locked_group", c_ubyte), ("base_group", ctypes.c_ushort),
                    ("latched_group", ctypes.c_ushort), ("mods", c_ubyte), ("base_mods", c_ubyte),
                    ("latched_mods", c_ubyte), ("locked_mods", c_ubyte), ("compat_state", c_ubyte),
                    ("grab_mods", c_ubyte), ("compat_grab_mods", c_ubyte), ("lookup_mods", c_ubyte),
                    ("compat_lookup_mods", c_ubyte), ("ptr_buttons", ctypes.c_ushort),
                    ("_reserve", c_ubyte * 16)]

    try:
        x11 = ctypes.CDLL("libX11.so.6")
    except OSError:
        raise X11Unavailable("нет libX11 — установите пакет libx11-6 (ввод и список окон недоступны)")
    try:
        xtst = ctypes.CDLL("libXtst.so.6")
    except OSError:
        xtst = None

    def declare(lib, name, restype, *argtypes):
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = list(argtypes)
        return function

    handler_type = ctypes.CFUNCTYPE(c_int, c_void_p, POINTER(XErrorEvent))
    d, w, atom = c_void_p, c_ulong, c_ulong
    ns = {
        "ctypes": ctypes, "XErrorEvent": XErrorEvent, "XWindowAttributes": XWindowAttributes,
        "XImage": XImage, "XEvent": XEvent, "XkbStateRec": XkbStateRec, "handler_type": handler_type, "xtst": xtst,
        "XOpenDisplay": declare(x11, "XOpenDisplay", c_void_p, c_char_p),
        "XCloseDisplay": declare(x11, "XCloseDisplay", c_int, d),
        "XDefaultRootWindow": declare(x11, "XDefaultRootWindow", w, d),
        "XDefaultScreen": declare(x11, "XDefaultScreen", c_int, d),
        "XFlush": declare(x11, "XFlush", c_int, d),
        "XSync": declare(x11, "XSync", c_int, d, c_int),
        "XSetErrorHandler": declare(x11, "XSetErrorHandler", c_void_p, handler_type),
        "XInternAtom": declare(x11, "XInternAtom", atom, d, c_char_p, c_int),
        "XGetWindowProperty": declare(x11, "XGetWindowProperty", c_int, d, w, atom, c_long, c_long,
                                      c_int, atom, POINTER(atom), POINTER(c_int), POINTER(c_ulong),
                                      POINTER(c_ulong), POINTER(c_void_p)),
        "XFree": declare(x11, "XFree", c_int, c_void_p),
        "XQueryPointer": declare(x11, "XQueryPointer", c_int, d, w, POINTER(w), POINTER(w),
                                 POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int),
                                 POINTER(c_uint)),
        "XGetWindowAttributes": declare(x11, "XGetWindowAttributes", c_int, d, w,
                                        POINTER(XWindowAttributes)),
        "XGetImage": declare(x11, "XGetImage", POINTER(XImage), d, w, c_int, c_int, c_uint, c_uint,
                             c_ulong, c_int),
        "XDestroyImage": declare(x11, "XDestroyImage", c_int, POINTER(XImage)),
        "XTranslateCoordinates": declare(x11, "XTranslateCoordinates", c_int, d, w, w, c_int, c_int,
                                         POINTER(c_int), POINTER(c_int), POINTER(w)),
        "XSendEvent": declare(x11, "XSendEvent", c_int, d, w, c_int, c_long, POINTER(XEvent)),
        "XIconifyWindow": declare(x11, "XIconifyWindow", c_int, d, w, c_int),
        "XMoveResizeWindow": declare(x11, "XMoveResizeWindow", c_int, d, w, c_int, c_int, c_uint, c_uint),
        "XStringToKeysym": declare(x11, "XStringToKeysym", c_ulong, c_char_p),
        # KeyCode arguments are declared as unsigned int (NeedWidePrototypes-safe; values < 256).
        "XKeysymToKeycode": declare(x11, "XKeysymToKeycode", c_ubyte, d, c_ulong),
        "XkbKeycodeToKeysym": declare(x11, "XkbKeycodeToKeysym", c_ulong, d, c_uint, c_int, c_int),
        "XkbGetState": declare(x11, "XkbGetState", c_int, d, c_uint, POINTER(XkbStateRec)),
        "XDisplayKeycodes": declare(x11, "XDisplayKeycodes", c_int, d, POINTER(c_int), POINTER(c_int)),
        "XGetKeyboardMapping": declare(x11, "XGetKeyboardMapping", POINTER(c_ulong), d, c_uint, c_int,
                                       POINTER(c_int)),
        "XChangeKeyboardMapping": declare(x11, "XChangeKeyboardMapping", c_int, d, c_int, c_int,
                                          POINTER(c_ulong), c_int),
    }
    if xtst is not None:
        ns["XTestQueryExtension"] = declare(xtst, "XTestQueryExtension", c_int, d, POINTER(c_int),
                                            POINTER(c_int), POINTER(c_int), POINTER(c_int))
        ns["XTestFakeMotionEvent"] = declare(xtst, "XTestFakeMotionEvent", c_int, d, c_int, c_int, c_int,
                                             c_ulong)
        ns["XTestFakeButtonEvent"] = declare(xtst, "XTestFakeButtonEvent", c_int, d, c_uint, c_int, c_ulong)
        ns["XTestFakeKeyEvent"] = declare(xtst, "XTestFakeKeyEvent", c_int, d, c_uint, c_int, c_ulong)
    return ns


class XlibNS(object):
    """Attribute access to the declared libX11/libXtst functions."""

    def __init__(self):
        self.__dict__.update(_load_xlib())


def _install_error_handler(xlib):
    global _X_HANDLER
    if _X_HANDLER is not None:
        return

    def handler(_display, event):
        try:
            e = event.contents
            _X_ERRORS.append((int(e.error_code), int(e.request_code), int(e.resourceid)))
            del _X_ERRORS[:-32]
        except Exception:
            pass
        return 0

    _X_HANDLER = xlib.handler_type(handler)
    xlib.XSetErrorHandler(_X_HANDLER)


class X11Backend(object):
    """XTEST input, pointer and EWMH windows on one Display connection."""

    def __init__(self, display_name):
        self.display_name = display_name or ""
        self.x = None
        self.dpy = None
        self.root = 0
        self.screen = 0
        self.xtest = False
        self.error = None
        self.notes = []
        self.held_keys = []       # keycodes pressed by us and not yet released (press order)
        self.held_buttons = []
        self.scratch_code = None  # keycode temporarily remapped for `type`
        self.scratch_keysym = None
        self.key_mods = {}        # keycode held by key_down → modifier keycodes that key_down pressed
        self.button_mods = {}     # button held by mouse_down → modifier keycodes that mouse_down pressed
        self._atoms = {}
        self._keycodes = None
        self._supported = None

    # ---- connection -------------------------------------------------------------------------

    def open(self):
        if self.dpy:
            return True
        if self.error:
            return False
        try:
            self.x = XlibNS()
        except X11Unavailable as error:
            self.error = str(error)
            self.notes.append(self.error)
            return False
        except Exception as error:  # broken libX11 build, missing symbol …
            self.error = "не удалось загрузить libX11: %s" % error
            self.notes.append(self.error)
            return False
        _install_error_handler(self.x)
        dpy = self.x.XOpenDisplay(self.display_name.encode("utf-8", "replace"))
        if not dpy:
            self.error = ("не удалось подключиться к дисплею X11 «%s» — проверьте переменные DISPLAY и "
                          "XAUTHORITY (ввод и список окон недоступны)" % self.display_name)
            self.notes.append(self.error)
            return False
        self.dpy = dpy
        self.root = self.x.XDefaultRootWindow(dpy)
        self.screen = self.x.XDefaultScreen(dpy)
        if self.x.xtst is None:
            self.notes.append("нет libXtst — установите пакет libxtst6 (ввод мышью и клавиатурой недоступен)")
        else:
            ct = self.x.ctypes
            a, b, c, e = ct.c_int(), ct.c_int(), ct.c_int(), ct.c_int()
            if self.x.XTestQueryExtension(dpy, ct.byref(a), ct.byref(b), ct.byref(c), ct.byref(e)):
                self.xtest = True
            else:
                self.notes.append("X-сервер без расширения XTEST — программный ввод недоступен; включите "
                                  "расширение XTEST в конфигурации X-сервера")
        return True

    def capture(self, region=None):
        """Capture the X11 virtual desktop or a rectangular root-window region as PNG."""
        self.require_display()
        ct = self.x.ctypes
        attrs = self.x.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, self.root, ct.byref(attrs)):
            raise DriverError("failed", "Не удалось получить геометрию корневого окна X11")
        root_width, root_height = int(attrs.width), int(attrs.height)
        x = y = 0
        width, height = root_width, root_height
        if region:
            try:
                x = int(round(float(region.get("x", 0))))
                y = int(round(float(region.get("y", 0))))
                width = int(round(float(region.get("width", root_width))))
                height = int(round(float(region.get("height", root_height))))
            except Exception:
                raise bad_request("region должен содержать числовые x, y, width, height")
            if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > root_width or y + height > root_height:
                raise bad_request("region выходит за границы виртуального рабочего стола X11")
        image = self.x.XGetImage(self.dpy, self.root, x, y, width, height, 0xffffffffffffffff, 2)
        if not image:
            raise DriverError("failed", "XGetImage не смог снять экран")
        try:
            meta = image.contents
            if meta.bits_per_pixel not in (24, 32) or meta.bytes_per_line <= 0 or not meta.data:
                raise DriverError("unsupported", "Неподдерживаемый формат XImage: %d bpp" % meta.bits_per_pixel)
            total = meta.bytes_per_line * height
            raw = ct.string_at(meta.data, total)
            byteorder = "little" if meta.byte_order == 0 else "big"

            def channel(pixel, mask):
                mask = int(mask)
                if mask == 0:
                    return 0
                shift = (mask & -mask).bit_length() - 1
                maximum = mask >> shift
                value = (pixel & mask) >> shift
                return int(round(value * 255.0 / maximum)) if maximum else 0

            max_edge = CAPTURE_MAX_EDGE
            scale = min(1.0, float(max_edge) / max(width, height))
            out_width = max(1, int(round(width * scale)))
            out_height = max(1, int(round(height * scale)))
            rgba = bytearray(out_width * out_height * 4)
            pixel_bytes = meta.bits_per_pixel // 8

            # Most Xorg/Xwayland XImages are little-endian BGRX with 8-bit RGB masks.
            # For an unscaled capture, use C-backed slice assignments instead of a
            # Python loop per pixel; this cuts full-screen capture CPU dramatically.
            standard_masks = (int(meta.red_mask), int(meta.green_mask), int(meta.blue_mask)) == (
                0x00ff0000, 0x0000ff00, 0x000000ff)
            if (out_width, out_height) == (width, height) and meta.byte_order == 0 and standard_masks:
                row_bytes = width * pixel_bytes
                if meta.bytes_per_line == row_bytes:
                    packed = raw[:row_bytes * height]
                else:
                    packed = b"".join(
                        raw[row * meta.bytes_per_line:row * meta.bytes_per_line + row_bytes]
                        for row in range(height)
                    )
                rgba[0::4] = packed[2::pixel_bytes]
                rgba[1::4] = packed[1::pixel_bytes]
                rgba[2::4] = packed[0::pixel_bytes]
                rgba[3::4] = b"\xff" * (width * height)
            else:
                out = 0
                for out_row in range(out_height):
                    row = min(height - 1, int(out_row * height / out_height))
                    base = row * meta.bytes_per_line
                    for out_col in range(out_width):
                        col = min(width - 1, int(out_col * width / out_width))
                        start = base + col * pixel_bytes
                        pixel = int.from_bytes(raw[start:start + pixel_bytes], byteorder=byteorder, signed=False)
                        rgba[out] = channel(pixel, meta.red_mask)
                        rgba[out + 1] = channel(pixel, meta.green_mask)
                        rgba[out + 2] = channel(pixel, meta.blue_mask)
                        rgba[out + 3] = 255
                        out += 4
            png = encode_png_rgba(out_width, out_height, bytes(rgba))
            return {"image_b64": base64.b64encode(png).decode("ascii"), "mime_type": "image/png",
                    "image_width": out_width, "image_height": out_height,
                    "bounds": {"x": x, "y": y, "width": width, "height": height},
                    "backend": "linux-x11"}
        finally:
            self.x.XDestroyImage(image)

    def close(self):
        if not self.dpy:
            return
        self.release_all()
        try:
            self.x.XCloseDisplay(self.dpy)
        except Exception:
            pass
        self.dpy = None

    def require_display(self):
        if not self.open():
            raise DriverError("unsupported", "X11 недоступен: %s" % self.error)

    def require_input(self):
        self.require_display()
        if not self.xtest:
            raise DriverError("unsupported", "Ввод недоступен: " + "; ".join(self.notes or ["нет XTEST"]))

    @contextlib.contextmanager
    def trap(self, what):
        """Run X calls; X errors reported asynchronously become protocol errors."""
        del _X_ERRORS[:]
        yield
        self.x.XSync(self.dpy, 0)
        if _X_ERRORS:
            code, request, _resource = _X_ERRORS[0]
            del _X_ERRORS[:]
            raise self.x_error(code, request, what)

    @staticmethod
    def x_error(code, request, what):
        if code in (X_BAD_WINDOW, X_BAD_DRAWABLE):
            return DriverError("not_found", "%s больше не существует (закрыто?) — запросите windows заново" % what)
        if code == X_BAD_ACCESS:
            return DriverError("blocked", "X-сервер отказал в доступе (%s): %s" % (X_ERROR_NAMES[code], what))
        return DriverError("failed", "Ошибка X11 %s (запрос %d): %s"
                           % (X_ERROR_NAMES.get(code, "код %d" % code), request, what))

    def flush(self):
        self.x.XFlush(self.dpy)

    def sync(self):
        self.x.XSync(self.dpy, 0)

    def atom(self, name):
        value = self._atoms.get(name)
        if value is None:
            value = self.x.XInternAtom(self.dpy, name.encode("ascii"), 0)
            self._atoms[name] = value
        return value

    # ---- properties -------------------------------------------------------------------------

    def get_property(self, window, name, req_type=0, max_longs=1 << 16):
        """(actual_type, format, items|bytes) or None. Format 32 items are C longs (c_ulong)."""
        ct = self.x.ctypes
        actual_type, fmt = ct.c_ulong(0), ct.c_int(0)
        count, after, data = ct.c_ulong(0), ct.c_ulong(0), ct.c_void_p(None)
        status = self.x.XGetWindowProperty(self.dpy, window, self.atom(name), 0, max_longs, 0, req_type,
                                           ct.byref(actual_type), ct.byref(fmt), ct.byref(count),
                                           ct.byref(after), ct.byref(data))
        if status != 0 or not data.value:
            if data.value:
                self.x.XFree(data)
            return None
        try:
            n = count.value
            if fmt.value == 32:
                array = ct.cast(data, ct.POINTER(ct.c_ulong))
                items = [array[i] for i in range(n)]
            elif fmt.value == 16:
                array = ct.cast(data, ct.POINTER(ct.c_ushort))
                items = [array[i] for i in range(n)]
            elif fmt.value == 8:
                items = ct.string_at(data, n)
            else:
                return None
        finally:
            self.x.XFree(data)
        return actual_type.value, fmt.value, items

    def prop_ints(self, window, name):
        result = self.get_property(window, name)
        return list(result[2]) if result and result[1] == 32 else []

    def prop_text(self, window, name):
        result = self.get_property(window, name)
        if not result or result[1] != 8:
            return None
        raw = bytes(result[2]).split(b"\x00")[0] if name != "WM_CLASS" else bytes(result[2])
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")

    # ---- pointer ----------------------------------------------------------------------------

    def cursor(self):
        self.require_display()
        ct = self.x.ctypes
        root, child = ct.c_ulong(0), ct.c_ulong(0)
        rx, ry, wx, wy, mask = ct.c_int(0), ct.c_int(0), ct.c_int(0), ct.c_int(0), ct.c_uint(0)
        self.x.XQueryPointer(self.dpy, self.root, ct.byref(root), ct.byref(child), ct.byref(rx),
                             ct.byref(ry), ct.byref(wx), ct.byref(wy), ct.byref(mask))
        return {"x": rx.value, "y": ry.value}

    def root_size(self):
        attrs = self.x.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, self.root, self.x.ctypes.byref(attrs)):
            return None
        return attrs.width, attrs.height

    def check_point(self, x, y):
        size = self.root_size()
        if size and not (0 <= x < size[0] and 0 <= y < size[1]):
            raise bad_request("Точка (%d, %d) вне экрана X11 %d×%d — координаты задаются в пикселях корневого "
                              "окна" % (x, y, size[0], size[1]))

    def move(self, x, y):
        if not self.x.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0):
            raise DriverError("blocked", "X-сервер не принял перемещение указателя")

    def button(self, number, press):
        if not self.x.XTestFakeButtonEvent(self.dpy, number, 1 if press else 0, 0):
            raise DriverError("blocked", "X-сервер не принял событие кнопки мыши %d" % number)
        if press:
            if number not in self.held_buttons:
                self.held_buttons.append(number)
        elif number in self.held_buttons:
            self.held_buttons.remove(number)

    def key(self, code, press):
        if not self.x.XTestFakeKeyEvent(self.dpy, code, 1 if press else 0, 0):
            raise DriverError("blocked", "X-сервер не принял событие клавиши")
        if press:
            if code not in self.held_keys:
                self.held_keys.append(code)
        elif code in self.held_keys:
            self.held_keys.remove(code)

    def release_all(self):
        """Release everything we hold (EOF, SIGTERM, fatal errors) and undo the scratch mapping."""
        if not self.dpy or not self.xtest:
            return
        for number in list(reversed(self.held_buttons)):
            try:
                self.button(number, False)
            except Exception:
                self.held_buttons = [b for b in self.held_buttons if b != number]
        for code in list(reversed(self.held_keys)):
            try:
                self.key(code, False)
            except Exception:
                self.held_keys = [k for k in self.held_keys if k != code]
        self.key_mods.clear()
        self.button_mods.clear()
        try:
            self.restore_scratch()
            self.sync()
        except Exception:
            pass

    # ---- keyboard ---------------------------------------------------------------------------

    def keycode_range(self):
        if self._keycodes is None:
            ct = self.x.ctypes
            lo, hi = ct.c_int(8), ct.c_int(255)
            self.x.XDisplayKeycodes(self.dpy, ct.byref(lo), ct.byref(hi))
            self._keycodes = (max(8, lo.value), min(255, hi.value))
        return self._keycodes

    def xkb_state(self):
        """(effective group, locked modifiers) of the core keyboard."""
        state = self.x.XkbStateRec()
        if self.x.XkbGetState(self.dpy, XKB_USE_CORE_KBD, self.x.ctypes.byref(state)) != 0:
            return 0, 0
        return int(state.group), int(state.locked_mods)

    def find_in_group(self, keysym, group, hint=0):
        """Keycode producing keysym in the given group at level 1 or 2 → (code, needs_shift)."""
        lo, hi = self.keycode_range()
        for code in ([hint] if hint else []) + list(range(lo, hi + 1)):
            for level in (0, 1):
                if self.x.XkbKeycodeToKeysym(self.dpy, code, group, level) == keysym:
                    return code, level == 1
        return None, False

    def modifier_code(self, keysym_name):
        keysym = self.x.XStringToKeysym(keysym_name.encode("ascii"))
        code = self.x.XKeysymToKeycode(self.dpy, keysym) if keysym else 0
        if not code:
            raise DriverError("unsupported", "В раскладке X11 нет клавиши %s — её нельзя нажать" % keysym_name)
        return code

    def resolve_key(self, key):
        """Protocol key (named or one char) → (keycode | None when unmapped, needs_shift, keysym)."""
        if len(key) > 1:
            name = KEY_NAMES[key]
            keysym = self.x.XStringToKeysym(name.encode("ascii"))
            if not keysym:
                raise DriverError("unsupported", "Библиотека X11 не знает клавишу %s (keysym %s)" % (key, name))
            return (self.x.XKeysymToKeycode(self.dpy, keysym) or None), False, keysym
        # Shortcut semantics: a letter means its key ("A" with control is Ctrl+A, not Ctrl+Shift+A);
        # characters that themselves need Shift ("!", "?") get it automatically.
        char = key.lower() if "A" <= key <= "Z" else key
        keysym = keysym_for_char(char)
        group, _locked = self.xkb_state()
        hint = self.x.XKeysymToKeycode(self.dpy, keysym)
        code, shift = self.find_in_group(keysym, group, hint)
        if code is None and hint:
            # The key exists only in another layout group: press the physical key — toolkits match
            # shortcuts by keycode across layouts, so Ctrl+C works on a Russian layout.
            shift = any(self.x.XkbKeycodeToKeysym(self.dpy, hint, g, 1) == keysym and
                        self.x.XkbKeycodeToKeysym(self.dpy, hint, g, 0) != keysym for g in range(4))
            return hint, shift, keysym
        return code, shift, keysym

    def find_scratch(self):
        """A keycode with no keysyms at all (highest first), for temporary remapping."""
        ct = self.x.ctypes
        lo, hi = self.keycode_range()
        count = hi - lo + 1
        per = ct.c_int(0)
        mapping = self.x.XGetKeyboardMapping(self.dpy, lo, count, ct.byref(per))
        if not mapping:
            raise DriverError("failed", "Не удалось прочитать раскладку клавиатуры X11")
        try:
            width = per.value
            for index in range(count - 1, -1, -1):
                if all(mapping[index * width + j] == 0 for j in range(width)):
                    return lo + index
        finally:
            self.x.XFree(ct.cast(mapping, ct.c_void_p))
        raise DriverError("blocked", "В раскладке X11 нет свободного keycode, чтобы ввести символ вне "
                                     "текущей раскладки — переключите раскладку или введите текст иначе")

    def map_scratch(self, keysym):
        if self.scratch_code is None:
            self.scratch_code = self.find_scratch()
        if self.scratch_keysym != keysym:
            # The previous release must reach clients before the key changes meaning.
            self.sync()
            time.sleep(0.005)
            syms = (self.x.ctypes.c_ulong * 2)(keysym, keysym)
            self.x.XChangeKeyboardMapping(self.dpy, self.scratch_code, 2, syms, 1)
            self.sync()
            time.sleep(0.01)  # clients refresh their keymap on MappingNotify
            self.scratch_keysym = keysym
        return self.scratch_code

    def restore_scratch(self):
        if self.scratch_code is None:
            return
        code, self.scratch_code, self.scratch_keysym = self.scratch_code, None, None
        self.sync()
        time.sleep(0.01)
        syms = (self.x.ctypes.c_ulong * 2)(0, 0)
        self.x.XChangeKeyboardMapping(self.dpy, code, 2, syms, 1)
        self.sync()

    def tap(self, code, shift_code=None):
        pressed = []
        try:
            if shift_code is not None:
                self.key(shift_code, True)
                pressed.append(shift_code)
            self.key(code, True)
            pressed.append(code)
        finally:
            for item in reversed(pressed):
                self.key(item, False)

    def type_text(self, text):
        group, locked = self.xkb_state()
        shift_key = self.modifier_code("Shift_L")
        cache = {}
        try:
            for index, char in enumerate(text):
                keysym = keysym_for_char(char)
                if keysym not in cache:
                    cache[keysym] = self.find_in_group(keysym, group, self.x.XKeysymToKeycode(self.dpy, keysym))
                code, shift = cache[keysym]
                if code is None or code == self.scratch_code:
                    code, shift = self.map_scratch(keysym), False
                elif locked & LOCK_MASK and char.lower() != char.upper():
                    shift = not shift  # Caps Lock inverts letters (alphabetic key types)
                self.tap(code, shift_key if shift and shift_key not in self.held_keys else None)
                self.flush()
                time.sleep(0.01 if index % 32 == 31 else 0.0015)
        finally:
            self.restore_scratch()

    # ---- input ------------------------------------------------------------------------------

    def input(self, a):
        self.require_input()
        kind = a["action"]
        with self.trap("ввод"):
            if "x" in a:
                self.check_point(a["x"], a["y"])
            if kind == "drag":
                self.check_point(a["toX"], a["toY"])
            mods = [self.modifier_code(name) for name in a.get("modifiers", [])]
            try:
                if kind == "key_down":
                    self.key_down(a["key"], mods)
                elif kind == "key_up":
                    self.key_up(a["key"], mods)
                elif kind == "mouse_down":
                    self.mouse_down(a, mods)
                elif kind == "mouse_up":
                    self.mouse_up(a, mods)
                else:
                    acquired = self.hold(mods)
                    try:
                        if acquired:
                            self.flush()
                        self.perform(a)
                    finally:
                        self.let_go(acquired)
            finally:
                self.flush()
        return {"cursor": self.cursor()}

    def perform(self, a):
        kind = a["action"]
        if kind in ("move", "click", "drag", "scroll"):
            self.move(a["x"], a["y"])
            self.flush()
        if kind == "click":
            time.sleep(0.01)
            for index in range(a["clicks"]):
                self.button(a["button"], True)
                try:
                    self.flush()
                    if a["hold_ms"]:
                        time.sleep(a["hold_ms"] / 1000.0)
                finally:
                    self.button(a["button"], False)
                self.flush()
                if index + 1 < a["clicks"]:
                    time.sleep(0.04)
        elif kind == "drag":
            # Intermediate moves with the button held — a jump from point to point reads as a click.
            time.sleep(0.02)
            self.button(a["button"], True)
            try:
                self.flush()
                steps = a["steps"]
                pause = a["duration_ms"] / 1000.0 / steps
                for step in range(1, steps + 1):
                    self.move(int(round(a["x"] + (a["toX"] - a["x"]) * step / float(steps))),
                              int(round(a["y"] + (a["toY"] - a["y"]) * step / float(steps))))
                    self.flush()
                    if pause:
                        time.sleep(pause)
            finally:
                self.button(a["button"], False)
                self.flush()
        elif kind == "scroll":
            for number, count in scroll_plan(a["deltaX"], a["deltaY"], a["unit"]):
                for _ in range(count):
                    self.button(number, True)
                    self.button(number, False)
                    self.flush()
                    time.sleep(0.008)
        elif kind == "type":
            self.type_text(a["text"])
        elif kind == "key":
            code, shift, keysym = self.resolve_key(a["key"])
            scratch = code is None
            try:
                if scratch:
                    code, shift = self.map_scratch(keysym), False
                shift_key = self.modifier_code("Shift_L") if shift else None
                if shift_key in self.held_keys:
                    shift_key = None
                for index in range(a["repeat"]):
                    self.tap(code, shift_key)
                    self.flush()
                    if index + 1 < a["repeat"]:
                        time.sleep(0.02)
            finally:
                if scratch:
                    self.restore_scratch()

    # ---- held keys and buttons: modifiers are remembered per key/button ----------------------

    def hold(self, codes):
        """Press the modifier keycodes that are not held yet; returns the ones pressed now."""
        pressed = []
        try:
            for code in codes:
                if code not in self.held_keys:
                    self.key(code, True)
                    pressed.append(code)
        except BaseException:
            self.let_go(pressed)
            raise
        return pressed

    def let_go(self, codes):
        for code in reversed(codes):
            if code in self.held_keys:
                try:
                    self.key(code, False)
                except DriverError:
                    pass

    @staticmethod
    def remember(table, item, pressed):
        record = table.setdefault(item, [])
        record.extend(code for code in pressed if code not in record)

    def key_down(self, key, mods):
        code, shift, _keysym = self.resolve_key(key)
        if code is None:
            raise DriverError("unsupported", "Клавиши «%s» нет в текущей раскладке X11 — удерживать её нельзя; "
                                             "используйте action key" % key)
        if shift:  # "!" needs Shift in this layout: pressed here, released by the paired key_up
            mods = list(mods) + [self.modifier_code("Shift_L")]
        pressed = self.hold(mods)
        try:
            self.key(code, True)
        except BaseException:
            self.let_go(pressed)
            raise
        self.remember(self.key_mods, code, pressed)

    def key_up(self, key, mods):
        """Releases the key, then every modifier its key_down pressed (even when mods is empty)."""
        code, _shift, _keysym = self.resolve_key(key)
        if code is None:
            raise DriverError("unsupported", "Клавиши «%s» нет в текущей раскладке X11" % key)
        try:
            self.key(code, False)
        finally:
            recorded = self.key_mods.pop(code, [])
            self.let_go(recorded + [m for m in mods if m not in recorded])

    def mouse_down(self, a, mods):
        self.move(a["x"], a["y"])
        pressed = self.hold(mods)
        try:
            self.flush()
            self.button(a["button"], True)
        except BaseException:
            self.let_go(pressed)
            raise
        self.remember(self.button_mods, a["button"], pressed)

    def mouse_up(self, a, mods):
        try:
            self.move(a["x"], a["y"])
            self.button(a["button"], False)
        finally:
            recorded = self.button_mods.pop(a["button"], [])
            self.let_go(recorded + [m for m in mods if m not in recorded])

    # ---- windows (EWMH) ---------------------------------------------------------------------

    KEEP_TYPES = ("_NET_WM_WINDOW_TYPE_NORMAL", "_NET_WM_WINDOW_TYPE_DIALOG", "_NET_WM_WINDOW_TYPE_UTILITY")

    def supported(self):
        if self._supported is None:
            self._supported = set(self.prop_ints(self.root, "_NET_SUPPORTED"))
        return self._supported

    def require_ewmh(self):
        self.require_display()
        if self.atom("_NET_CLIENT_LIST") not in self.supported():
            self._supported = None  # a window manager may start later
            raise DriverError("unsupported", "Оконный менеджер не поддерживает EWMH (_NET_CLIENT_LIST) — список "
                                             "окон и управление ими недоступны; работают ввод и снимки экрана")

    def client_list(self):
        """Managed windows, front to back."""
        ids = self.prop_ints(self.root, "_NET_CLIENT_LIST_STACKING") or self.prop_ints(self.root, "_NET_CLIENT_LIST")
        del _X_ERRORS[:]
        return [w for w in reversed(ids) if w]

    def active_window(self):
        ids = self.prop_ints(self.root, "_NET_ACTIVE_WINDOW")
        return ids[0] if ids else 0

    def is_app_window(self, window):
        types = self.prop_ints(window, "_NET_WM_WINDOW_TYPE")
        if not types:
            return True
        keep = set(self.atom(name) for name in self.KEEP_TYPES)
        return any(t in keep for t in types)

    def window_entry(self, window, active):
        """Protocol window entry, or None when the window vanished."""
        ct = self.x.ctypes
        attrs = self.x.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, window, ct.byref(attrs)):
            del _X_ERRORS[:]
            return None
        x, y, child = ct.c_int(0), ct.c_int(0), ct.c_ulong(0)
        self.x.XTranslateCoordinates(self.dpy, window, self.root, 0, 0, ct.byref(x), ct.byref(y), ct.byref(child))
        left = right = top = bottom = 0
        extents = self.prop_ints(window, "_NET_FRAME_EXTENTS")
        if len(extents) == 4 and all(0 <= v < 1000 for v in extents):
            left, right, top, bottom = extents
        title = self.prop_text(window, "_NET_WM_NAME") or self.prop_text(window, "WM_NAME") or ""
        pids = self.prop_ints(window, "_NET_WM_PID")
        pid = int(pids[0]) if pids and 0 < pids[0] < 1 << 31 else None
        wm_class = (self.prop_text(window, "WM_CLASS") or "").split("\x00")
        app = (wm_class[1] if len(wm_class) > 1 and wm_class[1] else wm_class[0]) or ""
        if not app and pid:
            try:
                with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as handle:
                    app = handle.read().strip()
            except OSError:
                pass
        state = self.prop_ints(window, "_NET_WM_STATE")
        del _X_ERRORS[:]
        entry = {"id": hex(window), "title": optional_text(title), "app": app,
                 "bounds": {"x": x.value - left, "y": y.value - top,
                            "width": attrs.width + left + right, "height": attrs.height + top + bottom},
                 "focused": window == active,
                 "minimized": self.atom("_NET_WM_STATE_HIDDEN") in state}
        if pid:
            entry["pid"] = pid
        return entry

    def windows(self):
        self.require_ewmh()
        active = self.active_window()
        out, front = [], None
        for window in self.client_list():
            if not self.is_app_window(window):
                continue
            entry = self.window_entry(window, active)
            if entry:
                out.append(entry)
                if window == active:
                    front = entry
        if front is None and active:
            front = self.window_entry(active, active)
        del _X_ERRORS[:]
        frontmost = None
        if front:
            frontmost = {"name": front["app"]}
            if "pid" in front:
                frontmost["pid"] = front["pid"]
        return {"windows": out, "frontmost_app": frontmost}

    def client_message(self, window, type_name, data):
        event = self.x.XEvent()
        message = event.xclient
        message.type = CLIENT_MESSAGE
        message.send_event = 1
        message.display = self.dpy
        message.window = window
        message.message_type = self.atom(type_name)
        message.format = 32
        for index, value in enumerate(data[:5]):
            message.data[index] = int(value)
        if not self.x.XSendEvent(self.dpy, self.root, 0, SUBSTRUCTURE_MASK, self.x.ctypes.byref(event)):
            raise DriverError("failed", "X-сервер не принял сообщение %s оконному менеджеру" % type_name)
        self.flush()

    def wait_active(self, window, timeout=0.3):
        deadline = time.time() + timeout
        while True:
            active = self.active_window()
            if active == window:
                return True
            if active:
                # A modal dialog of the window may take the focus on its behalf.
                transient = self.prop_ints(active, "WM_TRANSIENT_FOR")
                del _X_ERRORS[:]  # the active window may vanish meanwhile; that is not our window's error
                if window in transient:
                    return True
            if time.time() >= deadline:
                return False
            time.sleep(0.03)

    def window_action(self, window, action, bounds=None):
        self.require_ewmh()
        name = "Окно %s" % hex(window)
        if window not in self.client_list():
            raise DriverError("not_found", "%s не найдено среди окон приложений (закрыто?) — запросите windows "
                                           "заново" % name)
        maximized = (self.atom("_NET_WM_STATE_MAXIMIZED_VERT"), self.atom("_NET_WM_STATE_MAXIMIZED_HORZ"))
        with self.trap(name):
            state = self.prop_ints(window, "_NET_WM_STATE")
            if action == "focus":
                self.client_message(window, "_NET_ACTIVE_WINDOW", [2, 0, self.active_window(), 0, 0])
                if not self.wait_active(window):
                    raise DriverError("blocked", "%s: оконный менеджер не передал ему фокус за 300 мс (защита от "
                                                 "кражи фокуса или модальное окно). Попросите пользователя "
                                                 "переключиться вручную или кликните по окну" % name)
            elif action == "minimize":
                if not self.x.XIconifyWindow(self.dpy, window, self.screen):
                    raise DriverError("failed", "%s: X-сервер не принял запрос на сворачивание" % name)
            elif action == "maximize":
                self.client_message(window, "_NET_WM_STATE", [1, maximized[0], maximized[1], 2, 0])
            elif action == "restore":
                if self.atom("_NET_WM_STATE_HIDDEN") in state:
                    self.client_message(window, "_NET_ACTIVE_WINDOW", [2, 0, self.active_window(), 0, 0])
                self.client_message(window, "_NET_WM_STATE", [0, maximized[0], maximized[1], 2, 0])
            elif action == "close":
                self.client_message(window, "_NET_CLOSE_WINDOW", [0, 2, 0, 0, 0])
            elif action == "set_bounds":
                if maximized[0] in state or maximized[1] in state:
                    self.client_message(window, "_NET_WM_STATE", [0, maximized[0], maximized[1], 2, 0])
                    time.sleep(0.05)
                left = right = top = bottom = 0
                extents = self.prop_ints(window, "_NET_FRAME_EXTENTS")
                if len(extents) == 4 and all(0 <= v < 1000 for v in extents):
                    left, right, top, bottom = extents
                width = max(1, bounds["width"] - left - right)
                height = max(1, bounds["height"] - top - bottom)
                if self.atom("_NET_MOVERESIZE_WINDOW") in self.supported():
                    # NorthWest gravity: x, y is the outer frame corner; x/y/width/height present; source 2.
                    flags = 1 | (0xf << 8) | (2 << 12)
                    self.client_message(window, "_NET_MOVERESIZE_WINDOW",
                                        [flags, bounds["x"], bounds["y"], width, height])
                else:
                    self.x.XMoveResizeWindow(self.dpy, window, bounds["x"], bounds["y"], width, height)
        deadline = time.time() + (0.5 if action == "close" else 0.15)
        entry = None
        while True:
            time.sleep(0.05)
            present = window in self.client_list()
            entry = self.window_entry(window, self.active_window()) if present else None
            if time.time() >= deadline or (action == "close" and entry is None):
                break
        return {"window": entry}

    # ---- protocol 1.1: app_at and background input ------------------------------------------

    def viewable_here(self, window):
        """Mapped (not iconified, not on a hidden workspace) and on the current desktop or sticky."""
        attrs = self.x.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, window, self.x.ctypes.byref(attrs)) or attrs.map_state != 2:
            del _X_ERRORS[:]
            return False
        desktop = self.prop_ints(window, "_NET_WM_DESKTOP")
        current = self.prop_ints(self.root, "_NET_CURRENT_DESKTOP")
        return not desktop or not current or desktop[0] in (current[0], 0xFFFFFFFF)

    def app_at(self, x, y):
        """Top application window under a global point (front to back, frames included)."""
        self.require_ewmh()
        active = self.active_window()
        own_pid = os.getppid()
        skip_taskbar = self.atom("_NET_WM_STATE_SKIP_TASKBAR")
        for window in self.client_list():
            if not self.is_app_window(window) or not self.viewable_here(window):
                continue
            entry = self.window_entry(window, active)
            if not entry or entry["minimized"]:
                continue
            b = entry["bounds"]
            if not (b["x"] <= x < b["x"] + b["width"] and b["y"] <= y < b["y"] + b["height"]):
                continue
            if entry.get("pid") == own_pid and skip_taskbar in self.prop_ints(window, "_NET_WM_STATE"):
                continue  # chatrepo's own click-through overlay (skipTaskbar), not an application
            app = {"name": entry["app"]}
            if "pid" in entry:
                app["pid"] = entry["pid"]
            del _X_ERRORS[:]
            return {"app": app, "window_id": entry["id"]}
        del _X_ERRORS[:]
        return {"app": None, "window_id": None}

    def deepest_child(self, window, gx, gy):
        """Deepest subwindow of `window` under the global point and the point relative to it."""
        ct = self.x.ctypes
        attrs = self.x.XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, window, ct.byref(attrs)):
            raise DriverError("not_found", "Окно %s больше не существует — запросите windows заново" % hex(window))
        lx, ly, child = ct.c_int(0), ct.c_int(0), ct.c_ulong(0)
        self.x.XTranslateCoordinates(self.dpy, self.root, window, gx, gy, ct.byref(lx), ct.byref(ly), ct.byref(child))
        if not (0 <= lx.value < attrs.width and 0 <= ly.value < attrs.height):
            raise bad_request("Точка (%d, %d) вне окна %s — координаты глобальные, как в input" % (gx, gy, hex(window)))
        target = window
        for _ in range(64):
            if not child.value:
                break
            target = child.value
            self.x.XTranslateCoordinates(self.dpy, self.root, target, gx, gy, ct.byref(lx), ct.byref(ly),
                                         ct.byref(child))
        return target, lx.value, ly.value

    def synthetic(self, target, kind, detail, state, local=(1, 1), global_point=(1, 1)):
        """XSendEvent of KeyPress(2)/KeyRelease(3)/ButtonPress(4)/ButtonRelease(5); send_event set."""
        event = self.x.XEvent()
        e = event.xbutton if kind in (4, 5) else event.xkey
        e.type = kind
        e.serial = 0
        e.send_event = 1
        e.display = self.dpy
        e.window = target
        e.root = self.root
        e.subwindow = 0
        e.time = 0  # CurrentTime
        e.x, e.y = int(local[0]), int(local[1])
        e.x_root, e.y_root = int(global_point[0]), int(global_point[1])
        e.state = state
        e.same_screen = 1
        if kind in (4, 5):
            e.button = detail
        else:
            e.keycode = detail
        mask = {2: 1 << 0, 3: 1 << 1, 4: 1 << 2, 5: 1 << 3}[kind]  # Key/Button Press/Release masks
        if not self.x.XSendEvent(self.dpy, target, 1, mask, self.x.ctypes.byref(event)):
            raise DriverError("blocked", "X-сервер не принял синтетическое событие XSendEvent")

    def group_of(self, code, keysym, default):
        for group in [default] + [g for g in range(4) if g != default]:
            for level in (0, 1):
                if self.x.XkbKeycodeToKeysym(self.dpy, code, group, level) == keysym:
                    return group
        return default

    def background_input(self, a):
        """Key/button events straight to a window: no XTEST, pointer and focus stay untouched."""
        self.require_display()
        window = a["window"]
        name = "Окно %s" % hex(window)
        if self.window_entry(window, 0) is None:
            raise DriverError("not_found", "%s не найдено (закрыто?) — запросите windows заново" % name)
        mods = 0
        for keysym_name in a.get("modifiers", []):
            mods |= MODIFIER_MASKS[keysym_name]
        group, _locked = self.xkb_state()
        with self.trap(name):
            try:
                if a["action"] == "click":
                    target, lx, ly = self.deepest_child(window, a["x"], a["y"])
                    button = a["button"]
                    pressed_mask = (1 << (7 + button)) if button <= 5 else 0  # Button1Mask = 1 << 8
                    for index in range(a["clicks"]):
                        self.synthetic(target, 4, button, mods, (lx, ly), (a["x"], a["y"]))
                        self.synthetic(target, 5, button, mods | pressed_mask, (lx, ly), (a["x"], a["y"]))
                        self.flush()
                        if index + 1 < a["clicks"]:
                            time.sleep(0.04)
                else:
                    if a["action"] == "type":
                        cache, items = {}, []
                        for char in a["text"]:
                            keysym = keysym_for_char(char)
                            if keysym not in cache:
                                cache[keysym] = self.find_in_group(keysym, group,
                                                                   self.x.XKeysymToKeycode(self.dpy, keysym))
                            items.append((keysym,) + cache[keysym] + (group,))
                    else:
                        code, shift, keysym = self.resolve_key(a["key"])
                        key_group = self.group_of(code, keysym, group) if code else group
                        items = [(keysym, code, shift, key_group)] * a["repeat"]
                    for index, (keysym, code, shift, key_group) in enumerate(items):
                        if code is None or code == self.scratch_code:
                            code, shift, key_group = self.map_scratch(keysym), False, 0
                        state = mods | ((key_group & 3) << 13) | (1 if shift else 0)
                        self.synthetic(window, 2, code, state)
                        self.synthetic(window, 3, code, state)
                        self.flush()
                        time.sleep(0.01 if index % 32 == 31 else 0.0015)
            finally:
                self.restore_scratch()
        return {"delivered": True, "method": "XSendEvent", "note": BACKGROUND_NOTE}

# --------------------------------------------------------------------------------------------
# AT-SPI (gi Atspi 2.0, loaded lazily)
# --------------------------------------------------------------------------------------------

ATSPI_MISSING = ("нет python3-gi / gir1.2-atspi-2.0 — дерево элементов недоступно; установите пакеты "
                 "python3-gi и gir1.2-atspi-2.0")
ELEMENT_ACTIONS = ACTION_ORDER + ("locate", "read", "select_text")
VALUE_ACTION_ROLES = ("slider", "spin_button", "scroll_bar")


class AtspiBackend(object):
    def __init__(self, wayland, x11=None):
        self.wayland = wayland
        self.x11 = x11
        self.Atspi = None
        self.error = None
        self.ready = False
        self.epoch = 0
        self.store = {}

    def available(self):
        """Cheap check (import only): no bus connection, no prompts."""
        if self.Atspi is not None:
            return True
        if self.error:
            return False
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
            self.Atspi = Atspi
            return True
        except Exception:
            self.error = ATSPI_MISSING
            return False

    def start(self):
        if self.ready:
            return
        if not self.available():
            raise DriverError("unsupported", ATSPI_MISSING)
        self.enable_accessibility()
        try:
            self.Atspi.init()
        except Exception as error:
            raise DriverError("failed", "Не удалось подключиться к шине доступности AT-SPI: %s. Проверьте, что "
                                        "запущен at-spi-bus-launcher (пакет at-spi2-core)" % error)
        try:
            self.Atspi.set_timeout(1500, 5000)  # a hung application must not stall the helper for 25 s
        except Exception:
            pass
        self.ready = True

    @staticmethod
    def enable_accessibility():
        """org.a11y.Status.IsEnabled = true: Chromium/Electron/Qt build their tree only when it is set."""
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.freedesktop.DBus.Properties", "Set",
                          GLib.Variant("(ssv)", ("org.a11y.Status", "IsEnabled", GLib.Variant("b", True))),
                          None, Gio.DBusCallFlags.NONE, 1000, None)
        except Exception:
            pass

    # ---- element description ----------------------------------------------------------------

    def has(self, states, name):
        return states.contains(getattr(self.Atspi.StateType, name))

    @staticmethod
    def interfaces(acc):
        try:
            return set(acc.get_interfaces() or ())
        except Exception:
            return set()

    def action_names(self, acc, ifaces):
        if "Action" not in ifaces:
            return []
        action = acc.get_action_iface()
        names = []
        for index in range(min(action.get_n_actions(), 16)):
            try:
                names.append(action.get_action_name(index) or "")
            except AttributeError:
                names.append(action.get_name(index) or "")
        return names

    def parent_selectable(self, acc):
        try:
            parent = acc.get_parent()
            return parent is not None and "Selection" in self.interfaces(parent)
        except Exception:
            return False

    def describe(self, acc, element_id, selectable_parent=None, states=None):
        """Protocol element dict, or None for a defunct object. A secure value is never read."""
        A = self.Atspi
        states = states if states is not None else acc.get_state_set()
        if self.has(states, "DEFUNCT"):
            return None
        raw = acc.get_role_name() or ""
        secure = acc.get_role() == A.Role.PASSWORD_TEXT or role_key(raw) == "password text"
        editable = self.has(states, "EDITABLE")
        role = normalize_role(raw, editable, self.has(states, "MULTI_LINE"))
        ifaces = self.interfaces(acc)
        element = {"id": element_id, "role": role, "raw_role": raw, "name": optional_text(acc.get_name() or "")}
        if not secure:
            value = None
            if role in TEXT_VALUE_ROLES and "Text" in ifaces:
                # acc.get_text(...) would call Accessible.get_text(): the Text interface is used through its class.
                value = A.Text.get_text(acc, 0, max(A.Text.get_character_count(acc), 0))
            elif role in NUMERIC_VALUE_ROLES and "Value" in ifaces:
                current = acc.get_value_iface().get_current_value()
                value = ("%d" % current) if float(current).is_integer() else ("%g" % current)
            if value not in (None, ""):
                element["value"] = optional_text(value)
        description = acc.get_description()
        if description:
            element["description"] = optional_text(description)
        if not self.wayland and "Component" in ifaces:
            rect = acc.get_component_iface().get_extents(A.CoordType.SCREEN)
            if rect.width > 0 and rect.height > 0 and rect.x > -100000 and rect.y > -100000:
                element["bounds"] = {"x": rect.x, "y": rect.y, "width": rect.width, "height": rect.height}
        element["enabled"] = self.has(states, "ENABLED") or self.has(states, "SENSITIVE")
        element["focused"] = self.has(states, "FOCUSED")
        element["secure"] = bool(secure)
        if selectable_parent is None:
            selectable_parent = self.parent_selectable(acc)
        element["actions"] = compose_actions(
            self.action_names(acc, ifaces), focusable=self.has(states, "FOCUSABLE"),
            editable_text="EditableText" in ifaces and (editable or secure),
            value_iface="Value" in ifaces and role in VALUE_ACTION_ROLES,
            component="Component" in ifaces and hasattr(A.Component, "scroll_to"),
            selectable_parent=selectable_parent)
        return element

    # ---- scope ------------------------------------------------------------------------------

    def applications(self):
        desktop = self.Atspi.get_desktop(0)
        apps = []
        for index in range(desktop.get_child_count()):
            try:
                app = desktop.get_child_at_index(index)
            except Exception:
                continue
            if app is not None:
                apps.append(app)
        return apps

    @staticmethod
    def children(acc):
        out = []
        try:
            count = acc.get_child_count()
        except Exception:
            return out
        for index in range(count):
            try:
                child = acc.get_child_at_index(index)
            except Exception:
                continue
            if child is not None:
                out.append(child)
        return out

    def app_for_pid(self, pid):
        for app in self.applications():
            try:
                if app.get_process_id() == pid:
                    return app
            except Exception:
                continue
        raise DriverError("not_found", "Приложение pid %d не зарегистрировано в AT-SPI. Chromium/Electron и Qt "
                                       "строят дерево только при включённых специальных возможностях — "
                                       "перезапустите приложение и повторите" % pid)

    def state_of(self, acc, name):
        try:
            return self.has(acc.get_state_set(), name)
        except Exception:
            return False

    def pick_window(self, app, title=None):
        windows = self.children(app)
        if not windows:
            raise DriverError("not_found", "У приложения «%s» нет окон в дереве доступности" % (app.get_name() or "?"))
        if title:
            for test in (lambda n: n == title, lambda n: n and (n in title or title in n)):
                for window in windows:
                    if test(window.get_name() or ""):
                        return window
        for name in ("ACTIVE", "SHOWING"):
            for window in windows:
                if self.state_of(window, name):
                    return window
        return windows[0]

    def active_window(self):
        for app in self.applications():
            for window in self.children(app):
                if self.state_of(window, "ACTIVE"):
                    return app, window
        raise DriverError("not_found", "Не найдено активное окно в дереве доступности: переключитесь в нужное "
                                       "окно или укажите pid")

    def x11_window(self, window_id):
        if self.wayland or self.x11 is None or not self.x11.open():
            raise DriverError("unsupported", "Идентификаторы окон доступны только в X11; на Wayland укажите pid "
                                             "или не указывайте область (будет взято активное окно)")
        entry = self.x11.window_entry(window_id, 0)
        if entry is None:
            raise DriverError("not_found", "Окно %s больше не существует — запросите windows заново" % hex(window_id))
        if not entry.get("pid"):
            raise DriverError("not_found", "Окно %s не сообщает свой pid (_NET_WM_PID) — укажите pid "
                                           "приложения явно" % hex(window_id))
        return entry

    def resolve_scope(self, opts):
        """(root accessible, app accessible, {"id", "title"})."""
        if isinstance(opts.get("window_id"), tuple):
            pid, index = opts["window_id"]
            app, frame, _states = self.atspi_frame(pid, index)
            return frame, app, {"id": "atspi:%d:%d" % (pid, index), "title": frame.get_name() or ""}
        if opts.get("window_id"):
            entry = self.x11_window(opts["window_id"])
            app = self.app_for_pid(entry["pid"])
            window = self.pick_window(app, entry["title"])
            return window, app, {"id": entry["id"], "title": entry["title"] or window.get_name() or ""}
        if opts.get("pid"):
            app = self.app_for_pid(opts["pid"])
            window = self.pick_window(app)
            return app, app, {"id": "", "title": window.get_name() or app.get_name() or ""}
        if not self.wayland and self.x11 is not None and self.x11.open():
            try:
                active = self.x11.active_window()
                if active:
                    return self.resolve_scope({"window_id": active})
            except DriverError:
                pass
        app, window = self.active_window()
        return window, app, {"id": "", "title": window.get_name() or ""}

    def window_at_point(self, point):
        """Frontmost X11 application window containing the point, or None."""
        if self.wayland or self.x11 is None or not self.x11.open():
            return None
        try:
            for entry in self.x11.windows()["windows"]:
                b = entry["bounds"]
                if not entry["minimized"] and b["x"] <= point["x"] < b["x"] + b["width"] and \
                        b["y"] <= point["y"] < b["y"] + b["height"]:
                    return parse_window_id(entry["id"])
        except DriverError:
            return None
        return None

    # ---- windows from AT-SPI (Wayland, protocol 1.1) ----------------------------------------

    def frames(self):
        """[(app, pid, child index, frame, states)] for application frames/windows/dialogs."""
        out = []
        for app in self.applications():
            try:
                pid = app.get_process_id()
                count = min(app.get_child_count(), 100)
            except Exception:
                continue
            for index in range(count):
                try:
                    frame = app.get_child_at_index(index)
                    if frame is None or normalize_role(frame.get_role_name()) not in ("window", "dialog"):
                        continue
                    states = frame.get_state_set()
                    if self.has(states, "DEFUNCT"):
                        continue
                except Exception:
                    continue
                out.append((app, pid, index, frame, states))
        return out

    def frame_entry(self, app, pid, index, frame, states):
        return {"id": "atspi:%d:%d" % (pid, index), "title": optional_text(frame.get_name() or ""),
                "app": app.get_name() or "", "pid": pid, "bounds": None,
                "focused": self.has(states, "ACTIVE"), "minimized": self.has(states, "ICONIFIED")}

    def wayland_windows(self):
        """Active first, then showing, then the rest (Wayland gives no stacking order)."""
        self.start()
        ranked = []
        for app, pid, index, frame, states in self.frames():
            try:
                entry = self.frame_entry(app, pid, index, frame, states)
            except Exception:
                continue
            rank = 0 if entry["focused"] else 1 if self.has(states, "SHOWING") else 2
            ranked.append((rank, len(ranked), entry))
        ranked.sort(key=lambda item: item[:2])
        windows = [entry for _rank, _order, entry in ranked]
        front = next((w for w in windows if w["focused"]), None)
        return {"windows": windows,
                "frontmost_app": {"name": front["app"], "pid": front["pid"]} if front else None}

    def atspi_frame(self, pid, index):
        app = self.app_for_pid(pid)
        try:
            frame = app.get_child_at_index(index) if index < app.get_child_count() else None
            states = frame.get_state_set() if frame is not None else None
        except Exception:
            frame = states = None
        if frame is None or self.has(states, "DEFUNCT"):
            raise DriverError("not_found", "Окно atspi:%d:%d больше не существует — запросите windows заново"
                              % (pid, index))
        return app, frame, states

    def focus_window(self, pid, index):
        """Wayland focus: window action activate/raise, else grab_focus; ACTIVE verified within 500 ms."""
        self.start()
        app, frame, states = self.atspi_frame(pid, index)
        if not self.has(states, "ACTIVE"):
            ifaces = self.interfaces(frame)
            done = False
            for number, name in enumerate(self.action_names(frame, ifaces)):
                if role_key(name) in ("activate", "raise"):
                    done = frame.get_action_iface().do_action(number)
                    break
            if not done and "Component" in ifaces:
                frame.get_component_iface().grab_focus()
            deadline = time.time() + 0.5
            while not self.state_of(frame, "ACTIVE"):
                if time.time() >= deadline:
                    raise DriverError("blocked", "Окно «%s»: композитор Wayland не дал его активировать (защита от "
                                                 "кражи фокуса). Попросите пользователя переключиться вручную "
                                                 "(Alt+Tab)" % (frame.get_name() or ""))
                time.sleep(0.05)
            states = frame.get_state_set()
        return {"window": self.frame_entry(app, pid, index, frame, states)}

    # ---- methods ----------------------------------------------------------------------------

    def new_epoch(self):
        self.epoch += 1
        self.store = {}
        return self.epoch

    def remember(self, acc):
        number = len(self.store) + 1
        self.store[number] = acc
        return "e%d.%d" % (self.epoch, number)

    def elements(self, opts):
        self.start()
        if opts["point"] is not None:
            if self.wayland:
                raise DriverError("unsupported", "На Wayland экранные координаты элементов недоступны — ищите "
                                                 "элементы по query и role")
            if not opts["window_id"] and not opts["pid"]:
                opts = dict(opts, window_id=self.window_at_point(opts["point"]))
        root, _app, window = self.resolve_scope(opts)
        self.new_epoch()
        if opts["point"] is not None:
            elements = self.chain_at_point(root, opts["point"])
            return {"epoch": self.epoch, "truncated": False, "window": window, "elements": elements}
        elements, truncated = self.traverse(root, opts)
        return {"epoch": self.epoch, "truncated": truncated, "window": window, "elements": elements}

    def chain_at_point(self, root, point):
        A = self.Atspi
        x, y = point["x"], point["y"]
        deepest = None
        current = root
        seen = set()
        while current not in seen:
            seen.add(current)
            if "Component" not in self.interfaces(current):
                break
            child = current.get_component_iface().get_accessible_at_point(x, y, A.CoordType.SCREEN)
            if child is None or child == current:
                break
            deepest = current = child
        if deepest is None:
            if "Component" in self.interfaces(root) and root.get_component_iface().contains(x, y, A.CoordType.SCREEN):
                deepest = root
            else:
                return []
        chain, node, ancestors = [], deepest, set()
        while node is not None and node not in ancestors:
            ancestors.add(node)
            try:
                if node.get_role() == A.Role.APPLICATION:
                    break
                element = self.describe(node, "")
            except Exception:
                break
            if element:
                element["id"] = self.remember(node)
                chain.append(element)
            if node == root:
                break
            node = node.get_parent()
        return chain

    def traverse(self, root, opts):
        out, truncated = [], False
        stack = [(root, 0, None)]
        visited = set()
        while stack:
            acc, depth, selectable = stack.pop()
            if acc in visited:
                continue
            visited.add(acc)
            try:
                states = acc.get_state_set()
                if self.has(states, "DEFUNCT") or depth > 0 and not self.has(states, "SHOWING"):
                    continue
                element = self.describe(acc, "", selectable, states)
                count = acc.get_child_count()
            except Exception:
                continue
            if element and element_matches(element, opts["query"], opts["role"]):
                if opts["max"] is not None and len(out) >= opts["max"]:
                    truncated = True
                    break
                element["id"] = self.remember(acc)
                out.append(element)
            if count <= 0:
                continue
            if opts["depth"] is not None and depth >= opts["depth"]:
                truncated = True
                continue
            child_selectable = "Selection" in self.interfaces(acc)
            for index in range(count - 1, -1, -1):
                try:
                    child = acc.get_child_at_index(index)
                except Exception:
                    continue
                if child is not None:
                    stack.append((child, depth + 1, child_selectable))
        return out, truncated

    def focused(self):
        self.start()
        root, app, _window = self.resolve_scope({})
        stack, seen, found = [root], set(), None
        while stack:
            acc = stack.pop()
            if acc in seen:
                continue
            seen.add(acc)
            try:
                states = acc.get_state_set()
                if self.has(states, "DEFUNCT") or acc is not root and not self.has(states, "SHOWING"):
                    continue
                if self.has(states, "FOCUSED"):
                    found = acc
                    break
                stack.extend(reversed(self.children(acc)))
            except Exception:
                continue
        element = None
        if found is not None:
            try:
                element = self.describe(found, "")
            except Exception:
                element = None
        info = {"name": app.get_name() or ""}
        try:
            info["pid"] = app.get_process_id()
        except Exception:
            pass
        return {"element": element, "app": info}

    def element_action(self, element_id, action, params):
        epoch, number = parse_element_id(element_id)
        self.start()
        acc = self.store.get(number) if epoch == self.epoch else None
        if acc is None:
            raise DriverError("stale", "Элемент %s из прошлой выборки — получите элементы заново (elements)" % element_id)
        try:
            element = self.describe(acc, element_id)
        except Exception:
            element = None
        if element is None:
            raise DriverError("stale", "Элемент %s больше не существует — получите элементы заново" % element_id)
        if action == "read":
            return self.read_text(acc, element)
        if action == "select_text":
            return dict(self.select_text(acc, element, params), element=element)
        if action != "locate":
            if action not in element["actions"]:
                raise DriverError("unsupported", "Элемент «%s» (%s) не поддерживает «%s»; доступно: %s" % (
                    element["name"], element["role"], action, ", ".join(element["actions"] + ["locate", "read"])))
            self.perform(acc, element, action, params)
            time.sleep(0.05)
            try:
                element = self.describe(acc, element_id)
            except Exception:
                element = None
        return {"element": element}

    def read_text(self, acc, element):
        """element_action read: full text, selection, caret. Never for secure fields."""
        result = {"element": element, "text": None}
        if element.get("secure"):
            return result  # a password is never read — neither text nor selection
        ifaces = self.interfaces(acc)
        if "Text" in ifaces:
            Text = self.Atspi.Text
            count = Text.get_character_count(acc)
            full = Text.get_text(acc, 0, -1) if count < 0 else (Text.get_text(acc, 0, count) if count else "")
            full = full or ""
            result["text"] = full
            if Text.get_n_selections(acc) > 0:
                selection = Text.get_selection(acc, 0)
                start, end = selection.start_offset, selection.end_offset
                if end > start:
                    result["selected_text"] = (Text.get_text(acc, start, end) or "")
            caret = Text.get_caret_offset(acc)
            if caret is not None and caret >= 0:
                result["caret"] = caret
        elif "Value" in ifaces:
            current = acc.get_value_iface().get_current_value()
            result["text"] = ("%d" % current) if float(current).is_integer() else ("%g" % current)
        else:
            result["text"] = acc.get_name() or ""
        return result

    def select_text(self, acc, element, params):
        """element_action select_text: change the selection through Atspi.Text and read it back."""
        if element.get("secure"):
            raise DriverError("secure_field", "Поле пароля: его текст не выделяется и не читается")
        if "Text" not in self.interfaces(acc):
            raise DriverError("unsupported", "У элемента «%s» нет интерфейса Text AT-SPI: выделение через доступность "
                                             "невозможно" % element["name"])
        # Text methods are called on the interface class: acc.get_text(...) collides with Accessible.get_text().
        Text = self.Atspi.Text
        count = Text.get_character_count(acc)
        start, end = select_text_range((Text.get_text(acc, 0, count) or "") if count > 0 else "", params)
        for index in range(Text.get_n_selections(acc) - 1, -1, -1):
            Text.remove_selection(acc, index)
        ok = Text.add_selection(acc, start, end) if end > start else Text.set_caret_offset(acc, start)
        if Text.get_n_selections(acc) > 0:
            selection = Text.get_selection(acc, 0)
            applied = (selection.start_offset, selection.end_offset)
        else:
            applied = (Text.get_caret_offset(acc),) * 2
        if ok is False or applied != (start, end):
            raise DriverError("failed", "Приложение не применило выделение (сейчас %d–%d, нужно %d–%d)" % (
                applied + (start, end)))
        return {"selection": {"start": start, "end": end},
                "selected_text": (Text.get_text(acc, start, end) or "") if end > start else ""}

    def perform(self, acc, element, action, params):
        A = self.Atspi
        states = acc.get_state_set()
        ifaces = self.interfaces(acc)
        names = self.action_names(acc, ifaces)
        index = next((i for i, name in enumerate(names) if action in normalize_action_name(name)), None)
        ok = True
        if action in ("expand", "collapse") and self.has(states, "EXPANDABLE"):
            if self.has(states, "EXPANDED") == (action == "expand"):
                return  # already in the requested state; a toggle action would flip it back
        named = action in ("press", "show_menu", "expand", "collapse")
        if named and index is None:
            raise DriverError("unsupported", "У элемента «%s» нет действия AT-SPI для «%s»; доступно: %s"
                              % (element["name"], action, ", ".join(names) or "нет"))
        if named or (action == "select" and index is not None):
            ok = acc.get_action_iface().do_action(index)
        elif action == "select":
            parent = acc.get_parent()
            ok = parent.get_selection_iface().select_child(acc.get_index_in_parent())
        elif action == "focus":
            ok = acc.get_component_iface().grab_focus()
        elif action == "set_value":
            value = params.get("value")
            if value is None or isinstance(value, (dict, list)):
                raise bad_request("Для set_value нужно поле value (строка или число)")
            if "EditableText" in ifaces and element["role"] not in VALUE_ACTION_ROLES:
                ok = acc.get_editable_text_iface().set_text_contents(str(value))
            else:
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    raise bad_request("Элемент «%s» принимает только число в value" % element["name"])
                ok = acc.get_value_iface().set_current_value(number)
        elif action in ("increment", "decrement"):
            value = acc.get_value_iface()
            step = value.get_minimum_increment() or 1.0
            if step <= 0:
                step = 1.0
            target = value.get_current_value() + (step if action == "increment" else -step)
            target = min(max(target, value.get_minimum_value()), value.get_maximum_value())
            ok = value.set_current_value(target)
        elif action == "scroll_into_view":
            ok = acc.get_component_iface().scroll_to(A.ScrollType.ANYWHERE)
        if ok is False:
            raise DriverError("failed", "Приложение отклонило действие «%s» над элементом «%s»" % (action, element["name"]))

# --------------------------------------------------------------------------------------------
# Driver: dispatch
# --------------------------------------------------------------------------------------------

WINDOW_ACTIONS = ("focus", "minimize", "maximize", "restore", "close", "set_bounds")
WAYLAND_WINDOW_HINT = ("На Wayland программа не может двигать, сворачивать и закрывать чужие окна — доступен только "
                       "focus. Используйте клавиши через портал: Super+↑ — развернуть, Super+↓ — восстановить, "
                       "Super+H — свернуть, Alt+F4 — закрыть")
BACKGROUND_NOTE = ("многие приложения (GTK3+, Qt, Chromium) синтетические события XSendEvent игнорируют — "
                   "проверьте снимком")
OCR_MISSING = ("нет tesseract — распознавание текста (ocr) недоступно; установите пакеты tesseract-ocr и "
               "tesseract-ocr-rus")
CLIPBOARD_MISSING_X11 = ("нет xclip — помощник не кладёт файлы в буфер обмена (clipboard_files); установите пакет "
                         "xclip, иначе приложение использует свой буфер")
CLIPBOARD_MISSING_WAYLAND = ("нет wl-copy / wl-paste — помощник не кладёт файлы в буфер обмена (clipboard_files); "
                             "установите пакет wl-clipboard, иначе приложение использует свой буфер")
WAYLAND_REFUSAL = ("%s: Wayland запрещает это программам вне портала xdg-desktop-portal. Ввод и снимки идут через "
                   "сеанс общего доступа приложения (wayland_portal.py); этот помощник на Wayland отвечает только "
                   "за дерево элементов и запуск приложений")
NO_DISPLAY = ("%s: не задана переменная DISPLAY — нет графической сессии X11. Запустите приложение из "
              "графического сеанса")


def describe_exception(error):
    message = getattr(error, "message", None) or str(error) or type(error).__name__
    return optional_text("Внутренняя ошибка помощника (%s): %s" % (type(error).__name__, message))


class Driver(object):
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.wayland = (self.env.get("XDG_SESSION_TYPE", "").lower() == "wayland"
                        or bool(self.env.get("WAYLAND_DISPLAY")))
        display = self.env.get("DISPLAY", "")
        self.x11 = X11Backend(display) if display and not self.wayland else None
        self.atspi = AtspiBackend(self.wayland, self.x11)
        self.children = []
        self._gio = None
        self._tesseract = None
        self.methods = {
            "hello": self.hello, "input": self.input, "cursor": self.cursor, "windows": self.windows,
            "window": self.window, "launch": self.launch, "elements": self.elements,
            "element_action": self.element_action, "focused": self.focused,
            "ocr": self.ocr, "clipboard_files": self.clipboard_files, "app_at": self.app_at,
            "background_input": self.background_input, "capture": self.capture,
        }

    # ---- transport --------------------------------------------------------------------------

    def handle_line(self, line):
        if not line.strip():
            return None
        try:
            request = json.loads(line)
        except ValueError:
            return {"id": None, "error": {"code": "bad_request", "message": "Строка запроса не является JSON; "
                                          "ожидается {\"id\", \"method\", \"params\"} в одной строке"}}
        return self.handle(request)

    def handle(self, request):
        rid = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise bad_request("Запрос должен быть JSON-объектом {\"id\", \"method\", \"params\"}")
            method = request.get("method")
            params = request.get("params")
            params = {} if params is None else params
            if not isinstance(params, dict):
                raise bad_request("Поле params должно быть объектом")
            handler = self.methods.get(method) if isinstance(method, str) else None
            if handler is None:
                raise bad_request("Неизвестный метод «%s»; поддерживаются: %s" % (method, ", ".join(self.methods)))
            del _X_ERRORS[:]
            self.reap()
            result = handler(params)
            json.dumps(result, ensure_ascii=False)
            return {"id": rid, "result": result}
        except DriverError as error:
            return {"id": rid, "error": {"code": error.code, "message": error.message}}
        except Exception as error:
            return {"id": rid, "error": {"code": "failed", "message": describe_exception(error)}}

    def close(self):
        if self.x11 is not None:
            self.x11.close()

    def reap(self):
        self.children = [child for child in self.children if child.poll() is None]

    # ---- backends ---------------------------------------------------------------------------

    def x_backend(self, what):
        if self.wayland:
            raise DriverError("unsupported", WAYLAND_REFUSAL % what)
        if self.x11 is None:
            raise DriverError("unsupported", NO_DISPLAY % what)
        self.x11.require_display()
        return self.x11

    # ---- methods ----------------------------------------------------------------------------

    def hello(self, _params):
        notes = []
        if self.wayland:
            backend, display_ok, input_ok = "linux-wayland-atspi", False, False
            notes.append("Wayland: ввод и курсор недоступны вне портала — приложение управляет вводом через "
                         "xdg-desktop-portal; помощник отвечает за дерево элементов, окна (из AT-SPI, из действий "
                         "только focus) и запуск приложений")
        else:
            backend = "linux-x11"
            if self.x11 is None:
                notes.append(NO_DISPLAY % "Ввод, курсор и окна недоступны")
            display_ok = self.x11 is not None and self.x11.open()
            input_ok = display_ok and self.x11.xtest
            if self.x11 is not None:
                notes.extend(self.x11.notes)
        elements = self.atspi.available()
        if not elements:
            notes.append(ATSPI_MISSING)
        elif self.wayland:
            notes.append("На Wayland у элементов нет экранных координат (bounds): нажимайте их через element_action")
        tesseract, langs = self.tesseract()
        if not tesseract:
            notes.append(OCR_MISSING)
        else:
            missing = [code for code in ("rus", "eng") if code not in langs]
            if missing:
                notes.append("в tesseract нет языков %s — установите пакеты %s" % (
                    ", ".join(missing), ", ".join("tesseract-ocr-" + code for code in missing)))
        clipboard = self.clipboard_tool() is not None
        if not clipboard and (self.wayland or self.x11 is not None):
            notes.append(CLIPBOARD_MISSING_WAYLAND if self.wayland else CLIPBOARD_MISSING_X11)
        windows = elements if self.wayland else display_ok
        return {"protocol": PROTOCOL, "platform": "linux", "backend": backend,
                "capabilities": {"input": input_ok, "unicode_type": input_ok, "cursor": display_ok,
                                 "windows": windows, "window_control": windows,
                                 "elements": elements, "launch": True, "select_text": elements,
                                 "ocr": bool(tesseract), "clipboard_files": clipboard, "app_at": display_ok,
                                 "background_input": display_ok, "touch": False,
                                 "capture": bool(self.wayland or display_ok)},
                "permissions": {"accessibility": "not_applicable"},
                "notes": notes}

    def capture(self, params):
        if self.wayland:
            raise DriverError("unsupported", "Wayland capture выполняется через xdg-desktop-portal")
        return self.x_backend("Захват экрана").capture(params.get("region"))

    def input(self, params):
        action = parse_input(params)
        return self.x_backend("Ввод").input(action)

    def cursor(self, _params):
        return self.x_backend("Позиция указателя").cursor()

    def windows(self, _params):
        if self.wayland:
            if not self.atspi.available():
                raise DriverError("unsupported", "Список окон на Wayland берётся из AT-SPI, а он недоступен: "
                                                 + ATSPI_MISSING)
            return self.atspi.wayland_windows()
        return self.x_backend("Список окон").windows()

    def window(self, params):
        if self.wayland:
            window_id = parse_any_window_id(params.get("id"))
            if params.get("action") not in WINDOW_ACTIONS:
                raise bad_request("Неизвестное действие с окном «%s»; допустимы: %s"
                                  % (params.get("action"), ", ".join(WINDOW_ACTIONS)))
            if params["action"] != "focus":
                raise DriverError("unsupported", WAYLAND_WINDOW_HINT)
            if not isinstance(window_id, tuple):
                raise DriverError("unsupported", "На Wayland окна задаются идентификаторами вида atspi:<pid>:<n> "
                                                 "из ответа windows")
            if not self.atspi.available():
                raise DriverError("unsupported", "Wayland: управление окнами идёт через AT-SPI, а он недоступен: "
                                                 + ATSPI_MISSING)
            return self.atspi.focus_window(*window_id)
        window_id = parse_window_id(params.get("id"))
        action = params.get("action")
        if action not in WINDOW_ACTIONS:
            raise bad_request("Неизвестное действие с окном «%s»; допустимы: %s" % (action, ", ".join(WINDOW_ACTIONS)))
        bounds = parse_bounds(params.get("bounds")) if action == "set_bounds" else None
        return self.x_backend("Управление окнами").window_action(window_id, action, bounds)

    def elements(self, params):
        return self.atspi.elements(parse_elements_params(params))

    def element_action(self, params):
        parse_element_id(params.get("id"))
        action = params.get("action")
        if action not in ELEMENT_ACTIONS:
            raise bad_request("Неизвестное действие с элементом «%s»; допустимы: %s" % (action, ", ".join(ELEMENT_ACTIONS)))
        return self.atspi.element_action(params["id"], action, params)

    def focused(self, _params):
        return self.atspi.focused()

    # ---- protocol 1.1 -----------------------------------------------------------------------

    def which(self, name):
        return shutil.which(name, path=self.env.get("PATH"))

    def tesseract(self):
        """(tesseract path | None, installed languages); the language list is probed once."""
        if self._tesseract is None:
            path = self.which("tesseract")
            if not path:
                return None, []
            langs = []
            try:
                done = subprocess.run([path, "--list-langs"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=5, env=self.env)
                langs = parse_list_langs((done.stdout + b"\n" + done.stderr).decode("utf-8", "replace"))
            except (OSError, subprocess.SubprocessError):
                pass
            self._tesseract = (path, langs)
        return self._tesseract

    def ocr(self, params):
        data, suffix = decode_image(params.get("image_b64"))
        wanted = tesseract_languages(params.get("languages"))
        path, available = self.tesseract()
        if not path:
            raise DriverError("unsupported", "Распознавание текста недоступно: нет tesseract — установите пакеты "
                                             "tesseract-ocr и tesseract-ocr-rus")
        notes = []
        chosen = [code for code in wanted if code in available]
        missing = [code for code in wanted if code not in available]
        if missing:
            notes.append("в tesseract нет языков %s — установите пакеты %s" % (
                ", ".join(missing), ", ".join("tesseract-ocr-" + code.replace("_", "-") for code in missing)))
        if not chosen:
            fallback = [code for code in available if code != "osd"]
            if not fallback:
                raise DriverError("unsupported", "В tesseract не установлено ни одного языка — установите пакеты "
                                                 "tesseract-ocr-rus и tesseract-ocr-eng")
            chosen = fallback[:1]
            notes.append("распознано языком %s" % chosen[0])
        fd, image = tempfile.mkstemp(prefix="chatrepo-ocr-", suffix=suffix)  # 0600
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                done = subprocess.run([path, image, "stdout", "-l", "+".join(chosen), "--psm", "3", "tsv"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      timeout=30, env=self.env)
            except subprocess.TimeoutExpired:
                raise DriverError("failed", "tesseract не уложился в 30 с — передайте картинку поменьше или область")
            except OSError as error:
                raise DriverError("failed", "Не удалось запустить tesseract: %s" % (error.strerror or error))
        finally:
            try:
                os.unlink(image)
            except OSError:
                pass
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise DriverError("failed", "tesseract завершился с ошибкой: %s" % optional_text(detail[-1] if detail else
                                                                                        "код %d" % done.returncode))
        lines = parse_tesseract_tsv(done.stdout.decode("utf-8", "replace"))
        result = {"text": "\n".join(line["text"] for line in lines), "engine": "tesseract", "lines": lines}
        if notes:
            result["note"] = "; ".join(notes)
        return result

    def clipboard_tool(self):
        """("wayland" | "x11", copy tool, paste tool) or None."""
        if self.wayland:
            copy, paste = self.which("wl-copy"), self.which("wl-paste")
            return ("wayland", copy, paste) if copy and paste else None
        if self.x11 is None:
            return None
        xclip = self.which("xclip")
        return ("x11", xclip, xclip) if xclip else None

    def clipboard_files(self, params):
        action = params.get("action")
        if action not in ("get", "set"):
            raise bad_request("Поле action для clipboard_files должно быть \"get\" или \"set\"")
        paths = clipboard_paths(params.get("paths")) if action == "set" else None
        tool = self.clipboard_tool()
        if tool is None:
            if not self.wayland and self.x11 is None:
                raise DriverError("unsupported", NO_DISPLAY % "Буфер обмена")
            raise DriverError("unsupported", "Буфер обмена с файлами недоступен помощнику: " +
                              (CLIPBOARD_MISSING_WAYLAND if self.wayland else CLIPBOARD_MISSING_X11))
        kind, copy, paste = tool
        if action == "get":
            for target in ("text/uri-list", "x-special/gnome-copied-files"):
                argv = ([paste, "--no-newline", "--type", target] if kind == "wayland"
                        else [paste, "-selection", "clipboard", "-o", "-t", target])
                try:
                    done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, timeout=3, env=self.env)
                except (OSError, subprocess.SubprocessError):
                    continue
                if done.returncode == 0 and done.stdout.strip():
                    return {"paths": parse_uri_list(done.stdout.decode("utf-8", "replace"))}
            return {"paths": []}
        # One owner process serves one target: text/uri-list is what file managers and toolkits paste.
        argv = ([copy, "--type", "text/uri-list"] if kind == "wayland"
                else [copy, "-selection", "clipboard", "-t", "text/uri-list", "-i"])
        try:
            # The tool forks a background owner of the selection: no output pipes to wait on.
            done = subprocess.run(argv, input=build_uri_list(paths).encode("utf-8"), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=5, env=self.env, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as error:
            raise DriverError("failed", "Не удалось положить файлы в буфер обмена (%s): %s" % (os.path.basename(copy),
                                                                                               error))
        if done.returncode != 0:
            raise DriverError("failed", "%s не смог занять буфер обмена (код %d)" % (os.path.basename(copy),
                                                                                  done.returncode))
        return {"count": len(paths)}

    def app_at(self, params):
        x, y = _number(params, "x"), _number(params, "y")
        return self.x_backend("Определение приложения под точкой").app_at(x, y)

    def background_input(self, params):
        action = parse_background(params)
        return self.x_backend("Фоновый ввод").background_input(action)

    # ---- launch -----------------------------------------------------------------------------

    def gio(self):
        if self._gio is None:
            try:
                import gi
                gi.require_version("Gio", "2.0")
                from gi.repository import Gio
                self._gio = Gio
            except Exception:
                self._gio = False
        return self._gio or None

    def launch(self, params):
        app = params.get("app")
        args = params.get("args", [])
        if not isinstance(app, str) or not app.strip():
            raise bad_request("Для launch нужно строковое поле app (имя, .desktop id или путь)")
        if args is None:
            args = []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise bad_request("Поле args должно быть списком строк")
        argv, name = self.resolve_launch(app.strip(), args)
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                                     env=self.env)
        except FileNotFoundError:
            raise DriverError("not_found", "Программа «%s» не найдена (%s)" % (name, argv[0]))
        except PermissionError:
            raise DriverError("permission", "Нет права запускать %s — проверьте права на файл" % argv[0])
        except OSError as error:
            raise DriverError("failed", "Не удалось запустить %s: %s" % (argv[0], error.strerror or error))
        self.children.append(child)
        return {"app": name, "pid": child.pid}

    def resolve_launch(self, app, args):
        """app → (argv, display name). Path → exact .desktop id/name → PATH → fuzzy desktop search."""
        if "/" in app or app.startswith("~"):
            path = os.path.expanduser(app)
            if path.endswith(".desktop") and os.path.isfile(path):
                entry = read_desktop_entry(path) or {}
                if entry.get("Exec"):
                    return exec_to_argv(entry["Exec"], args), entry.get("Name") or os.path.basename(path)
            if os.path.isfile(path):
                if os.access(path, os.X_OK):
                    return [path] + list(args), os.path.basename(path)
                raise DriverError("permission", "Файл %s не исполняемый — сделайте его исполняемым (chmod +x) "
                                                "или укажите программу, которая его открывает" % path)
            raise DriverError("not_found", "Файл %s не найден" % path)
        found = self.find_desktop(app)
        if found:
            return exec_to_argv(found[0], args), found[1]
        executable = shutil.which(app, path=self.env.get("PATH"))
        if executable:
            return [executable] + list(args), app
        found = self.search_desktop(app)
        if found:
            return exec_to_argv(found[0], args), found[1]
        raise DriverError("not_found", "Приложение «%s» не найдено: нет такого .desktop-файла, программы в PATH или "
                                       "пути. Укажите точное имя команды, .desktop id или полный путь" % app)

    def desktop_files(self):
        for directory in desktop_dirs(self.env):
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in names:
                if name.endswith(".desktop"):
                    yield name[:-len(".desktop")], os.path.join(directory, name)

    @staticmethod
    def usable(entry):
        return bool(entry and entry.get("Exec") and entry.get("Type", "Application") == "Application"
                    and entry.get("Hidden", "false").lower() != "true")

    def find_desktop(self, app):
        """Exact desktop id (also reverse-DNS tail) or exact Name → (Exec, Name)."""
        ids = [app] if app.endswith(".desktop") else [app + ".desktop"]
        gio = self.gio()
        if gio:
            for desktop_id in ids + [i.lower() for i in ids if i != i.lower()]:
                try:
                    info = gio.DesktopAppInfo.new(desktop_id)
                except Exception:
                    info = None
                if info is not None and info.get_commandline():
                    return info.get_commandline(), info.get_name() or desktop_id
        wanted = app[:-len(".desktop")].casefold() if app.endswith(".desktop") else app.casefold()
        for stem, path in self.desktop_files():
            key = stem.casefold()
            if key == wanted or key.rsplit(".", 1)[-1] == wanted:
                entry = read_desktop_entry(path)
                if self.usable(entry):
                    return entry["Exec"], entry.get("Name") or stem
        for stem, path in self.desktop_files():
            entry = read_desktop_entry(path)
            if self.usable(entry) and entry.get("Name", "").casefold() == wanted:
                return entry["Exec"], entry["Name"]
        return None

    def search_desktop(self, app):
        gio = self.gio()
        if gio:
            try:
                groups = gio.DesktopAppInfo.search(app) or []
            except Exception:
                groups = []
            for group in groups:
                for desktop_id in group:
                    try:
                        info = gio.DesktopAppInfo.new(desktop_id)
                    except Exception:
                        info = None
                    if info is not None and info.get_commandline():
                        return info.get_commandline(), info.get_name() or desktop_id
        wanted = app.casefold()
        for stem, path in self.desktop_files():
            entry = read_desktop_entry(path)
            if self.usable(entry) and entry.get("NoDisplay", "false").lower() != "true" and \
                    wanted in entry.get("Name", "").casefold():
                return entry["Exec"], entry["Name"]
        return None


# --------------------------------------------------------------------------------------------
# stdio loop
# --------------------------------------------------------------------------------------------

def main():
    driver = Driver()

    def terminate(_signum, _frame):
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), terminate)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    try:
        while True:
            raw = stdin.readline()
            if not raw:
                break
            reply = driver.handle_line(raw.decode("utf-8", "replace"))
            if reply is None:
                continue
            try:
                data = json.dumps(reply, ensure_ascii=False)
            except (TypeError, ValueError) as error:
                data = json.dumps({"id": reply.get("id"), "error": {"code": "failed",
                                   "message": describe_exception(error)}}, ensure_ascii=False)
            # Bytes, not text: replies stay UTF-8 even under an ASCII locale.
            stdout.write(data.encode("utf-8") + b"\n")
            stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        # EOF, SIGTERM or a broken pipe: never leave keys or buttons held.
        driver.close()


if __name__ == "__main__":
    main()
