"""Run-owned ScreenCast + RemoteDesktop portal. No shell, root, or XWayland fallback.

Persistence (`--token-file PATH`, protocol 1.1):
  * RemoteDesktop >= 2 carries persistence for the whole remote-desktop session, so persist_mode=2 and
    restore_token go to RemoteDesktop.SelectDevices and ScreenCast.SelectSources gets neither (a
    remote-desktop screencast must not persist on its own).
  * Only on old portals (RemoteDesktop < 2) with ScreenCast >= 4 are they passed to SelectSources.
  * Tokens are single-use: every Start returns a fresh one, written atomically (0600, tmp + rename).
  * A token the portal rejects is deleted and the normal consent dialog appears once — never a loop;
    a cancelled dialog is never re-asked.

Capture (protocol 1.1): appsink receives raw RGB frames at the stream's native pixel size (5 fps,
latest frame only); scaling/cropping/PNG encoding happen on demand with GdkPixbuf.
"""
import base64
import json
import math
import os
import signal
import sys
import threading
import time
import uuid

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
REMOTE = "org.freedesktop.portal.RemoteDesktop"
CAST = "org.freedesktop.portal.ScreenCast"
MAX_EDGE = max(256, min(8192, int(os.getenv("COMPUTER_CAPTURE_MAX_EDGE", "1568"))))
KEYBOARD_POINTER = 3
TOUCHSCREEN = 4
PINCH_DISTANCE = 200.0
ROTATE_RADIUS = 100.0
GESTURE_STEPS = 20


class InputError(ValueError):
    """Invalid action arguments do not revoke an otherwise healthy portal session."""


# Protocol key names (lower case, no spaces/'_'/'-') → X keysyms (keysymdef.h, XF86keysym.h).
KEYSYMS = {
    "enter": 0xff0d, "return": 0xff0d, "tab": 0xff09, "space": 0x20, "backspace": 0xff08,
    "delete": 0xffff, "forwarddelete": 0xffff, "del": 0xffff, "escape": 0xff1b, "esc": 0xff1b,
    "insert": 0xff63, "home": 0xff50, "end": 0xff57, "pageup": 0xff55, "pagedown": 0xff56,
    "left": 0xff51, "arrowleft": 0xff51, "up": 0xff52, "arrowup": 0xff52,
    "right": 0xff53, "arrowright": 0xff53, "down": 0xff54, "arrowdown": 0xff54,
    "capslock": 0xffe5, "numlock": 0xff7f, "scrolllock": 0xff14, "printscreen": 0xff61, "pause": 0xff13,
    "menu": 0xff67, "apps": 0xff67,
    "shift": 0xffe1, "control": 0xffe3, "ctrl": 0xffe3, "option": 0xffe9, "alt": 0xffe9,
    "command": 0xffeb, "cmd": 0xffeb, "meta": 0xffeb, "win": 0xffeb, "super": 0xffeb,
    "multiply": 0xffaa, "add": 0xffab, "subtract": 0xffad, "decimal": 0xffae, "divide": 0xffaf,
    "numpadenter": 0xff8d,
    "volumeup": 0x1008ff13, "volumedown": 0x1008ff11, "volumemute": 0x1008ff12,
    "medianext": 0x1008ff17, "mediaprev": 0x1008ff16, "mediaplay": 0x1008ff14, "mediastop": 0x1008ff15,
}
KEYSYMS.update({"f%d" % n: 0xffbe + n - 1 for n in range(1, 21)})
KEYSYMS.update({"numpad%d" % n: 0xffb0 + n for n in range(10)})


def char_keyval(char):
    """One character → keysym (Latin-1 as is, otherwise 0x01000000 | codepoint)."""
    if char in ("\n", "\r"):
        return 0xff0d
    if char == "\t":
        return 0xff09
    code = ord(char)
    if 0x20 <= code <= 0x7e or 0xa0 <= code <= 0xff:
        return code
    if code < 0x20 or 0x7f <= code < 0xa0:
        raise InputError("Управляющий символ U+%04X нельзя нажать как клавишу" % code)
    return 0x01000000 | code


def keyval(key):
    """Protocol key name (case/spaces ignored) or one printable character → keysym."""
    if not isinstance(key, str) or not key:
        raise InputError("Пустое имя клавиши")
    if len(key) == 1:
        return char_keyval(key)
    name = key.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    if name in ("fn", "function"):
        raise InputError("клавиши fn нет на этой клавиатуре")
    if len(name) == 1:
        return char_keyval(name)
    if name not in KEYSYMS:
        raise InputError("Неизвестная клавиша: " + key)
    return KEYSYMS[name]


def limited(item, name, default, lo):
    """Validate finite action parameters without silently clamping them."""
    value = item.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < lo:
        raise InputError("%s должен быть числом не меньше %s" % (name, lo))
    return value


class PortalResponse(RuntimeError):
    """A portal Request finished with a non-zero response code (1 cancelled, 2 other)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def libraries():
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gio, GLib, Gst, Gdk, GdkPixbuf
    GLib.set_prgname("chatrepo")
    GLib.set_application_name("chatrepo")
    Gst.init(None)
    for plugin in ("pipewiresrc", "videoconvert", "videorate", "appsink"):
        if not Gst.ElementFactory.find(plugin):
            raise RuntimeError("Недоступен компонент GStreamer: " + plugin)
    return Gio, GLib, Gst, Gdk, GdkPixbuf


# ---- restore token (pure file helpers) --------------------------------------------------------

def read_token(path):
    """Saved restore_token or None (missing, unreadable, empty or malformed file)."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token if token and len(token) <= 4096 and "\n" not in token else None


def write_token(path, token):
    """Atomic 0600 write: temp file in the same directory, fsync, rename over the old token."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, token.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def delete_token(path):
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def persist_options(remote_version, cast_version, token):
    """(SelectDevices extras, SelectSources extras) as {key: (signature, value)}; see module doc."""
    devices, sources = {}, {}
    if remote_version >= 2:
        target = devices
    elif cast_version >= 4:
        target = sources
    else:
        return devices, sources
    target["persist_mode"] = ("u", 2)
    if token:
        target["restore_token"] = ("s", token)
    return devices, sources


# ---- capture geometry (pure) -------------------------------------------------------------------

def region_plan(logical, stream, region=None, limit=MAX_EDGE):
    """What to crop from a native frame and how big to encode it.

    logical — (width, height) of input coordinates; stream — native frame pixels (may differ on scaled
    outputs). Without region: the whole frame scaled like before (logical size, long edge <= limit).
    With region (logical coords): clamped to the screen, mapped to stream pixels by the ratio, native
    resolution, long edge <= limit. Returns {"crop": (x, y, w, h) stream px, "size": (w, h), "region"}.
    """
    lw, lh = float(logical[0]), float(logical[1])
    sw, sh = int(stream[0]), int(stream[1])
    if lw <= 0 or lh <= 0 or sw <= 0 or sh <= 0:
        raise RuntimeError("Неизвестный размер кадра или экрана")
    if region is None:
        scale = min(1.0, limit / max(lw, lh))
        return {"crop": (0, 0, sw, sh),
                "size": (max(1, int(round(lw * scale))), max(1, int(round(lh * scale)))),
                "region": {"x": 0, "y": 0, "width": int(lw), "height": int(lh)}}
    if not isinstance(region, dict):
        raise InputError("region должен быть объектом {x, y, width, height} в логических координатах")
    try:
        x, y, w, h = (float(region[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        raise InputError("region должен содержать числа x, y, width, height")
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        raise InputError("region: ширина и высота должны быть положительными конечными числами")
    x0, y0 = max(0, int(math.floor(x))), max(0, int(math.floor(y)))
    x1, y1 = min(int(lw), int(math.ceil(x + w))), min(int(lh), int(math.ceil(y + h)))
    if x1 <= x0 or y1 <= y0:
        raise InputError("Область region вне выбранного экрана")
    sx, sy = sw / lw, sh / lh
    px, py = min(sw - 1, int(math.floor(x0 * sx))), min(sh - 1, int(math.floor(y0 * sy)))
    pw = max(1, min(sw - px, int(math.ceil(x1 * sx)) - px))
    ph = max(1, min(sh - py, int(math.ceil(y1 * sy)) - py))
    scale = min(1.0, float(limit) / max(pw, ph))
    return {"crop": (px, py, pw, ph),
            "size": (max(1, int(round(pw * scale))), max(1, int(round(ph * scale)))),
            "region": {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}}


def normalize_stream_layout(raw_streams):
    """Normalize portal stream geometry into one non-negative virtual desktop."""
    parsed = []
    fallback_x = 0
    for node, props in raw_streams:
        logical = props.get("logical_size", props.get("size"))
        if not logical or len(logical) != 2 or min(logical) <= 0:
            raise RuntimeError("Портал не сообщил координатный размер одного из экранов")
        position = props.get("position")
        if not position or len(position) != 2:
            position = (fallback_x, 0)
        fallback_x = max(fallback_x, int(position[0]) + int(logical[0]))
        parsed.append({
            "node": int(node),
            "props": props,
            "logical": (int(logical[0]), int(logical[1])),
            "position": (int(position[0]), int(position[1])),
        })
    if not parsed:
        raise RuntimeError("Портал не вернул ни одного экрана")
    min_x = min(item["position"][0] for item in parsed)
    min_y = min(item["position"][1] for item in parsed)
    max_x = max(item["position"][0] + item["logical"][0] for item in parsed)
    max_y = max(item["position"][1] + item["logical"][1] for item in parsed)
    for item in parsed:
        item["virtual_position"] = (
            item["position"][0] - min_x,
            item["position"][1] - min_y,
        )
    return parsed, (min_x, min_y), (max_x - min_x, max_y - min_y)


# ---- touch gestures (pure) ---------------------------------------------------------------------

def _gesture_number(item, name, default=None):
    value = item.get(name, default)
    if value is None:
        raise InputError("Для жеста нужно числовое поле " + name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise InputError("Поле %s должно быть конечным числом" % name)
    return float(value)


def gesture_strokes(item, logical):
    """Touch gesture → strokes: {"points": [[(x, y) per contact] per step], "step_s", "hold_s", "gap_s"}.

    tap/double_tap/long_press — one contact; swipe — one contact from (x, y) to (toX, toY);
    pinch — two horizontal contacts around (x, y), distance 200 → 200 × scale;
    rotate — two opposite contacts on a radius of 100 around (x, y), turned by `angle` degrees
    (positive = clockwise on screen, y grows downwards).
    """
    gesture = item.get("gesture")
    x, y = _gesture_number(item, "x"), _gesture_number(item, "y")
    duration = item.get("duration_ms")
    if duration is not None:
        duration = limited(item, "duration_ms", 0, 0)

    def stroke(points, total_ms, hold_ms=20.0, gap_ms=0.0):
        steps = max(1, len(points) - 1)
        return {"points": points, "step_s": total_ms / 1000.0 / steps if len(points) > 1 else 0.0,
                "hold_s": hold_ms / 1000.0, "gap_s": gap_ms / 1000.0}

    if gesture == "tap":
        strokes = [stroke([[(x, y)]], 0, duration if duration is not None else 50)]
    elif gesture == "double_tap":
        strokes = [stroke([[(x, y)]], 0, 50, 100), stroke([[(x, y)]], 0, 50)]
    elif gesture == "long_press":
        strokes = [stroke([[(x, y)]], 0, duration if duration is not None else 800)]
    elif gesture == "swipe":
        tx, ty = _gesture_number(item, "toX"), _gesture_number(item, "toY")
        points = [[(x + (tx - x) * i / GESTURE_STEPS, y + (ty - y) * i / GESTURE_STEPS)]
                  for i in range(GESTURE_STEPS + 1)]
        strokes = [stroke(points, duration if duration is not None else 300)]
    elif gesture == "pinch":
        scale = _gesture_number(item, "scale", 0.5)
        if scale <= 0:
            raise InputError("scale для pinch должен быть положительным (<1 — свести пальцы)")
        points = []
        for i in range(GESTURE_STEPS + 1):
            half = (PINCH_DISTANCE + (PINCH_DISTANCE * scale - PINCH_DISTANCE) * i / GESTURE_STEPS) / 2
            points.append([(x - half, y), (x + half, y)])
        strokes = [stroke(points, duration if duration is not None else 400)]
    elif gesture == "rotate":
        angle = math.radians(_gesture_number(item, "angle", 45))
        points = []
        for i in range(GESTURE_STEPS + 1):
            a = angle * i / GESTURE_STEPS
            dx, dy = ROTATE_RADIUS * math.cos(a), ROTATE_RADIUS * math.sin(a)
            points.append([(x + dx, y + dy), (x - dx, y - dy)])
        strokes = [stroke(points, duration if duration is not None else 400)]
    else:
        raise InputError("Неизвестный жест: %s; допустимы tap, double_tap, long_press, swipe, pinch, rotate" % gesture)
    width, height = logical
    for s in strokes:
        for step in s["points"]:
            for px, py in step:
                if not (0 <= px < width and 0 <= py < height):
                    raise InputError("Жест выходит за пределы выбранного экрана")
    return strokes


class Portal:
    def __init__(self, token_file=None):
        self.Gio, self.GLib, self.Gst, self.Gdk, self.GdkPixbuf = libraries()
        self.bus = self.Gio.bus_get_sync(self.Gio.BusType.SESSION, None)
        self.token_file = token_file
        self.session = None
        self.closed_subscription = None
        self.pipelines = []
        self.streams = []
        self.logical = (0, 0)
        self.desktop_origin = (0, 0)
        self.fd = None
        self.closed = False
        self.started = False
        self.held = set()
        self.buttons = set()
        self.touches = set()
        self.key_mods = {}     # keysym held by key_down → modifiers that key_down pressed
        self.button_mods = {}  # button held by mouse_down → modifiers that mouse_down pressed
        self.request_loop = None
        self.versions = {REMOTE: 1, CAST: 1}
        self.available_devices = KEYBOARD_POINTER
        self.devices = 0

    def get_property(self, interface, prop):
        return self.bus.call_sync(BUS, PATH, "org.freedesktop.DBus.Properties", "Get",
            self.GLib.Variant("(ss)", (interface, prop)), None,
            self.Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]

    def probe(self):
        devices = self.get_property(REMOTE, "AvailableDeviceTypes")
        if devices & 3 != 3:
            raise RuntimeError("Портал не предоставляет клавиатуру и указатель")
        if not self.get_property(CAST, "AvailableSourceTypes") & 1:
            raise RuntimeError("Портал не предоставляет захват монитора")
        self.available_devices = devices
        for interface in (REMOTE, CAST):
            try:
                self.versions[interface] = int(self.get_property(interface, "version"))
            except Exception:
                self.versions[interface] = 1
        return {"supported": True, "backend": "xdg-desktop-portal", "session_type": "wayland",
                "touch": bool(devices & TOUCHSCREEN)}

    def request(self, interface, method, signature, args, options):
        token = "chatrepo_" + uuid.uuid4().hex
        options = dict(options, handle_token=self.GLib.Variant("s", token))
        path = "/org/freedesktop/portal/desktop/request/" + self.bus.get_unique_name()[1:].replace(".", "_") + "/" + token
        result = []
        loop = self.GLib.MainLoop()
        self.request_loop = loop

        def response(_bus, _sender, _path, _iface, _signal, parameters, *_unused):
            result.append(parameters.unpack())
            loop.quit()

        def timeout():
            result.append((2, {}))
            loop.quit()
            return False

        subscription = self.bus.signal_subscribe(BUS, "org.freedesktop.portal.Request", "Response",
            path, None, self.Gio.DBusSignalFlags.NONE, response)
        timer = self.GLib.timeout_add_seconds(120, timeout)
        try:
            self.bus.call_sync(BUS, PATH, interface, method,
                self.GLib.Variant(signature, (*args, options)), None,
                self.Gio.DBusCallFlags.NONE, 10000, None)
            if not result and not self.closed:
                loop.run()
            if not result or self.closed:
                raise RuntimeError("Сессия общего доступа закрыта")
            code, values = result[0]
            if code != 0:
                raise PortalResponse(code, "Доступ к экрану/управлению не предоставлен или запрос отменён. Автоматического повторного запроса не будет.")
            return values
        finally:
            self.request_loop = None
            if self.GLib.MainContext.default().find_source_by_id(timer):
                self.GLib.source_remove(timer)
            self.bus.signal_unsubscribe(subscription)

    def start(self):
        if self.started:
            if self.closed:
                raise RuntimeError("Общий доступ остановлен пользователем; сессия закрыта")
            return
        # A denied/failed attempt is never silently retried by the same run.
        self.started = True
        self.probe()
        token = read_token(self.token_file)
        try:
            self.open_session(token)
        except PortalResponse as error:
            if not token:
                raise
            # The saved permission did not work (revoked, other monitor, portal restarted): forget it.
            delete_token(self.token_file)
            if error.code == 1:
                raise  # the person cancelled the dialog — never ask again
            self.drop_session()
            self.open_session(None)  # exactly one normal consent dialog; a second failure propagates

    def open_session(self, token):
        GLib = self.GLib
        self.session = self.request(REMOTE, "CreateSession", "(a{sv})", (),
            {"session_handle_token": GLib.Variant("s", "chatrepo_" + uuid.uuid4().hex)})["session_handle"]
        self.closed_subscription = self.bus.signal_subscribe(BUS, "org.freedesktop.portal.Session", "Closed",
            self.session, None, self.Gio.DBusSignalFlags.NONE, lambda *_: self.on_closed())
        extra_devices, extra_sources = persist_options(self.versions[REMOTE], self.versions[CAST], token) \
            if self.token_file else ({}, {})
        wanted = KEYBOARD_POINTER | (self.available_devices & TOUCHSCREEN)
        options = {"types": GLib.Variant("u", wanted)}
        options.update({key: GLib.Variant(sig, value) for key, (sig, value) in extra_devices.items()})
        self.request(REMOTE, "SelectDevices", "(oa{sv})", (self.session,), options)
        # Ask the compositor for every monitor the person approves. On portals that
        # support persistence, the restore token remembers that exact multi-monitor choice.
        options = {"types": GLib.Variant("u", 1), "multiple": GLib.Variant("b", True)}
        options.update({key: GLib.Variant(sig, value) for key, (sig, value) in extra_sources.items()})
        self.request(CAST, "SelectSources", "(oa{sv})", (self.session,), options)
        result = self.request(REMOTE, "Start", "(osa{sv})", (self.session, ""), {})
        self.devices = int(result.get("devices", 0))
        raw_streams = list(result.get("streams", []))
        if self.devices & 3 != 3 or not raw_streams:
            raise RuntimeError("Для управления нужны хотя бы один экран, клавиатура и указатель")
        if self.token_file:
            fresh = result.get("restore_token")
            try:
                if fresh:
                    write_token(self.token_file, fresh)
                else:
                    delete_token(self.token_file)  # tokens are single-use; the old one is spent
            except OSError:
                pass  # failing to remember permission must not break an approved session

        # Normalize portal monitor geometry into one virtual logical desktop. ScreenCast
        # normally supplies position; old portals may omit it, so fall back to a
        # deterministic left-to-right layout rather than inventing overlapping monitors.
        parsed, self.desktop_origin, self.logical = normalize_stream_layout(raw_streams)

        result, fds = self.bus.call_with_unix_fd_list_sync(BUS, PATH, CAST, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session, {})), None,
            self.Gio.DBusCallFlags.NONE, 10000, None, None)
        self.fd = fds.get(result.unpack()[0])
        self.streams = []
        self.pipelines = []
        for index, item in enumerate(parsed):
            stream_fd = os.dup(self.fd)
            sink_name = "frame%d" % index
            pipeline = self.Gst.parse_launch(
                f"pipewiresrc fd={stream_fd} path={item['node']} do-timestamp=true ! "
                "videorate drop-only=true ! video/x-raw,framerate=5/1 ! videoconvert ! "
                f"video/x-raw,format=RGB ! appsink name={sink_name} sync=false max-buffers=1 drop=true")
            if pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
                try:
                    os.close(stream_fd)
                except OSError:
                    pass
                raise RuntimeError("Не удалось открыть один из PipeWire-видеопотоков")
            item["pipeline"] = pipeline
            item["sink"] = pipeline.get_by_name(sink_name)
            item["fd"] = stream_fd
            self.streams.append(item)
            self.pipelines.append(pipeline)

    def drop_session(self):
        """Close a half-open session before the single retry, without marking the run closed."""
        if self.closed_subscription is not None:
            self.bus.signal_unsubscribe(self.closed_subscription)
            self.closed_subscription = None
        if self.session:
            try:
                self.bus.call_sync(BUS, self.session, "org.freedesktop.portal.Session", "Close", None,
                    None, self.Gio.DBusCallFlags.NONE, 1000, None)
            except Exception:
                pass
            self.session = None

    def stop_pipelines(self):
        for stream in self.streams:
            pipeline = stream.get("pipeline")
            if pipeline:
                pipeline.set_state(self.Gst.State.NULL)
            fd = stream.get("fd")
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            stream["pipeline"] = None
            stream["sink"] = None
            stream["fd"] = None
        self.pipelines = []

    def on_closed(self):
        self.closed = True
        if self.request_loop:
            self.request_loop.quit()
        self.stop_pipelines()

    def pull_stream_pixbuf(self, stream):
        sample = stream["sink"].emit("try-pull-sample", 5 * self.Gst.SECOND)
        if self.closed or sample is None:
            raise RuntimeError("Нет нового кадра PipeWire или пользователь остановил общий доступ")
        structure = sample.get_caps().get_structure(0)
        width, height = int(structure.get_value("width")), int(structure.get_value("height"))
        buffer = sample.get_buffer()
        data = buffer.extract_dup(0, buffer.get_size())
        del sample, buffer
        stride = len(data) // height if height else 0
        if width <= 0 or height <= 0 or stride < width * 3:
            raise RuntimeError("Кадр PipeWire неожиданного размера")
        pixbuf = self.GdkPixbuf.Pixbuf.new_from_bytes(
            self.GLib.Bytes.new(data), self.GdkPixbuf.Colorspace.RGB, False, 8,
            width, height, stride)
        return pixbuf, width, height

    def capture(self, args=None):
        region = (args or {}).get("region")
        self.start()
        frames = []
        scale = 1.0
        for stream in self.streams:
            pixbuf, width, height = self.pull_stream_pixbuf(stream)
            logical_width, logical_height = stream["logical"]
            scale = max(scale, width / logical_width, height / logical_height)
            frames.append((stream, pixbuf, width, height))

        # Build one virtual desktop image. A single uniform scale keeps model-pixel
        # coordinates reversible even when monitors use different physical DPI.
        canvas_width = max(1, int(round(self.logical[0] * scale)))
        canvas_height = max(1, int(round(self.logical[1] * scale)))
        P = self.GdkPixbuf
        canvas = P.Pixbuf.new(P.Colorspace.RGB, False, 8, canvas_width, canvas_height)
        canvas.fill(0x000000ff)
        monitors = []
        for stream, pixbuf, native_width, native_height in frames:
            logical_width, logical_height = stream["logical"]
            virtual_x, virtual_y = stream["virtual_position"]
            target_width = max(1, int(round(logical_width * scale)))
            target_height = max(1, int(round(logical_height * scale)))
            target_x = int(round(virtual_x * scale))
            target_y = int(round(virtual_y * scale))
            if (pixbuf.get_width(), pixbuf.get_height()) != (target_width, target_height):
                pixbuf = pixbuf.scale_simple(target_width, target_height, P.InterpType.BILINEAR)
            pixbuf.copy_area(0, 0, target_width, target_height, canvas, target_x, target_y)
            monitors.append({
                "stream_id": stream["node"],
                "x": virtual_x, "y": virtual_y,
                "width": logical_width, "height": logical_height,
                "native_width": native_width, "native_height": native_height,
            })

        plan = region_plan(self.logical, (canvas_width, canvas_height), region)
        png, out_width, out_height = self.render_pixbuf(canvas, plan)
        return {"image_b64": base64.b64encode(png).decode("ascii"),
            "logical_width": self.logical[0], "logical_height": self.logical[1],
            "stream_ids": [stream["node"] for stream in self.streams],
            "monitors": monitors, "target": "local-desktop", "backend": "xdg-desktop-portal",
            "region": plan["region"], "image_width": out_width, "image_height": out_height}

    def render_pixbuf(self, pixbuf, plan):
        """Crop/scale a virtual-desktop pixbuf and encode it as PNG."""
        P = self.GdkPixbuf
        width, height = pixbuf.get_width(), pixbuf.get_height()
        x, y, crop_width, crop_height = plan["crop"]
        if (x, y, crop_width, crop_height) != (0, 0, width, height):
            pixbuf = pixbuf.new_subpixbuf(x, y, crop_width, crop_height)
        out_width, out_height = plan["size"]
        if (out_width, out_height) != (crop_width, crop_height):
            pixbuf = pixbuf.scale_simple(out_width, out_height, P.InterpType.BILINEAR)
        ok, png = pixbuf.save_to_bufferv("png", [], [])
        if not ok:
            raise RuntimeError("Не удалось закодировать кадр в PNG")
        return bytes(png), out_width, out_height

    def notify(self, method, signature, values, options=None):
        if self.closed or not self.session:
            raise RuntimeError("Сессия управления экраном закрыта")
        self.bus.call_sync(BUS, PATH, REMOTE, method,
            self.GLib.Variant(signature, (self.session, options or {}, *values)),
            None, self.Gio.DBusCallFlags.NONE, 5000, None)

    def keyval(self, key):
        return keyval(key)

    def release(self, recorded, explicit):
        """Release what a *_down pressed plus explicitly listed modifiers that are still held."""
        for mod in reversed(recorded + [m for m in explicit if m not in recorded]):
            if mod in self.held:
                self.key(mod, False)

    @staticmethod
    def remember(table, item, pressed):
        record = table.setdefault(item, [])
        record.extend(mod for mod in pressed if mod not in record)

    def key(self, value, pressed):
        # Mutter resolves keysyms in the active layout: Latin shortcut keysyms can be
        # silently ignored in e.g. Russian. Accelerators use physical evdev keys,
        # as on the user's keyboard; ordinary key presses retain keysym semantics.
        letters = dict(zip("qwertyuiopasdfghjklzxcvbnm",
            [16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 30, 31, 32, 33, 34, 35, 36, 37, 38, 44, 45, 46, 47, 48, 49, 50]))
        shortcut = bool(self.held & {0xffe3, 0xffe4, 0xffe9, 0xffea, 0xffeb, 0xffec})
        code = letters.get(chr(value).lower()) if 65 <= value <= 122 and shortcut else None
        method = "NotifyKeyboardKeycode" if code is not None else "NotifyKeyboardKeysym"
        self.notify(method, "(oa{sv}iu)", (code if code is not None else value, int(pressed)))
        (self.held.add if pressed else self.held.discard)(value)

    def button(self, value, pressed):
        self.notify("NotifyPointerButton", "(oa{sv}iu)", (value, int(pressed)))
        (self.buttons.add if pressed else self.buttons.discard)(value)

    def stream_for_point(self, x, y):
        if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < self.logical[0] and 0 <= y < self.logical[1]):
            raise InputError("Координаты вне выбранного виртуального рабочего стола")
        for stream in self.streams:
            sx, sy = stream["virtual_position"]
            width, height = stream["logical"]
            if sx <= x < sx + width and sy <= y < sy + height:
                return stream, float(x - sx), float(y - sy)
        raise InputError("Координаты попали в промежуток между мониторами")

    def move(self, x, y):
        stream, local_x, local_y = self.stream_for_point(x, y)
        self.notify("NotifyPointerMotionAbsolute", "(oa{sv}udd)",
                    (stream["node"], local_x, local_y))

    def touch_point(self, method, slot, x, y):
        stream, local_x, local_y = self.stream_for_point(x, y)
        self.notify(method, "(oa{sv}uudd)",
                    (stream["node"], slot, local_x, local_y))

    def touch_up(self, slot):
        self.touches.discard(slot)
        self.notify("NotifyTouchUp", "(oa{sv}u)", (slot,))

    def touch(self, item):
        if not self.devices & TOUCHSCREEN:
            raise InputError("Портал не выдал сенсорное устройство — жест недоступен; используйте мышь")
        for stroke in gesture_strokes(item, self.logical):
            first = stroke["points"][0]
            try:
                for slot, (x, y) in enumerate(first):
                    self.touch_point("NotifyTouchDown", slot, x, y)
                    self.touches.add(slot)
                for step in stroke["points"][1:]:
                    time.sleep(stroke["step_s"])
                    for slot, (x, y) in enumerate(step):
                        self.touch_point("NotifyTouchMotion", slot, x, y)
                if stroke["hold_s"]:
                    time.sleep(stroke["hold_s"])
            finally:
                for slot in sorted(self.touches):
                    self.touch_up(slot)
            if stroke["gap_s"]:
                time.sleep(stroke["gap_s"])

    def action(self, item):
        if not self.started or self.closed or not self.streams:
            raise RuntimeError("Перед вводом получите снимок выбранного экрана")
        kind = item["action"]
        if kind not in ("click", "move", "drag", "scroll", "type", "key", "key_down", "key_up", "mouse_down",
                        "mouse_up", "touch"):
            raise InputError("Неизвестное действие: " + kind)
        if kind == "touch":
            self.touch(item)
            return {"backend": "xdg-desktop-portal", "emulated": False}
        # evdev: BTN_LEFT/RIGHT/MIDDLE и боковые BTN_SIDE («назад») / BTN_EXTRA («вперёд»).
        buttons = {"left": 272, "right": 273, "middle": 274, "back": 275, "forward": 276}
        if "button" in item and item["button"] not in buttons:
            raise InputError("Неизвестная кнопка мыши: " + str(item["button"]))
        modifiers = [self.keyval(x) for x in item.get("modifiers", [])]
        if kind in ("mouse_down", "mouse_up"):
            button = buttons[item.get("button", "left")]
            if kind == "mouse_up":
                try:
                    self.move(item["x"], item["y"])
                    self.button(button, False)
                finally:
                    self.release(self.button_mods.pop(button, []), modifiers)
                return {"backend": "xdg-desktop-portal"}
            acquired = [mod for mod in modifiers if mod not in self.held]
            try:
                for mod in acquired:
                    self.key(mod, True)
                self.move(item["x"], item["y"])
                self.button(button, True)
            except BaseException:
                self.release(acquired, [])
                raise
            self.remember(self.button_mods, button, acquired)  # held until the paired mouse_up
            return {"backend": "xdg-desktop-portal"}
        key = self.keyval(item["key"]) if kind.startswith("key") else None
        if kind == "key_up":
            try:
                self.key(key, False)
            finally:
                self.release(self.key_mods.pop(key, []), modifiers)
            return {}
        clicks = int(limited(item, "clicks", 1, 1)) if kind == "click" else 1
        hold = limited(item, "hold_ms", 0, 0) / 1000 if kind == "click" else 0
        steps = int(limited(item, "steps", 24, 2)) if kind == "drag" else 1
        pause = limited(item, "duration_ms", 400, 0) / 1000 / steps if kind == "drag" else 0
        acquired = [mod for mod in modifiers if mod not in self.held]
        kept = False
        try:
            for mod in acquired:
                self.key(mod, True)
            if kind in ("click", "move", "drag", "scroll"):
                self.move(item["x"], item["y"])
            if kind == "click":
                button = buttons[item.get("button", "left")]
                for _ in range(clicks):
                    self.button(button, True)
                    if hold:
                        time.sleep(hold)
                    self.button(button, False)
                    time.sleep(0.04)
            elif kind == "drag":
                button = buttons[item.get("button", "left")]
                self.button(button, True)
                try:
                    for step in range(1, steps + 1):
                        self.move(item["x"] + (item["toX"] - item["x"]) * step / steps,
                            item["y"] + (item["toY"] - item["y"]) * step / steps)
                        time.sleep(pause)
                finally:
                    self.button(button, False)
            elif kind == "scroll" and item.get("unit") == "line":
                # Дискретные «щелчки» колеса: ось 0 — вертикаль, 1 — горизонталь.
                for axis, steps in ((0, int(item["deltaY"])), (1, int(item["deltaX"]))):
                    if steps:
                        self.notify("NotifyPointerAxisDiscrete", "(oa{sv}ui)", (axis, steps))
            elif kind == "scroll":
                self.notify("NotifyPointerAxis", "(oa{sv}dd)", (float(item["deltaX"]), float(item["deltaY"])),
                    {"finish": self.GLib.Variant("b", True)})
            elif kind == "type":
                for char in item["text"]:
                    value = char_keyval(char)
                    self.key(value, True)
                    self.key(value, False)
            elif kind == "key_down":
                self.key(key, True)
                self.remember(self.key_mods, key, acquired)  # released by the paired key_up
                kept = True
            elif kind == "key":
                for _ in range(item.get("repeat", 1)):
                    self.key(key, True)
                    self.key(key, False)
            elif kind != "move":
                raise RuntimeError("Неизвестное действие: " + kind)
        finally:
            if not kept:
                self.release(acquired, [])
        return {"backend": "xdg-desktop-portal"}

    def close(self):
        try:
            for slot in sorted(self.touches):
                self.touch_up(slot)
            for key in list(self.held):
                self.key(key, False)
            for button in list(self.buttons):
                self.button(button, False)
        except Exception:
            pass
        self.on_closed()
        if self.session:
            try:
                self.bus.call_sync(BUS, self.session, "org.freedesktop.portal.Session", "Close", None,
                    None, self.Gio.DBusCallFlags.NONE, 1000, None)
            except Exception:
                pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def option(name, argv=None):
    argv = sys.argv if argv is None else argv
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return None


def main():
    portal = Portal(token_file=option("--token-file"))
    if "--probe" in sys.argv:
        print(json.dumps(portal.probe()), flush=True)
        return
    loop = portal.GLib.MainLoop()
    failed = None

    def stop():
        portal.close()
        loop.quit()
        return False

    portal.GLib.unix_signal_add(portal.GLib.PRIORITY_HIGH, signal.SIGTERM, stop)
    portal.GLib.unix_signal_add(portal.GLib.PRIORITY_HIGH, signal.SIGINT, stop)

    def dispatch(line):
        nonlocal failed
        request = {}
        try:
            request = json.loads(line)
            if failed:
                raise RuntimeError(failed)
            params = request.get("params") or {}
            if request["method"] == "probe":
                result = portal.probe()
            elif request["method"] == "capture":
                result = portal.capture(params)
            elif request["method"] == "action":
                result = portal.action(params)
            else:
                raise InputError("Неизвестный метод портала: " + str(request["method"]))
            reply = {"id": request["id"], "result": result}
        except InputError as error:
            reply = {"id": request.get("id"), "error": str(error), "fatal": False}
        except Exception as error:
            failed = str(error)
            portal.close()
            reply = {"id": request.get("id"), "error": failed, "fatal": True}
        print(json.dumps(reply, ensure_ascii=False), flush=True)
        return False

    def read_commands():
        for line in sys.stdin:
            portal.GLib.idle_add(dispatch, line)
        portal.GLib.idle_add(stop)

    threading.Thread(target=read_commands, daemon=True).start()
    try:
        loop.run()
    finally:
        portal.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": "Wayland: " + str(error)}, ensure_ascii=False), flush=True)
        sys.exit(1)
