# chatrepo-computer (Windows): долгоживущий помощник Computer Use.
# Протокол: одна JSON-строка на запрос в stdin, одна на ответ в stdout; клиент — src/host/tools/computer/computer-driver.ts.
#
# Запуск:
#   powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command
#     "& ([ScriptBlock]::Create([IO.File]::ReadAllText('<path>', [Text.Encoding]::UTF8)))"
#
# ⚠⚠ ВСЯ ЛОГИКА — В ОДНОМ КЛАССЕ C#, КОМПИЛИРУЕМОМ ОДИН РАЗ. Прежде на КАЖДОЕ действие запускался
# новый powershell.exe и заново компилировал Add-Type: 0,5–1,5 с на клик. Здесь компиляция одна при
# старте, дальше каждое действие — вызов метода.
#
# ⚠⚠ Add-Type в Windows PowerShell 5.1 компилирует СТАРЫМ компилятором CodeDOM — это C# 5. Никаких
# $"…", ?., nameof, выражений-членов (=>) у методов и свойств, инициализаторов автосвойств, out var,
# кортежей и сопоставления с образцом. Автоматической проверки этого нет: нарушение видно только при запуске на Windows.

$ErrorActionPreference = 'Stop'
try { [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch {}

$driverSource = @'
using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using UiaText = System.Windows.Automation.Text;

/// <summary>Ошибка протокола: код и русский текст для агента.</summary>
public class ChatRepoDriverError : Exception {
  public readonly string Code;
  public ChatRepoDriverError(string code, string message) : base(message) { Code = code; }
}

public static class ChatRepoDriver {
  // ───────────────────────────── Win32 ─────────────────────────────

  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X; public int Y; }
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct HARDWAREINPUT { public uint uMsg; public ushort wParamL; public ushort wParamH; }
  // ⚠ ОБЪЕДИНЕНИЕ ЯВНОЕ, КАК В WinUser.h: размер INPUT обязан совпасть с sizeof(INPUT) (40 байт на x64,
  // 28 на x86), иначе SendInput отвергает весь массив с ERROR_INVALID_PARAMETER.
  [StructLayout(LayoutKind.Explicit)] public struct InputUnion {
    [FieldOffset(0)] public MOUSEINPUT mi;
    [FieldOffset(0)] public KEYBDINPUT ki;
    [FieldOffset(0)] public HARDWAREINPUT hi;
  }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public InputUnion U; }

  [StructLayout(LayoutKind.Sequential)] public struct GUITHREADINFO {
    public int cbSize; public uint flags; public IntPtr hwndActive; public IntPtr hwndFocus;
    public IntPtr hwndCapture; public IntPtr hwndMenuOwner; public IntPtr hwndMoveSize; public IntPtr hwndCaret; public RECT rcCaret;
  }

  // ⚠ РАСКЛАДКА СТРУКТУР ДЛЯ x64. POINTER_INFO содержит четыре POINT (по 8 байт) и два указателя
  // (sourceDevice, hwndTarget — по 8 байт на x64), PerformanceCount — UINT64. Sequential + упаковка
  // по умолчанию (8 на x64) выравнивает всё как в WinUser.h; менять Pack нельзя.
  [StructLayout(LayoutKind.Sequential)] public struct POINTER_INFO {
    public uint pointerType; public uint pointerId; public uint frameId; public uint pointerFlags;
    public IntPtr sourceDevice; public IntPtr hwndTarget;
    public POINT ptPixelLocation; public POINT ptHimetricLocation; public POINT ptPixelLocationRaw; public POINT ptHimetricLocationRaw;
    public uint dwTime; public uint historyCount; public int inputData; public uint dwKeyStates; public ulong PerformanceCount; public int ButtonChangeType;
  }
  [StructLayout(LayoutKind.Sequential)] public struct POINTER_TOUCH_INFO {
    public POINTER_INFO pointerInfo; public uint touchFlags; public uint touchMask; public RECT rcContact; public RECT rcContactRaw; public uint orientation; public uint pressure;
  }

  public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

  [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
  [DllImport("user32.dll", SetLastError = true)] static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll", SetLastError = true)] static extern bool GetCursorPos(out POINT point);
  [DllImport("user32.dll")] static extern IntPtr MonitorFromPoint(POINT point, uint flags);
  [DllImport("user32.dll", SetLastError = true)] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
  [DllImport("user32.dll")] static extern IntPtr GetThreadDpiAwarenessContext();
  [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
  [DllImport("user32.dll")] static extern bool AreDpiAwarenessContextsEqual(IntPtr a, IntPtr b);
  [DllImport("shcore.dll")] static extern int SetProcessDpiAwareness(int value);
  [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern short VkKeyScanExW(char ch, IntPtr hkl);
  [DllImport("user32.dll")] static extern IntPtr GetKeyboardLayout(uint threadId);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern uint MapVirtualKeyW(uint code, uint mapType);
  [DllImport("user32.dll")] static extern int GetSystemMetrics(int index);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool IsZoomed(IntPtr hwnd);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextLengthW(IntPtr hwnd);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowTextW(IntPtr hwnd, StringBuilder text, int maxCount);
  [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] static extern int GetWindowLong(IntPtr hwnd, int index);
  [DllImport("user32.dll")] static extern IntPtr GetAncestor(IntPtr hwnd, uint flags);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool ShowWindowAsync(IntPtr hwnd, int command);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint attach, uint attachTo, bool doAttach);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  [DllImport("user32.dll", SetLastError = true)] static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);
  [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] static extern int DwmGetInt(IntPtr hwnd, int attribute, out int value, int size);
  [DllImport("dwmapi.dll", EntryPoint = "DwmGetWindowAttribute")] static extern int DwmGetRect(IntPtr hwnd, int attribute, out RECT value, int size);
  // v1.1
  [DllImport("user32.dll")] static extern IntPtr WindowFromPoint(POINT point);
  [DllImport("user32.dll")] static extern IntPtr ChildWindowFromPointEx(IntPtr parent, POINT point, uint flags);
  [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr hwnd, ref POINT point);
  [DllImport("user32.dll")] static extern bool GetGUIThreadInfo(uint threadId, ref GUITHREADINFO info);
  [DllImport("user32.dll", SetLastError = true)] static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam);
  [DllImport("user32.dll", SetLastError = true)] static extern bool InitializeTouchInjection(uint maxCount, uint dwMode);
  [DllImport("user32.dll", SetLastError = true)] static extern bool InjectTouchInput(uint count, POINTER_TOUCH_INFO[] contacts);

  const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
  const uint MOUSEEVENTF_MOVE = 0x0001, MOUSEEVENTF_ABSOLUTE = 0x8000, MOUSEEVENTF_VIRTUALDESK = 0x4000;
  const uint LEFTDOWN = 0x0002, LEFTUP = 0x0004, RIGHTDOWN = 0x0008, RIGHTUP = 0x0010;
  const uint MIDDLEDOWN = 0x0020, MIDDLEUP = 0x0040, XDOWN = 0x0080, XUP = 0x0100;
  const uint WHEEL = 0x0800, HWHEEL = 0x1000;
  const int XBUTTON1 = 1, XBUTTON2 = 2;
  const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004, KEYEVENTF_EXTENDEDKEY = 0x0001;
  const int SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77, SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79;
  const int GWL_EXSTYLE = -20;
  const int WS_EX_TOOLWINDOW = 0x00000080, WS_EX_APPWINDOW = 0x00040000;
  const uint GA_ROOT = 2, GA_ROOTOWNER = 3;
  const int DWMWA_EXTENDED_FRAME_BOUNDS = 9, DWMWA_CLOAKED = 14;
  const int SW_MAXIMIZE = 3, SW_MINIMIZE = 6, SW_RESTORE = 9;
  const uint WM_CLOSE = 0x0010;
  const uint SWP_NOZORDER = 0x0004, SWP_NOACTIVATE = 0x0010, SWP_ASYNCWINDOWPOS = 0x4000;
  const ushort VK_RETURN = 0x0D, VK_TAB = 0x09, VK_MENU = 0x12;
  // v1.1
  const uint WM_CHAR = 0x0102, WM_KEYDOWN = 0x0100, WM_KEYUP = 0x0101;
  const uint WM_LBUTTONDOWN = 0x0201, WM_LBUTTONUP = 0x0202, WM_LBUTTONDBLCLK = 0x0203;
  const uint WM_RBUTTONDOWN = 0x0204, WM_RBUTTONUP = 0x0205, WM_RBUTTONDBLCLK = 0x0206;
  const uint WM_MBUTTONDOWN = 0x0207, WM_MBUTTONUP = 0x0208, WM_MBUTTONDBLCLK = 0x0209;
  const int MK_LBUTTON = 0x0001, MK_RBUTTON = 0x0002, MK_MBUTTON = 0x0010;
  const uint CWP_SKIPINVISIBLE = 0x0001, CWP_SKIPTRANSPARENT = 0x0004;
  const uint PT_TOUCH = 2;
  const uint TOUCH_FEEDBACK_DEFAULT = 1;
  const uint POINTER_FLAG_INRANGE = 0x00000002, POINTER_FLAG_INCONTACT = 0x00000004;
  const uint POINTER_FLAG_DOWN = 0x00010000, POINTER_FLAG_UPDATE = 0x00020000, POINTER_FLAG_UP = 0x00040000;
  const uint TOUCH_MASK_CONTACTAREA = 0x00000001, TOUCH_MASK_ORIENTATION = 0x00000002, TOUCH_MASK_PRESSURE = 0x00000004;

  static readonly int InputSize = Marshal.SizeOf(typeof(INPUT));
  static readonly JavaScriptSerializer Json = CreateSerializer();
  static readonly object HeldGate = new object();
  static string dpiMode = "не установлен";
  static bool initialized;

  static JavaScriptSerializer CreateSerializer() {
    JavaScriptSerializer serializer = new JavaScriptSerializer();
    serializer.MaxJsonLength = int.MaxValue;
    serializer.RecursionLimit = 64;
    return serializer;
  }

  // ───────────────────────────── Запуск и транспорт ─────────────────────────────

  /// <summary>
  /// ⚠⚠ ОСВЕДОМЛЁННОСТЬ О DPI — ДО ЛЮБОГО ДРУГОГО ВЫЗОВА. Координаты протокола — физические пиксели
  /// виртуального стола. Процесс без Per-Monitor-V2 получает от Windows виртуализованные координаты
  /// под масштаб основного экрана, и на 125%/150% или на втором мониторе клик уходит мимо, а
  /// BoundingRectangle из UIA приходит в чужих единицах.
  /// </summary>
  public static void Init() {
    if (initialized) return;
    initialized = true;
    IntPtr perMonitorV2 = new IntPtr(-4);
    try {
      if (SetProcessDpiAwarenessContext(perMonitorV2)) dpiMode = "per-monitor-v2";
      else if (AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(), perMonitorV2)) dpiMode = "per-monitor-v2";
    } catch (EntryPointNotFoundException) { }
    // ⚠ Если осведомлённость процесса уже зафиксирована (манифест хоста, прежний вызов), процессный
    // вызов отказывает. Тогда Per-Monitor-V2 ставится на поток: главный — здесь, рабочие UIA — в
    // RunWorker. Все вызовы с координатами идут только из этих потоков.
    if (dpiMode != "per-monitor-v2" && EnsureThreadDpi()) dpiMode = "per-monitor-v2";
    if (dpiMode != "per-monitor-v2") {
      try { if (SetProcessDpiAwareness(2) == 0) dpiMode = "per-monitor"; } catch (Exception) { }
    }
    if (dpiMode == "не установлен") {
      try { if (SetProcessDPIAware()) dpiMode = "system"; } catch (Exception) { }
    }
    // Зажатое помощником не должно пережить помощника: иначе у человека «залипнет» Shift или кнопка мыши.
    AppDomain.CurrentDomain.ProcessExit += new EventHandler(OnProcessExit);
  }

  static void OnProcessExit(object sender, EventArgs args) { ReleaseAll(); }

  /// <summary>Per-Monitor-V2 для текущего потока (Windows 10 1607+); true — поток осведомлён.</summary>
  static bool EnsureThreadDpi() {
    IntPtr perMonitorV2 = new IntPtr(-4);
    try {
      SetThreadDpiAwarenessContext(perMonitorV2);
      return AreDpiAwarenessContextsEqual(GetThreadDpiAwarenessContext(), perMonitorV2);
    } catch (EntryPointNotFoundException) {
      return false;
    }
  }

  // ⚠ СВОИ ПОТОКИ UTF-8, А НЕ [Console]::In/Out. Если Windows не даст сменить кодовую страницу
  // консоли (скрытая консоль, политика), Console.In читает в OEM-866 — и кириллица в `type`
  // превращается в мусор. Поток поверх дескриптора stdin от кодовой страницы консоли не зависит.
  static StreamReader stdin;
  static StreamWriter stdout;

  public static string ReadLine() {
    if (stdin == null) stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false), false, 65536);
    return stdin.ReadLine();
  }

  public static void WriteLine(string line) {
    if (stdout == null) {
      stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false), 65536);
      stdout.NewLine = "\n";
    }
    stdout.WriteLine(line);
    stdout.Flush();
  }

  /// <summary>Один запрос → одна строка ответа. Никогда не бросает исключений.</summary>
  public static string Handle(string line) {
    object id = null;
    try {
      object parsed;
      try { parsed = Json.DeserializeObject(line); }
      catch (Exception) { throw Bad("Запрос не разобран как JSON. Помощник ждёт одну строку {\"id\", \"method\", \"params\"}."); }
      Dictionary<string, object> request = parsed as Dictionary<string, object>;
      if (request == null) throw Bad("Запрос должен быть JSON-объектом {\"id\", \"method\", \"params\"}.");
      object rawId;
      if (request.TryGetValue("id", out rawId)) id = rawId;
      object rawMethod;
      string method = request.TryGetValue("method", out rawMethod) ? rawMethod as string : null;
      if (string.IsNullOrEmpty(method)) throw Bad("В запросе нет строки method.");
      object rawParams;
      Dictionary<string, object> p = null;
      if (request.TryGetValue("params", out rawParams) && rawParams != null) {
        p = rawParams as Dictionary<string, object>;
        if (p == null) throw Bad("params должен быть JSON-объектом.");
      }
      if (p == null) p = new Dictionary<string, object>();
      if (!initialized) Init();
      // ⚠ OCR ЖИВЁТ В POWERSHELL. Windows.Media.Ocr — это WinRT, а метаданные WinRT из легаси-компилятора
      // CodeDOM (C# 5) не подключить. Handle разбирает и проверяет запрос, складывает параметры и отдаёт
      // маркер; цикл PowerShell видит маркер и выполняет распознавание, а ответ собирает обратно через
      // OcrFinish/OcrFail — сериализация остаётся в JavaScriptSerializer с тем же id.
      if (method == "ocr") return PrepareOcr(id, p);
      return Reply(id, "result", Dispatch(method, p));
    } catch (ChatRepoDriverError error) {
      return Reply(id, "error", ErrorBody(error.Code, error.Message));
    } catch (ElementNotAvailableException) {
      return Reply(id, "error", ErrorBody("not_found", "Элемент интерфейса исчез (окно закрылось или перестроилось). Получите элементы заново."));
    } catch (Exception error) {
      return Reply(id, "error", ErrorBody("failed", "Внутренняя ошибка помощника Windows: " + error.GetType().Name + ": " + error.Message));
    }
  }

  static object Dispatch(string method, Dictionary<string, object> p) {
    switch (method) {
      case "hello": return Hello();
      case "capture": return Capture(p);
      case "input": return Input(p);
      case "cursor": return CursorPoint();
      case "windows": return ListWindows();
      case "window": return WindowAction(p);
      case "launch": return Launch(p);
      case "elements": return Elements(p);
      case "element_action": return ElementAction(p);
      case "focused": return Focused();
      case "clipboard_files": return ClipboardFiles(p);
      case "app_at": return AppAt(p);
      case "background_input": return BackgroundInput(p);
      case "touch": return Touch(p);
      // ⚠ ocr сюда не доходит: его перехватывает Handle до Dispatch и отдаёт в PowerShell (WinRT).
      default: throw Bad("Неизвестный метод «" + method + "». Поддерживаются: hello, capture, input, cursor, windows, window, launch, elements, element_action, focused, ocr, clipboard_files, app_at, background_input, touch.");
    }
  }

  static Dictionary<string, object> ErrorBody(string code, string message) {
    Dictionary<string, object> body = new Dictionary<string, object>();
    body["code"] = code;
    body["message"] = message;
    return body;
  }

  static string Reply(object id, string key, object value) {
    Dictionary<string, object> reply = new Dictionary<string, object>();
    reply["id"] = id;
    reply[key] = value;
    try {
      return Json.Serialize(reply);
    } catch (Exception error) {
      // Последний рубеж: ответ собирается вручную, чтобы клиент не ждал его вечно.
      string idText = "null";
      if (id is int || id is long || id is decimal || id is double) idText = Convert.ToString(id, CultureInfo.InvariantCulture);
      return "{\"id\":" + idText + ",\"error\":{\"code\":\"failed\",\"message\":\"" +
        EscapeJson("Помощник не смог сериализовать ответ: " + error.Message) + "\"}}";
    }
  }

  static string EscapeJson(string text) {
    StringBuilder builder = new StringBuilder();
    foreach (char c in text) {
      if (c == '"' || c == '\\') { builder.Append('\\'); builder.Append(c); }
      else if (c < 0x20) builder.Append("\\u" + ((int)c).ToString("x4"));
      else builder.Append(c);
    }
    return builder.ToString();
  }

  // ───────────────────────────── Разбор параметров ─────────────────────────────

  static ChatRepoDriverError Bad(string message) { return new ChatRepoDriverError("bad_request", message); }

  static object Get(Dictionary<string, object> p, string key) {
    object value;
    return p != null && p.TryGetValue(key, out value) ? value : null;
  }

  static bool ToDouble(object value, out double number) {
    number = 0;
    if (value == null || value is bool) return false;
    if (value is int) { number = (int)value; return true; }
    if (value is long) { number = (long)value; return true; }
    if (value is decimal) { number = (double)(decimal)value; return true; }
    if (value is double) { number = (double)value; return !double.IsNaN(number) && !double.IsInfinity(number); }
    string text = value as string;
    if (text != null && double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number)) {
      return !double.IsNaN(number) && !double.IsInfinity(number);
    }
    return false;
  }

  static int ReqInt(Dictionary<string, object> p, string key) {
    double number;
    if (!ToDouble(Get(p, key), out number)) throw Bad("Поле «" + key + "» обязательно и должно быть числом.");
    if (number < int.MinValue || number > int.MaxValue) throw Bad("Поле «" + key + "» вне диапазона int32.");
    return (int)Math.Round(number);
  }

  static int OptInt(Dictionary<string, object> p, string key, int fallback, int min, int max) {
    object raw = Get(p, key);
    if (raw == null) return fallback;
    double number;
    if (!ToDouble(raw, out number)) throw Bad("Поле «" + key + "» должно быть числом.");
    if (number < min || number > max) throw Bad("Поле «" + key + "» должно быть от " + min + " до " + max + ".");
    return (int)Math.Round(number);
  }

  static string OptString(Dictionary<string, object> p, string key) {
    object raw = Get(p, key);
    if (raw == null) return null;
    string text = raw as string;
    if (text != null) return text;
    if (raw is int || raw is long || raw is decimal || raw is double) return Convert.ToString(raw, CultureInfo.InvariantCulture);
    throw Bad("Поле «" + key + "» должно быть строкой.");
  }

  static List<object> OptList(Dictionary<string, object> p, string key) {
    object raw = Get(p, key);
    List<object> list = new List<object>();
    if (raw == null) return list;
    if (raw is string || !(raw is IEnumerable)) throw Bad("Поле «" + key + "» должно быть массивом.");
    foreach (object one in (IEnumerable)raw) list.Add(one);
    return list;
  }

  static Dictionary<string, object> Point(int x, int y) {
    Dictionary<string, object> point = new Dictionary<string, object>();
    point["x"] = x;
    point["y"] = y;
    return point;
  }

  static Dictionary<string, object> Bounds(int x, int y, int width, int height) {
    Dictionary<string, object> bounds = new Dictionary<string, object>();
    bounds["x"] = x;
    bounds["y"] = y;
    bounds["width"] = width;
    bounds["height"] = height;
    return bounds;
  }

  // ───────────────────────────── hello ─────────────────────────────

  static object Hello() {
    Dictionary<string, object> capabilities = new Dictionary<string, object>();
    foreach (string name in new string[] { "input", "unicode_type", "cursor", "windows", "window_control", "elements", "launch" }) {
      capabilities[name] = true;
    }
    // v1.1. ocr — по итогу пробы WinRT при старте (см. SetOcrAvailable). clipboard_files/app_at/
    // background_input есть всегда. touch — по версии ОС (InjectTouchInput есть с Windows 8);
    // фактическая проба InitializeTouchInjection ленива, чтобы hello оставался дешёвым.
    capabilities["ocr"] = ocrAvailable;
    capabilities["clipboard_files"] = true;
    capabilities["app_at"] = true;
    capabilities["background_input"] = true;
    capabilities["touch"] = IsTouchInjectionOs();
    capabilities["select_text"] = true;
    capabilities["capture"] = true;
    Dictionary<string, object> permissions = new Dictionary<string, object>();
    permissions["accessibility"] = "not_applicable";
    List<object> notes = new List<object>();
    if (!IsElevated()) {
      notes.Add("Помощник работает без прав администратора: Windows (UIPI) не пропускает ввод и чтение элементов " +
        "в окна, запущенные от имени администратора (диспетчер задач, установщики, консоль администратора). " +
        "Такие действия вернут blocked; управлять ими может только человек.");
    }
    if (dpiMode != "per-monitor-v2") {
      notes.Add("Не удалось включить Per-Monitor-V2 DPI (режим: " + dpiMode + "). На экранах с масштабом, " +
        "отличным от основного, координаты могут расходиться; обновите Windows 10 до 1703 или новее.");
    }
    Dictionary<string, object> hello = new Dictionary<string, object>();
    hello["protocol"] = 1;
    hello["platform"] = "win32";
    hello["backend"] = "windows-uia";
    hello["capabilities"] = capabilities;
    hello["permissions"] = permissions;
    hello["notes"] = notes;
    return hello;
  }

  static object Capture(Dictionary<string, object> p) {
    int left = GetSystemMetrics(SM_XVIRTUALSCREEN);
    int top = GetSystemMetrics(SM_YVIRTUALSCREEN);
    int width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    int height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    object rawRegion = Get(p, "region");
    if (rawRegion != null) {
      Dictionary<string, object> region = rawRegion as Dictionary<string, object>;
      if (region == null) throw Bad("region должен быть объектом x/y/width/height");
      double dx, dy, dw, dh;
      if (!ToDouble(Get(region, "x"), out dx) || !ToDouble(Get(region, "y"), out dy) ||
          !ToDouble(Get(region, "width"), out dw) || !ToDouble(Get(region, "height"), out dh)) {
        throw Bad("region должен содержать числовые x/y/width/height");
      }
      int rx = (int)Math.Round(dx), ry = (int)Math.Round(dy);
      int rw = (int)Math.Round(dw), rh = (int)Math.Round(dh);
      if (rw <= 0 || rh <= 0 || rx < left || ry < top || rx + rw > left + width || ry + rh > top + height) {
        throw Bad("region выходит за границы виртуального рабочего стола Windows");
      }
      left = rx; top = ry; width = rw; height = rh;
    }
    using (Bitmap bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb)) {
      using (Graphics graphics = Graphics.FromImage(bitmap)) {
        graphics.CopyFromScreen(left, top, 0, 0, new Size(width, height), CopyPixelOperation.SourceCopy);
      }
      using (MemoryStream stream = new MemoryStream()) {
        bitmap.Save(stream, ImageFormat.Png);
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["image_b64"] = Convert.ToBase64String(stream.ToArray());
        result["mime_type"] = "image/png";
        result["image_width"] = width;
        result["image_height"] = height;
        result["bounds"] = Bounds(left, top, width, height);
        result["backend"] = "windows-uia";
        return result;
      }
    }
  }

  static bool IsElevated() {
    try {
      using (WindowsIdentity identity = WindowsIdentity.GetCurrent()) {
        return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
      }
    } catch (Exception) {
      return false;
    }
  }

  // InitializeTouchInjection / InjectTouchInput появились в Windows 8 (версия 6.2).
  static bool IsTouchInjectionOs() {
    Version version = Environment.OSVersion.Version;
    return version.Major > 6 || (version.Major == 6 && version.Minor >= 2);
  }

  // Ставится из PowerShell при старте: удалось ли поднять WinRT-движок распознавания.
  static bool ocrAvailable;
  public static void SetOcrAvailable(bool value) { ocrAvailable = value; }

  // ───────────────────────────── Ввод: клавиши и кнопки ─────────────────────────────

  sealed class KeySpec {
    public readonly ushort Vk;
    public readonly bool Extended;
    public KeySpec(ushort vk, bool extended) { Vk = vk; Extended = extended; }
    public bool Same(KeySpec other) { return other != null && other.Vk == Vk && other.Extended == Extended; }
  }

  sealed class ButtonSpec {
    public readonly uint Down;
    public readonly uint Up;
    public readonly int Data;
    public readonly string Name;
    public ButtonSpec(uint down, uint up, int data, string name) { Down = down; Up = up; Data = data; Name = name; }
  }

  // Код 0x10D — «Enter цифрового блока»: тот же VK_RETURN, но с флагом KEYEVENTF_EXTENDEDKEY.
  const int NUMPAD_ENTER = 0x10D;
  static readonly Dictionary<string, int> NamedKeys = BuildKeyTable();
  // ⚠ Клавиши расширенного набора обязаны нести флаг KEYEVENTF_EXTENDEDKEY, иначе стрелки, Home/End и Delete
  // приходят как клавиши цифрового блока (при включённом NumLock — цифрами).
  static readonly Dictionary<int, bool> ExtendedKeys = BuildExtendedTable();

  static Dictionary<string, int> BuildKeyTable() {
    Dictionary<string, int> table = new Dictionary<string, int>();
    AddKeys(table, "enter|return", 0x0D);
    AddKeys(table, "tab", 0x09);
    AddKeys(table, "space", 0x20);
    AddKeys(table, "backspace", 0x08);
    // delete на Windows — это Delete (как в прежнем коде), а не Backspace, как на macOS.
    AddKeys(table, "delete|forwarddelete|del", 0x2E);
    AddKeys(table, "escape|esc", 0x1B);
    AddKeys(table, "insert", 0x2D);
    AddKeys(table, "home", 0x24);
    AddKeys(table, "end", 0x23);
    AddKeys(table, "pageup", 0x21);
    AddKeys(table, "pagedown", 0x22);
    AddKeys(table, "left|arrowleft", 0x25);
    AddKeys(table, "up|arrowup", 0x26);
    AddKeys(table, "right|arrowright", 0x27);
    AddKeys(table, "down|arrowdown", 0x28);
    for (int i = 1; i <= 20; i++) table["f" + i] = 0x70 + i - 1;
    AddKeys(table, "capslock", 0x14);
    AddKeys(table, "numlock", 0x90);
    AddKeys(table, "scrolllock", 0x91);
    AddKeys(table, "printscreen", 0x2C);
    AddKeys(table, "pause", 0x13);
    AddKeys(table, "menu|apps", 0x5D);
    AddKeys(table, "shift", 0x10);
    AddKeys(table, "control|ctrl", 0x11);
    AddKeys(table, "option|alt", 0x12);
    AddKeys(table, "command|cmd|meta|win", 0x5B);
    for (int i = 0; i <= 9; i++) table["numpad" + i] = 0x60 + i;
    AddKeys(table, "multiply", 0x6A);
    AddKeys(table, "add", 0x6B);
    AddKeys(table, "subtract", 0x6D);
    AddKeys(table, "decimal", 0x6E);
    AddKeys(table, "divide", 0x6F);
    AddKeys(table, "numpadenter", NUMPAD_ENTER);
    AddKeys(table, "volumeup", 0xAF);
    AddKeys(table, "volumedown", 0xAE);
    AddKeys(table, "volumemute", 0xAD);
    AddKeys(table, "medianext", 0xB0);
    AddKeys(table, "mediaprev", 0xB1);
    AddKeys(table, "mediastop", 0xB2);
    AddKeys(table, "mediaplay", 0xB3);
    AddKeys(table, "browserback", 0xA6);
    AddKeys(table, "browserforward", 0xA7);
    AddKeys(table, "browserrefresh", 0xA8);
    return table;
  }

  static void AddKeys(Dictionary<string, int> table, string names, int vk) {
    foreach (string name in names.Split('|')) table[name] = vk;
  }

  static Dictionary<int, bool> BuildExtendedTable() {
    Dictionary<int, bool> table = new Dictionary<int, bool>();
    foreach (int vk in new int[] { 0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x90, 0x2C, 0x5B, 0x5D, 0x6F, 0xA6, 0xA7, 0xA8 }) {
      table[vk] = true;
    }
    return table;
  }

  // ⚠ РАСКЛАДКА — ТА, ЧТО У ОКНА В ФОКУСЕ. Раскладка своя у каждого потока: долгоживущий помощник
  // застыл бы в той, что была при его запуске, и после переключения человека на русскую Ctrl+C
  // искал бы «c» в чужой раскладке. Поэтому раскладка берётся у потока переднего окна на каждое нажатие.
  static IntPtr TargetLayout() {
    IntPtr foreground = GetForegroundWindow();
    uint pid;
    uint thread = foreground == IntPtr.Zero ? 0 : GetWindowThreadProcessId(foreground, out pid);
    return GetKeyboardLayout(thread);
  }

  /// <summary>
  /// Клавиша по имени протокола или одиночному символу. implied (может быть null) получает модификаторы,
  /// которых символ требует на ТЕКУЩЕЙ раскладке («!» — Shift, «A» — Shift, «@» на немецкой — Ctrl+Alt).
  /// </summary>
  static KeySpec KeyOf(string raw, List<KeySpec> implied) {
    if (raw == null || raw.Length == 0) throw Bad("Клавиша не указана (поле key).");
    string key = raw == " " ? "space" : raw.ToLowerInvariant().Replace(" ", "");
    int code;
    if (NamedKeys.TryGetValue(key, out code)) {
      if (code == NUMPAD_ENTER) return new KeySpec(VK_RETURN, true);
      return new KeySpec((ushort)code, ExtendedKeys.ContainsKey(code));
    }
    // Регистр одиночного символа сохраняется: «A» без модификаторов — это Shift+A (приложение шлёт
    // букву с модификаторами строчной, так что заглавная приходит только когда Shift нужен).
    string single = raw.Replace(" ", "");
    if (single.Length == 1) {
      // ⚠ РАСКЛАДКА УЧИТЫВАЕТСЯ: VkKeyScanEx переводит символ в код ТЕКУЩЕЙ раскладки, поэтому
      // Ctrl+C работает и на русской, а не только на US QWERTY.
      short scan = VkKeyScanExW(single[0], TargetLayout());
      if (scan != -1 && (scan & 0xFF) != 0xFF) {
        // ⚠ СТАРШИЙ БАЙТ — СОСТОЯНИЕ СДВИГА: 1 = Shift, 2 = Ctrl, 4 = Alt. Раньше он отбрасывался,
        // и «!» печатал «1», «?» — «/», «+» — «=». Модификаторы добавляются к явным (без повторов).
        int shiftState = (scan >> 8) & 0xFF;
        if (implied != null) {
          if ((shiftState & 1) != 0) implied.Add(new KeySpec(0x10, false));
          if ((shiftState & 2) != 0) implied.Add(new KeySpec(0x11, false));
          if ((shiftState & 4) != 0) implied.Add(new KeySpec(0x12, false));
        }
        return new KeySpec((ushort)(scan & 0xFF), false);
      }
      throw Bad("Символ «" + raw + "» не набирается в текущей раскладке клавиатуры. Текст вводите действием type; " +
        "для сочетания назовите клавишу символом текущей раскладки или латиницей после переключения раскладки.");
    }
    throw Bad("Неизвестная клавиша «" + raw + "». Допустимы имена из протокола (enter, tab, escape, f1…f20, pageup, " +
      "arrowleft, numpad0…numpad9, volumeup, …) или один печатный символ.");
  }

  static List<KeySpec> ModifiersOf(Dictionary<string, object> p) {
    List<KeySpec> result = new List<KeySpec>();
    foreach (object raw in OptList(p, "modifiers")) {
      string name = raw as string;
      if (name == null) throw Bad("modifiers должен быть массивом строк.");
      KeySpec spec;
      switch (name.Trim().ToLowerInvariant()) {
        case "shift": spec = new KeySpec(0x10, false); break;
        case "control": case "ctrl": spec = new KeySpec(0x11, false); break;
        case "option": case "alt": spec = new KeySpec(0x12, false); break;
        case "command": case "cmd": case "meta": case "win": spec = new KeySpec(0x5B, true); break;
        case "fn": case "function":
          throw new ChatRepoDriverError("unsupported", "Модификатор fn существует только на клавиатурах Mac: Windows его не видит, " +
            "и нажать его программно нельзя. Уберите fn и назовите нужную клавишу напрямую (например, f5 или volumeup).");
        default:
          throw Bad("Неизвестный модификатор «" + name + "». Допустимы: shift, control|ctrl, option|alt, command|cmd|meta|win.");
      }
      bool seen = false;
      foreach (KeySpec one in result) { if (one.Same(spec)) seen = true; }
      if (!seen) result.Add(spec);
    }
    return result;
  }

  static ButtonSpec ButtonOf(Dictionary<string, object> p) {
    string name = OptString(p, "button");
    switch (name == null ? "left" : name.Trim().ToLowerInvariant()) {
      case "left": return new ButtonSpec(LEFTDOWN, LEFTUP, 0, "left");
      case "right": return new ButtonSpec(RIGHTDOWN, RIGHTUP, 0, "right");
      case "middle": return new ButtonSpec(MIDDLEDOWN, MIDDLEUP, 0, "middle");
      // Боковые кнопки мыши: XBUTTON1 — «назад», XBUTTON2 — «вперёд» (номер кнопки идёт в mouseData).
      case "back": return new ButtonSpec(XDOWN, XUP, XBUTTON1, "back");
      case "forward": return new ButtonSpec(XDOWN, XUP, XBUTTON2, "forward");
      default: throw Bad("Неизвестная кнопка мыши «" + name + "». Допустимы: left, right, middle, back, forward.");
    }
  }

  // ───────────────────────────── Ввод: события ─────────────────────────────

  static INPUT MouseInput(uint flags, int data) {
    INPUT one = new INPUT();
    one.type = INPUT_MOUSE;
    one.U.mi.dwFlags = flags;
    one.U.mi.mouseData = unchecked((uint)data);
    return one;
  }

  static INPUT KeyInput(KeySpec key, bool up) {
    INPUT one = new INPUT();
    one.type = INPUT_KEYBOARD;
    one.U.ki.wVk = key.Vk;
    // Скан-код только для полноты lParam (без KEYEVENTF_SCANCODE Windows берёт wVk): часть приложений
    // (удалённые рабочие столы, эмуляторы) смотрит именно на скан-код.
    one.U.ki.wScan = (ushort)(MapVirtualKeyW(key.Vk, 0) & 0xFF);
    uint flags = 0;
    if (key.Extended) flags |= KEYEVENTF_EXTENDEDKEY;
    if (up) flags |= KEYEVENTF_KEYUP;
    one.U.ki.dwFlags = flags;
    return one;
  }

  static INPUT UnicodeInput(char unit, bool up) {
    INPUT one = new INPUT();
    one.type = INPUT_KEYBOARD;
    one.U.ki.wVk = 0;
    one.U.ki.wScan = unit;
    one.U.ki.dwFlags = up ? (KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) : KEYEVENTF_UNICODE;
    return one;
  }

  static INPUT[] Tap(KeySpec key) { return new INPUT[] { KeyInput(key, false), KeyInput(key, true) }; }

  // ⚠ ЧАСТИЧНАЯ ОТПРАВКА — ЭТО ОТКАЗ, А НЕ УСПЕХ. SendInput молча теряет события, когда ввод
  // блокирует окно с повышенными правами (UIPI), экран блокировки или политика системы. Без этой
  // проверки агент получал бы «клик выполнен» на клик, которого не было.
  static void Send(INPUT[] items) {
    if (items.Length == 0) return;
    uint sent = SendInput((uint)items.Length, items, InputSize);
    if (sent != (uint)items.Length) throw Blocked(sent, items.Length, "");
  }

  /// <summary>Отправка без исключения — только для уборки (отпустить зажатое после отказа).</summary>
  static bool TrySend(INPUT[] items) {
    if (items.Length == 0) return true;
    try { return SendInput((uint)items.Length, items, InputSize) == (uint)items.Length; } catch (Exception) { return false; }
  }

  static ChatRepoDriverError Blocked(uint sent, int total, string extra) {
    return new ChatRepoDriverError("blocked", "SendInput принял " + sent + " из " + total + " событий: Windows заблокировала ввод. " +
      "Обычно это окно, запущенное от имени администратора (UIPI), экран блокировки или запрос UAC. " + extra +
      "Действие не выполнено или выполнено частично: посмотрите на экран; окно с правами администратора может переключить только человек.");
  }

  // Зажатое между вызовами (key_down / mouse_down): отпускается парным *_up, а при закрытии stdin —
  // принудительно, чтобы у человека не «залип» Shift или левая кнопка.
  static readonly List<KeySpec> heldKeys = new List<KeySpec>();
  static readonly List<ButtonSpec> heldButtons = new List<ButtonSpec>();

  static bool IsHeld(KeySpec key) {
    foreach (KeySpec one in heldKeys) { if (one.Same(key)) return true; }
    return false;
  }

  static void ForgetKey(KeySpec key) {
    for (int i = heldKeys.Count - 1; i >= 0; i--) { if (heldKeys[i].Same(key)) heldKeys.RemoveAt(i); }
  }

  static void ForgetButton(ButtonSpec button) {
    for (int i = heldButtons.Count - 1; i >= 0; i--) { if (heldButtons[i].Name == button.Name) heldButtons.RemoveAt(i); }
  }

  public static void ReleaseAll() {
    lock (HeldGate) {
      try {
        foreach (ButtonSpec button in heldButtons) TrySend(new INPUT[] { MouseInput(button.Up, button.Data) });
        for (int i = heldKeys.Count - 1; i >= 0; i--) TrySend(new INPUT[] { KeyInput(heldKeys[i], true) });
      } catch (Exception) { }
      heldButtons.Clear();
      heldKeys.Clear();
      keyDownModifiers.Clear();
    }
  }

  /// <summary>Нажать модификаторы, которых ещё нет среди зажатых; вернуть нажатые (их и отпускать).</summary>
  static List<KeySpec> PressModifiers(List<KeySpec> modifiers) {
    List<KeySpec> pressed = new List<KeySpec>();
    foreach (KeySpec one in modifiers) { if (!IsHeld(one)) pressed.Add(one); }
    if (pressed.Count == 0) return pressed;
    INPUT[] downs = new INPUT[pressed.Count];
    for (int i = 0; i < pressed.Count; i++) downs[i] = KeyInput(pressed[i], false);
    uint sent = SendInput((uint)downs.Length, downs, InputSize);
    if (sent != (uint)downs.Length) {
      ReleaseModifiers(pressed, false);
      throw Blocked(sent, downs.Length, "");
    }
    return pressed;
  }

  /// <summary>Отпустить в обратном порядке. check=false — уборка после отказа, без исключения.</summary>
  static void ReleaseModifiers(List<KeySpec> pressed, bool check) {
    INPUT[] ups = new INPUT[pressed.Count];
    for (int i = 0; i < pressed.Count; i++) ups[i] = KeyInput(pressed[pressed.Count - 1 - i], true);
    if (check) Send(ups); else TrySend(ups);
  }

  // ───────────────────────────── Ввод: указатель ─────────────────────────────

  static void CheckPoint(int x, int y) {
    POINT point;
    point.X = x;
    point.Y = y;
    if (MonitorFromPoint(point, 0) == IntPtr.Zero) {
      throw Bad("Точка (" + x + ", " + y + ") не попадает ни на один экран. Координаты помощника — физические пиксели " +
        "виртуального рабочего стола Windows.");
    }
  }

  // ⚠ УКАЗАТЕЛЬ ДВИГАЕТСЯ НАСТОЯЩИМ СОБЫТИЕМ SendInput, А НЕ SetCursorPos: только событие движения
  // даёт окну WM_MOUSEMOVE, наведение, подсказки и начало перетаскивания. Нормировка — на ВИРТУАЛЬНЫЙ
  // стол (VIRTUALDESK): без неё 0..65535 покрывает только основной экран, и второй монитор недостижим.
  static bool TrySendMove(int x, int y) {
    int left = GetSystemMetrics(SM_XVIRTUALSCREEN);
    int top = GetSystemMetrics(SM_YVIRTUALSCREEN);
    int width = GetSystemMetrics(SM_CXVIRTUALSCREEN);
    int height = GetSystemMetrics(SM_CYVIRTUALSCREEN);
    if (width <= 1 || height <= 1) return false;
    INPUT move = MouseInput(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0);
    move.U.mi.dx = (int)Math.Round((x - left) * 65535.0 / (width - 1));
    move.U.mi.dy = (int)Math.Round((y - top) * 65535.0 / (height - 1));
    return TrySend(new INPUT[] { move });
  }

  static bool PointerAt(int x, int y, int waits) {
    for (int i = 0; ; i++) {
      POINT point;
      if (GetCursorPos(out point) && Math.Abs(point.X - x) <= 1 && Math.Abs(point.Y - y) <= 1) return true;
      if (i >= waits) return false;
      Thread.Sleep(2);
    }
  }

  /// <summary>Поставить указатель и убедиться, что он встал; иначе blocked.</summary>
  static void MovePointer(int x, int y) {
    bool injected = TrySendMove(x, y);
    if (PointerAt(x, y, injected ? 5 : 0)) return;
    // ⚠ Округление нормировки 0..65535 на широком виртуальном столе и причуды DPI дают промах в
    // пиксель-другой: доводим точным SetCursorPos, событие движения окно уже получило.
    SetCursorPos(x, y);
    if (PointerAt(x, y, 3)) return;
    POINT now;
    if (!GetCursorPos(out now)) {
      throw new ChatRepoDriverError("blocked", "Windows не даёт управлять указателем: открыт экран блокировки или запрос UAC " +
        "(защищённый рабочий стол). Дождитесь, пока человек его закроет.");
    }
    throw new ChatRepoDriverError("blocked", "Указатель не встал в (" + x + ", " + y + "), он в (" + now.X + ", " + now.Y + "). " +
      "Ввод перехватывает окно с правами администратора или политика системы; посмотрите на экран.");
  }

  // ───────────────────────────── input ─────────────────────────────

  static object Input(Dictionary<string, object> p) {
    string action = OptString(p, "action");
    if (action == null) throw Bad("input: нужно поле action (move, click, mouse_down, mouse_up, drag, scroll, type, key, key_down, key_up).");
    switch (action) {
      case "move": DoMove(p); break;
      case "click": DoClick(p); break;
      case "mouse_down": DoMouseDown(p); break;
      case "mouse_up": DoMouseUp(p); break;
      case "drag": DoDrag(p); break;
      case "scroll": DoScroll(p); break;
      case "type": DoType(p); break;
      case "key": DoKey(p, "key"); break;
      case "key_down": DoKey(p, "key_down"); break;
      case "key_up": DoKey(p, "key_up"); break;
      default: throw Bad("Неизвестное действие ввода «" + action + "». Допустимы: move, click, mouse_down, mouse_up, drag, scroll, type, key, key_down, key_up.");
    }
    Dictionary<string, object> result = new Dictionary<string, object>();
    POINT point;
    result["cursor"] = GetCursorPos(out point) ? (object)Point(point.X, point.Y) : null;
    return result;
  }

  static object CursorPoint() {
    POINT point;
    if (!GetCursorPos(out point)) {
      throw new ChatRepoDriverError("blocked", "Windows не отдала позицию указателя: открыт экран блокировки или запрос UAC " +
        "(защищённый рабочий стол).");
    }
    return Point(point.X, point.Y);
  }

  static void DoMove(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    CheckPoint(x, y);
    MovePointer(x, y);
  }

  static void DoClick(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    ButtonSpec button = ButtonOf(p);
    // Preserve the requested positive click count within the native integer representation.
    int clicks = OptInt(p, "clicks", 1, 1, int.MaxValue);
    int hold = OptInt(p, "hold_ms", 0, 0, int.MaxValue);
    // Input has no duration ceiling; the client can cancel by stopping this helper.
    List<KeySpec> modifiers = ModifiersOf(p);
    CheckPoint(x, y);
    MovePointer(x, y);
    List<KeySpec> pressed = PressModifiers(modifiers);
    bool done = false;
    try {
      if (hold == 0) {
        // Все нажатия одной пачкой: двойной клик распознаётся, только если оба пришли в пределах
        // системного интервала двойного щелчка.
        INPUT[] sequence = new INPUT[clicks * 2];
        for (int i = 0; i < clicks; i++) {
          sequence[i * 2] = MouseInput(button.Down, button.Data);
          sequence[i * 2 + 1] = MouseInput(button.Up, button.Data);
        }
        Send(sequence);
      } else {
        for (int i = 0; i < clicks; i++) {
          Send(new INPUT[] { MouseInput(button.Down, button.Data) });
          Thread.Sleep(hold);
          Send(new INPUT[] { MouseInput(button.Up, button.Data) });
        }
      }
      done = true;
    } finally {
      if (!done) TrySend(new INPUT[] { MouseInput(button.Up, button.Data) });
      ReleaseModifiers(pressed, done);
    }
  }

  static void DoMouseDown(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    ButtonSpec button = ButtonOf(p);
    List<KeySpec> modifiers = ModifiersOf(p);
    CheckPoint(x, y);
    MovePointer(x, y);
    List<KeySpec> pressed = PressModifiers(modifiers);
    try {
      Send(new INPUT[] { MouseInput(button.Down, button.Data) });
    } catch {
      ReleaseModifiers(pressed, false);
      throw;
    }
    // ⚠ Модификаторы и кнопка остаются зажатыми до парного mouse_up (или до закрытия помощника).
    heldKeys.AddRange(pressed);
    ForgetButton(button);
    heldButtons.Add(button);
  }

  static void DoMouseUp(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    ButtonSpec button = ButtonOf(p);
    List<KeySpec> modifiers = ModifiersOf(p);
    CheckPoint(x, y);
    MovePointer(x, y);
    Send(new INPUT[] { MouseInput(button.Up, button.Data) });
    ForgetButton(button);
    // Модификаторы отпускаются ПОСЛЕ кнопки: Shift+перетаскивание обязано закончиться с Shift.
    ReleaseModifiers(modifiers, true);
    foreach (KeySpec one in modifiers) ForgetKey(one);
  }

  static void DoDrag(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    int toX = ReqInt(p, "toX"), toY = ReqInt(p, "toY");
    ButtonSpec button = ButtonOf(p);
    // Preserve requested steps and duration; native integer representation is validated.
    int steps = OptInt(p, "steps", 24, 2, int.MaxValue);
    // До 10 с: клиент ждёт ответа не дольше 30 с, а перетаскивание держит кнопку всё это время.
    int duration = OptInt(p, "duration_ms", 400, 0, int.MaxValue);
    List<KeySpec> modifiers = ModifiersOf(p);
    CheckPoint(x, y);
    CheckPoint(toX, toY);
    MovePointer(x, y);
    List<KeySpec> pressed = PressModifiers(modifiers);
    bool buttonDown = false;
    bool done = false;
    try {
      Send(new INPUT[] { MouseInput(button.Down, button.Data) });
      buttonDown = true;
      Thread.Sleep(40);
      // ⚠ ПРОМЕЖУТОЧНЫЕ ШАГИ ОБЯЗАТЕЛЬНЫ. Приложения начинают перетаскивание по движению с зажатой
      // кнопкой; прыжок из точки в точку многие принимают за обычный клик и ничего не тащат.
      int pause = duration / steps;
      for (int i = 1; i <= steps; i++) {
        int nx = x + (int)Math.Round((toX - x) * (double)i / steps);
        int ny = y + (int)Math.Round((toY - y) * (double)i / steps);
        if (!TrySendMove(nx, ny)) SetCursorPos(nx, ny);
        if (pause > 0) Thread.Sleep(pause);
      }
      MovePointer(toX, toY);
      Thread.Sleep(60);
      Send(new INPUT[] { MouseInput(button.Up, button.Data) });
      buttonDown = false;
      done = true;
    } finally {
      if (buttonDown) TrySend(new INPUT[] { MouseInput(button.Up, button.Data) });
      ReleaseModifiers(pressed, done);
    }
  }

  static void DoScroll(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    double deltaX = 0, deltaY = 0;
    if (Get(p, "deltaX") != null && !ToDouble(Get(p, "deltaX"), out deltaX)) throw Bad("deltaX должен быть числом.");
    if (Get(p, "deltaY") != null && !ToDouble(Get(p, "deltaY"), out deltaY)) throw Bad("deltaY должен быть числом.");
    string unit = OptString(p, "unit");
    unit = unit == null ? "pixel" : unit.Trim().ToLowerInvariant();
    // unit: "line" (синоним "notch") — число делений колеса, каждое = WHEEL_DELTA (120).
    // "pixel" — прежняя семантика без изменений: значение уходит в mouseData КАК ЕСТЬ, то есть в
    // единицах колеса 1/120 деления. Настоящих пикселей Windows не знает: сколько строк/пикселей
    // прокрутит 120 единиц, решает приложение (обычно 3 строки на деление).
    if (unit == "notch") unit = "line";
    if (unit != "pixel" && unit != "line") throw Bad("unit должен быть pixel, line или notch.");
    double scale = unit == "line" ? 120.0 : 1.0;
    // ⚠ ЗНАКИ: положительный deltaY — вниз, а у WHEEL положительное значение — «от себя» (вверх),
    // поэтому WHEEL = -deltaY. У HWHEEL положительное — вправо, поэтому HWHEEL = +deltaX.
    double wheelY = -Math.Round(deltaY) * scale;
    double wheelX = Math.Round(deltaX) * scale;
    if (Math.Abs(wheelY) > int.MaxValue || Math.Abs(wheelX) > int.MaxValue) throw Bad("deltaX/deltaY вне диапазона int32 единиц колеса.");
    if (wheelY == 0 && wheelX == 0) throw Bad("Нужен ненулевой deltaX или deltaY.");
    List<KeySpec> modifiers = ModifiersOf(p);
    CheckPoint(x, y);
    MovePointer(x, y);
    List<KeySpec> pressed = PressModifiers(modifiers);
    bool done = false;
    try {
      List<INPUT> sequence = new List<INPUT>();
      if (wheelY != 0) sequence.Add(MouseInput(WHEEL, (int)wheelY));
      if (wheelX != 0) sequence.Add(MouseInput(HWHEEL, (int)wheelX));
      Send(sequence.ToArray());
      done = true;
    } finally {
      ReleaseModifiers(pressed, done);
    }
  }

  static void DoType(Dictionary<string, object> p) {
    string text = Get(p, "text") as string;
    if (text == null) throw Bad("type: нужно строковое поле text.");
    if (text.Length == 0) throw Bad("type: текст пуст.");
    // ⚠⚠⚠ ЮНИКОД НАПРЯМУЮ, БЕЗ ЭМУЛЯЦИИ КЛАВИШ ПО СТРОКЕ. Прежний путь через экранирование посылал
    // модификаторы вместо знаков + ^ % ~ ( ): «100%» нажимало Alt. KEYEVENTF_UNICODE вводит любой
    // символ как есть, не зависит от раскладки и ничего не экранирует. Суррогатная пара — двумя
    // событиями, и она никогда не разрывается между пачками.
    List<INPUT[]> groups = new List<INPUT[]>();
    for (int i = 0; i < text.Length; i++) {
      char c = text[i];
      if (c == '\r' || c == '\n') {
        if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n') i++;
        groups.Add(Tap(new KeySpec(VK_RETURN, false)));
      } else if (c == '\t') {
        groups.Add(Tap(new KeySpec(VK_TAB, false)));
      } else if (char.IsHighSurrogate(c) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1])) {
        char low = text[i + 1];
        groups.Add(new INPUT[] { UnicodeInput(c, false), UnicodeInput(c, true), UnicodeInput(low, false), UnicodeInput(low, true) });
        i++;
      } else {
        groups.Add(new INPUT[] { UnicodeInput(c, false), UnicodeInput(c, true) });
      }
    }
    // Пачками по ≤200 событий с паузой 4 мс: иначе длинный текст упирается в очередь ввода и теряется.
    List<INPUT> batch = new List<INPUT>();
    int typed = 0, inBatch = 0;
    for (int g = 0; g < groups.Count; g++) {
      if (batch.Count + groups[g].Length > 200) {
        SendTypingBatch(batch, typed, groups.Count);
        typed += inBatch;
        batch.Clear();
        inBatch = 0;
        Thread.Sleep(4);
      }
      batch.AddRange(groups[g]);
      inBatch++;
    }
    if (batch.Count > 0) SendTypingBatch(batch, typed, groups.Count);
  }

  static void SendTypingBatch(List<INPUT> batch, int typedBefore, int total) {
    INPUT[] items = batch.ToArray();
    uint sent = SendInput((uint)items.Length, items, InputSize);
    if (sent != (uint)items.Length) {
      int typed = typedBefore + (int)(sent / 2);
      throw Blocked(sent, items.Length, "Введено примерно " + typed + " из " + total + " символов. ");
    }
  }

  // ⚠ ПАРНЫЙ key_up ОТПУСКАЕТ ТО, ЧТО НАЖАЛ ЕГО key_down, даже если пришёл без модификаторов: иначе
  // Shift, зажатый ради «!» или явный Ctrl из key_down, остаётся нажатым у человека. Ключ — vk+extended.
  static readonly Dictionary<string, List<KeySpec>> keyDownModifiers = new Dictionary<string, List<KeySpec>>();

  static string KeyId(KeySpec key) { return key.Vk.ToString(CultureInfo.InvariantCulture) + (key.Extended ? "e" : ""); }

  static void AddUnique(List<KeySpec> list, KeySpec key) {
    foreach (KeySpec one in list) { if (one.Same(key)) return; }
    list.Add(key);
  }

  static void DoKey(Dictionary<string, object> p, string action) {
    string name = OptString(p, "key");
    int repeat = action == "key" ? OptInt(p, "repeat", 1, 1, int.MaxValue) : 1;
    List<KeySpec> implied = new List<KeySpec>();
    KeySpec key = KeyOf(name, implied);
    // Явные модификаторы + требуемые раскладкой для символа («!» → Shift), без повторов.
    List<KeySpec> modifiers = ModifiersOf(p);
    foreach (KeySpec one in implied) AddUnique(modifiers, one);
    if (action == "key_down") {
      List<KeySpec> pressed = PressModifiers(modifiers);
      try {
        Send(new INPUT[] { KeyInput(key, false) });
      } catch {
        ReleaseModifiers(pressed, false);
        throw;
      }
      // ⚠ Клавиша и модификаторы остаются зажатыми до парного key_up (или до закрытия помощника).
      heldKeys.AddRange(pressed);
      if (!IsHeld(key)) heldKeys.Add(key);
      List<KeySpec> recorded;
      if (!keyDownModifiers.TryGetValue(KeyId(key), out recorded)) {
        recorded = new List<KeySpec>();
        keyDownModifiers[KeyId(key)] = recorded;
      }
      foreach (KeySpec one in pressed) AddUnique(recorded, one);
    } else if (action == "key_up") {
      Send(new INPUT[] { KeyInput(key, true) });
      ForgetKey(key);
      List<KeySpec> release = new List<KeySpec>();
      List<KeySpec> recorded;
      if (keyDownModifiers.TryGetValue(KeyId(key), out recorded)) {
        foreach (KeySpec one in recorded) AddUnique(release, one);
        keyDownModifiers.Remove(KeyId(key));
      }
      foreach (KeySpec one in modifiers) AddUnique(release, one);
      ReleaseModifiers(release, true);
      foreach (KeySpec one in release) ForgetKey(one);
    } else {
      List<KeySpec> pressed = PressModifiers(modifiers);
      bool done = false;
      try {
        INPUT[] sequence = new INPUT[] { KeyInput(key, false), KeyInput(key, true) };
        for (int i = 0; i < repeat; i++) Send(sequence);
        done = true;
      } finally {
        if (!done) TrySend(new INPUT[] { KeyInput(key, true) });
        ReleaseModifiers(pressed, done);
      }
    }
  }

  // ───────────────────────────── Окна ─────────────────────────────

  delegate bool WaitCheck();

  static bool WaitFor(WaitCheck check, int milliseconds) {
    Stopwatch clock = Stopwatch.StartNew();
    while (true) {
      if (check()) return true;
      if (clock.ElapsedMilliseconds >= milliseconds) return false;
      Thread.Sleep(20);
    }
  }

  static List<IntPtr> TopWindows() {
    List<IntPtr> handles = new List<IntPtr>();
    EnumWindowsProc callback = delegate(IntPtr hwnd, IntPtr lParam) { handles.Add(hwnd); return true; };
    EnumWindows(callback, IntPtr.Zero);
    GC.KeepAlive(callback);
    return handles;
  }

  static string WindowTitle(IntPtr hwnd) {
    int length = GetWindowTextLengthW(hwnd);
    if (length <= 0) return "";
    StringBuilder text = new StringBuilder(length + 1);
    GetWindowTextW(hwnd, text, text.Capacity);
    return text.ToString();
  }

  // ⚠ ОБЛАЧЁННЫЕ ОКНА ВИДИМЫ ДЛЯ IsWindowVisible, НО НЕ НА ЭКРАНЕ: окна других виртуальных столов,
  // приостановленные приложения магазина, фоновые окна UWP. Без этой проверки агент «видит» и
  // пытается кликнуть окна, которых на экране нет.
  static bool IsCloaked(IntPtr hwnd) {
    try {
      int cloaked;
      return DwmGetInt(hwnd, DWMWA_CLOAKED, out cloaked, 4) == 0 && cloaked != 0;
    } catch (Exception) {
      return false;
    }
  }

  static bool IsAppWindow(IntPtr hwnd) {
    if (!IsWindowVisible(hwnd)) return false;
    if (GetAncestor(hwnd, GA_ROOTOWNER) != hwnd) return false;
    int exStyle = GetWindowLong(hwnd, GWL_EXSTYLE);
    if ((exStyle & WS_EX_TOOLWINDOW) != 0 && (exStyle & WS_EX_APPWINDOW) == 0) return false;
    if (IsCloaked(hwnd)) return false;
    return WindowTitle(hwnd).Length > 0;
  }

  /// <summary>Видимая рамка окна (без невидимых полей изменения размера), иначе GetWindowRect.</summary>
  static bool VisibleRect(IntPtr hwnd, out RECT rect) {
    try {
      if (DwmGetRect(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, out rect, Marshal.SizeOf(typeof(RECT))) == 0 && rect.Right > rect.Left) return true;
    } catch (Exception) { }
    return GetWindowRect(hwnd, out rect);
  }

  static string ProcessName(uint pid, Dictionary<uint, string> cache) {
    string name;
    if (cache != null && cache.TryGetValue(pid, out name)) return name;
    name = "";
    try {
      using (Process process = Process.GetProcessById((int)pid)) { name = process.ProcessName; }
    } catch (Exception) { }
    if (cache != null) cache[pid] = name;
    return name;
  }

  static IntPtr ForegroundRoot() {
    IntPtr foreground = GetForegroundWindow();
    if (foreground == IntPtr.Zero) return IntPtr.Zero;
    IntPtr root = GetAncestor(foreground, GA_ROOTOWNER);
    return root == IntPtr.Zero ? foreground : root;
  }

  static string WindowId(IntPtr hwnd) { return hwnd.ToInt64().ToString(CultureInfo.InvariantCulture); }

  static Dictionary<string, object> WindowEntry(IntPtr hwnd, Dictionary<uint, string> cache, IntPtr foregroundRoot) {
    uint pid;
    GetWindowThreadProcessId(hwnd, out pid);
    Dictionary<string, object> entry = new Dictionary<string, object>();
    entry["id"] = WindowId(hwnd);
    entry["title"] = WindowTitle(hwnd);
    entry["app"] = ProcessName(pid, cache);
    entry["pid"] = (int)pid;
    RECT rect;
    if (VisibleRect(hwnd, out rect)) entry["bounds"] = Bounds(rect.Left, rect.Top, rect.Right - rect.Left, rect.Bottom - rect.Top);
    entry["focused"] = hwnd == foregroundRoot;
    entry["minimized"] = IsIconic(hwnd);
    return entry;
  }

  static List<IntPtr> AppWindows() {
    List<IntPtr> result = new List<IntPtr>();
    foreach (IntPtr hwnd in TopWindows()) { if (IsAppWindow(hwnd)) result.Add(hwnd); }
    return result;
  }

  static object ListWindows() {
    Dictionary<uint, string> cache = new Dictionary<uint, string>();
    IntPtr foregroundRoot = ForegroundRoot();
    List<object> windows = new List<object>();
    // EnumWindows отдаёт окна верхнего уровня в Z-порядке — спереди назад, как требует протокол.
    foreach (IntPtr hwnd in AppWindows()) windows.Add(WindowEntry(hwnd, cache, foregroundRoot));
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["windows"] = windows;
    result["frontmost_app"] = null;
    IntPtr foreground = GetForegroundWindow();
    if (foreground != IntPtr.Zero) {
      uint pid;
      GetWindowThreadProcessId(foreground, out pid);
      Dictionary<string, object> app = new Dictionary<string, object>();
      app["name"] = ProcessName(pid, cache);
      app["pid"] = (int)pid;
      result["frontmost_app"] = app;
    }
    return result;
  }

  static IntPtr WindowHandleOf(string id) {
    if (string.IsNullOrEmpty(id)) throw Bad("Нужен id окна из метода windows.");
    long value;
    if (!long.TryParse(id, NumberStyles.Integer, CultureInfo.InvariantCulture, out value) || value == 0) {
      throw Bad("id окна «" + id + "» не похож на идентификатор из метода windows (десятичное число).");
    }
    IntPtr hwnd;
    try { hwnd = new IntPtr(value); } catch (OverflowException) { throw Bad("id окна «" + id + "» вне диапазона дескрипторов."); }
    if (!IsWindow(hwnd)) throw new ChatRepoDriverError("not_found", "Окна id=" + id + " больше нет (закрыто). Вызовите windows заново.");
    return hwnd;
  }

  static bool IsForeground(IntPtr hwnd) {
    IntPtr foreground = GetForegroundWindow();
    return foreground == hwnd || (foreground != IntPtr.Zero && GetAncestor(foreground, GA_ROOTOWNER) == hwnd);
  }

  static void FocusWindow(IntPtr hwnd) {
    if (IsIconic(hwnd)) {
      ShowWindowAsync(hwnd, SW_RESTORE);
      WaitFor(delegate { return !IsIconic(hwnd); }, 1000);
    }
    if (IsForeground(hwnd)) return;
    SetForegroundWindow(hwnd);
    if (WaitFor(delegate { return IsForeground(hwnd); }, 150)) return;
    // ⚠⚠ WINDOWS ОГРАНИЧИВАЕТ КРАЖУ ФОКУСА: фоновому процессу SetForegroundWindow обычно отказывает
    // и лишь мигает кнопкой на панели задач. Законные обходы — присоединиться к очереди ввода
    // переднего потока и стать «получателем последнего ввода» (синтетический Alt). Alt зажимается ДО
    // переключения и отпускается ПОСЛЕ: пара нажатие+отпускание в одном окне открыла бы его меню.
    IntPtr foreground = GetForegroundWindow();
    uint pid;
    uint foregroundThread = foreground == IntPtr.Zero ? 0 : GetWindowThreadProcessId(foreground, out pid);
    uint self = GetCurrentThreadId();
    bool attached = foregroundThread != 0 && foregroundThread != self && AttachThreadInput(self, foregroundThread, true);
    KeySpec alt = new KeySpec(VK_MENU, false);
    try {
      TrySend(new INPUT[] { KeyInput(alt, false) });
      BringWindowToTop(hwnd);
      SetForegroundWindow(hwnd);
    } finally {
      TrySend(new INPUT[] { KeyInput(alt, true) });
      if (attached) AttachThreadInput(self, foregroundThread, false);
    }
    if (WaitFor(delegate { return IsForeground(hwnd); }, 500)) return;
    throw new ChatRepoDriverError("blocked", "Windows не дала вывести окно «" + WindowTitle(hwnd) + "» на передний план: " +
      "система не позволяет фоновым программам забирать фокус (например, пока человек работает в другом окне, " +
      "или окно принадлежит программе с правами администратора). Кликните по заголовку окна или по его кнопке " +
      "на панели задач либо попросите человека переключиться на него.");
  }

  static object WindowAction(Dictionary<string, object> p) {
    string action = OptString(p, "action");
    if (action == null) throw Bad("window: нужно поле action (focus, minimize, maximize, restore, close, set_bounds).");
    IntPtr hwnd = WindowHandleOf(OptString(p, "id"));
    Dictionary<string, object> result = new Dictionary<string, object>();
    switch (action) {
      case "focus":
        FocusWindow(hwnd);
        break;
      case "minimize":
        ShowWindowAsync(hwnd, SW_MINIMIZE);
        if (!WaitFor(delegate { return IsIconic(hwnd); }, 1500)) throw NotResponding(hwnd, "свернуть");
        break;
      case "maximize":
        ShowWindowAsync(hwnd, SW_MAXIMIZE);
        if (!WaitFor(delegate { return IsZoomed(hwnd); }, 1500)) throw NotResponding(hwnd, "развернуть");
        break;
      case "restore":
        ShowWindowAsync(hwnd, SW_RESTORE);
        if (!WaitFor(delegate { return !IsIconic(hwnd) && !IsZoomed(hwnd); }, 1500)) throw NotResponding(hwnd, "восстановить");
        break;
      case "close":
        PostMessageW(hwnd, WM_CLOSE, IntPtr.Zero, IntPtr.Zero);
        if (WaitFor(delegate { return !IsWindow(hwnd); }, 1500)) {
          result["window"] = null;
          result["closed"] = true;
          return result;
        }
        result["closed"] = false;
        result["note"] = "Окно получило команду закрытия, но ещё открыто: вероятно, приложение спрашивает о сохранении. Посмотрите на экран.";
        break;
      case "set_bounds":
        SetBounds(hwnd, p);
        break;
      default:
        throw Bad("Неизвестное действие с окном «" + action + "». Допустимы: focus, minimize, maximize, restore, close, set_bounds.");
    }
    result["window"] = WindowEntry(hwnd, null, ForegroundRoot());
    return result;
  }

  static ChatRepoDriverError NotResponding(IntPtr hwnd, string verb) {
    return new ChatRepoDriverError("failed", "Окно «" + WindowTitle(hwnd) + "» не удалось " + verb + " за 1,5 с: " +
      "приложение не отвечает или запрещает это действие. Посмотрите на экран.");
  }

  static void SetBounds(IntPtr hwnd, Dictionary<string, object> p) {
    Dictionary<string, object> bounds = Get(p, "bounds") as Dictionary<string, object>;
    if (bounds == null) throw Bad("set_bounds: нужно поле bounds {x, y, width, height}.");
    int x = ReqInt(bounds, "x"), y = ReqInt(bounds, "y"), width = ReqInt(bounds, "width"), height = ReqInt(bounds, "height");
    if (width < 1 || height < 1) throw Bad("set_bounds: width и height должны быть положительными.");
    if (IsIconic(hwnd) || IsZoomed(hwnd)) {
      ShowWindowAsync(hwnd, SW_RESTORE);
      WaitFor(delegate { return !IsIconic(hwnd) && !IsZoomed(hwnd); }, 1000);
    }
    // ⚠ bounds в протоколе — ВИДИМАЯ рамка (DWMWA_EXTENDED_FRAME_BOUNDS), а SetWindowPos берёт
    // прямоугольник с невидимыми полями изменения размера (≈7 px слева, справа и снизу на Windows 10/11).
    // Без поправки окно встаёт на несколько пикселей не туда, куда просили.
    RECT outer, frame;
    int dl = 0, dt = 0, dr = 0, db = 0;
    if (GetWindowRect(hwnd, out outer) && VisibleRect(hwnd, out frame)) {
      dl = frame.Left - outer.Left; dt = frame.Top - outer.Top;
      dr = outer.Right - frame.Right; db = outer.Bottom - frame.Bottom;
      if (dl < 0 || dl > 64 || dt < 0 || dt > 64 || dr < 0 || dr > 64 || db < 0 || db > 64) { dl = 0; dt = 0; dr = 0; db = 0; }
    }
    // ASYNC: зависшее приложение не должно подвесить помощника.
    if (!SetWindowPos(hwnd, IntPtr.Zero, x - dl, y - dt, width + dl + dr, height + dt + db, SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS)) {
      throw new ChatRepoDriverError("blocked", "Windows отказала в перемещении окна «" + WindowTitle(hwnd) + "» " +
        "(код " + Marshal.GetLastWin32Error() + "): обычно это окно программы с правами администратора.");
    }
    WaitFor(delegate {
      RECT now;
      return VisibleRect(hwnd, out now) && Math.Abs(now.Left - x) <= 2 && Math.Abs(now.Top - y) <= 2 &&
        Math.Abs(now.Right - now.Left - width) <= 2 && Math.Abs(now.Bottom - now.Top - height) <= 2;
    }, 1000);
    // Приложение вправе ограничить минимальный размер: ответ несёт фактические bounds, а не запрошенные.
  }

  // ───────────────────────────── launch ─────────────────────────────

  static object Launch(Dictionary<string, object> p) {
    string app = OptString(p, "app");
    if (app == null || app.Trim().Length == 0) throw Bad("launch: нужно поле app (имя из меню «Пуск», путь или имя .exe).");
    app = app.Trim();
    List<string> args = new List<string>();
    foreach (object one in OptList(p, "args")) {
      string text = one as string;
      if (text == null) throw Bad("args должен быть массивом строк.");
      args.Add(text);
    }
    string argLine = JoinArgs(args);
    // 1. Существующий путь — запускается как есть (аргументы — списком, без оболочки).
    if (File.Exists(app) || Directory.Exists(app)) return StartProcess(app, argLine, Path.GetFileNameWithoutExtension(app));
    if (app.IndexOf('\\') >= 0 || app.IndexOf('/') >= 0) {
      throw new ChatRepoDriverError("not_found", "Файл «" + app + "» не найден. Проверьте путь или укажите имя приложения из меню «Пуск».");
    }
    // 2. Приложения меню «Пуск» (включая приложения магазина) — через shell:AppsFolder.
    if (!app.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) {
      string[] found = FindStartApp(app);
      if (found != null) {
        if (args.Count > 0) {
          if (File.Exists(found[1])) return StartProcess(found[1], argLine, found[0]);
          throw new ChatRepoDriverError("unsupported", "Приложение «" + found[0] + "» из меню «Пуск» запускается только без " +
            "аргументов (это приложение магазина или ярлык без пути к .exe). Запустите его без args или укажите полный путь к .exe.");
        }
        string explorer = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe");
        ProcessStartInfo info = new ProcessStartInfo(explorer, QuoteArg("shell:AppsFolder\\" + found[1]));
        info.UseShellExecute = false;
        using (Process started = Process.Start(info)) { }
        // pid не возвращается: explorer.exe лишь посредник и сразу завершается.
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["app"] = found[0];
        return result;
      }
    }
    // 3. Зарегистрированные программы (App Paths) и PATH.
    return StartProcess(app, argLine, app);
  }

  static object StartProcess(string file, string argLine, string display) {
    ProcessStartInfo info = new ProcessStartInfo(file, argLine);
    info.UseShellExecute = true;
    Process started;
    try {
      started = Process.Start(info);
    } catch (Win32Exception error) {
      if (error.NativeErrorCode == 2 || error.NativeErrorCode == 3) {
        throw new ChatRepoDriverError("not_found", "Приложение «" + file + "» не найдено: его нет ни в меню «Пуск», ни среди " +
          "зарегистрированных программ (App Paths), ни в PATH. Укажите полный путь к .exe или точное имя из меню «Пуск».");
      }
      if (error.NativeErrorCode == 1223) throw new ChatRepoDriverError("permission", "Запуск «" + file + "» отменён: запрос UAC отклонён.");
      if (error.NativeErrorCode == 5) throw new ChatRepoDriverError("permission", "Windows отказала в запуске «" + file + "»: нет доступа (политика или права).");
      throw new ChatRepoDriverError("failed", "Не удалось запустить «" + file + "»: " + error.Message);
    }
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["app"] = display;
    if (started != null) {
      try { result["pid"] = started.Id; } catch (Exception) { }
      started.Dispose();
    }
    return result;
  }

  /// <summary>Правила разбора командной строки MSVCRT/CommandLineToArgvW: пробелы, кавычки, обратные косые.</summary>
  static string QuoteArg(string arg) {
    if (arg.Length > 0 && arg.IndexOfAny(new char[] { ' ', '\t', '\n', '\v', '"' }) < 0) return arg;
    StringBuilder builder = new StringBuilder("\"");
    int backslashes = 0;
    foreach (char c in arg) {
      if (c == '\\') { backslashes++; continue; }
      if (c == '"') { builder.Append('\\', backslashes * 2 + 1); builder.Append('"'); }
      else { builder.Append('\\', backslashes); builder.Append(c); }
      backslashes = 0;
    }
    builder.Append('\\', backslashes * 2);
    builder.Append('"');
    return builder.ToString();
  }

  static string JoinArgs(List<string> args) {
    StringBuilder line = new StringBuilder();
    foreach (string one in args) {
      if (line.Length > 0) line.Append(' ');
      line.Append(QuoteArg(one));
    }
    return line.ToString();
  }

  static object ComInvoke(object target, string name, BindingFlags kind, params object[] args) {
    return target.GetType().InvokeMember(name, kind, null, target, args);
  }

  static void ReleaseCom(object com) {
    if (com == null || !Marshal.IsComObject(com)) return;
    try { Marshal.ReleaseComObject(com); } catch (Exception) { }
  }

  /// <summary>Поиск в shell:AppsFolder: точное имя (или AUMID), затем начало имени, затем подстрока; короче — лучше.</summary>
  static string[] FindStartApp(string query) {
    Type shellType = Type.GetTypeFromProgID("Shell.Application");
    if (shellType == null) return null;
    object shell = null, folder = null, items = null;
    try {
      shell = Activator.CreateInstance(shellType);
      folder = ComInvoke(shell, "NameSpace", BindingFlags.InvokeMethod, "shell:AppsFolder");
      if (folder == null) return null;
      items = ComInvoke(folder, "Items", BindingFlags.InvokeMethod);
      int count = Convert.ToInt32(ComInvoke(items, "Count", BindingFlags.GetProperty), CultureInfo.InvariantCulture);
      string wanted = query.ToLowerInvariant();
      string[] exact = null, prefix = null, contains = null;
      for (int i = 0; i < count && exact == null; i++) {
        object item = null;
        try {
          item = ComInvoke(items, "Item", BindingFlags.InvokeMethod, i);
          if (item == null) continue;
          string name = ComInvoke(item, "Name", BindingFlags.GetProperty) as string;
          string path = ComInvoke(item, "Path", BindingFlags.GetProperty) as string;
          if (string.IsNullOrEmpty(name) || string.IsNullOrEmpty(path)) continue;
          string lower = name.ToLowerInvariant();
          if (lower == wanted || path.ToLowerInvariant() == wanted) exact = new string[] { name, path };
          else if (lower.StartsWith(wanted, StringComparison.Ordinal)) { if (prefix == null || name.Length < prefix[0].Length) prefix = new string[] { name, path }; }
          else if (lower.Contains(wanted)) { if (contains == null || name.Length < contains[0].Length) contains = new string[] { name, path }; }
        } catch (Exception) {
          // Отдельный сломанный ярлык не должен лишать поиска остальных.
        } finally {
          ReleaseCom(item);
        }
      }
      if (exact != null) return exact;
      return prefix != null ? prefix : contains;
    } catch (Exception) {
      return null;
    } finally {
      ReleaseCom(items);
      ReleaseCom(folder);
      ReleaseCom(shell);
    }
  }

  // ───────────────────────────── Дерево доступности (UI Automation) ─────────────────────────────

  const int ElementsBudgetMs = 3000;
  const int ActionBudgetMs = 5000;
  static readonly object ElementGate = new object();
  static int epoch;
  static Dictionary<string, AutomationElement> elementCache = new Dictionary<string, AutomationElement>();

  // ⚠⚠ ВСЯ РАБОТА С UIA — В ОТДЕЛЬНОМ MTA-ПОТОКЕ С ТАЙМАУТОМ. Вызов UIA уходит в процесс приложения;
  // зависшее приложение держит его десятки секунд, а InvokePattern.Invoke на кнопке Win32, открывшей
  // модальное окно, не возвращается, пока окно не закроют. Главный поток обязан ответить клиенту
  // вовремя, поэтому ждёт рабочий поток ограниченно и бросает его, если тот застрял.
  static bool RunWorker(ThreadStart work, int milliseconds) {
    ThreadStart dpiAware = delegate {
      EnsureThreadDpi();
      work();
    };
    Thread thread = new Thread(dpiAware);
    thread.IsBackground = true;
    thread.SetApartmentState(ApartmentState.MTA);
    thread.Start();
    return thread.Join(milliseconds);
  }

  // ⚠ Свойства читаются ПАКЕТОМ через CacheRequest: одно обращение к процессу на элемент вместо
  // двадцати. Значение (ValuePattern.Value) в пакет НЕ входит нарочно — см. ValueOf.
  static CacheRequest BuildRequest() {
    CacheRequest request = new CacheRequest();
    request.AutomationElementMode = AutomationElementMode.Full;
    request.TreeFilter = Automation.ControlViewCondition;
    request.TreeScope = TreeScope.Element;
    AutomationProperty[] properties = new AutomationProperty[] {
      AutomationElement.ControlTypeProperty, AutomationElement.LocalizedControlTypeProperty,
      AutomationElement.NameProperty, AutomationElement.HelpTextProperty,
      AutomationElement.BoundingRectangleProperty, AutomationElement.IsOffscreenProperty,
      AutomationElement.IsEnabledProperty, AutomationElement.HasKeyboardFocusProperty,
      AutomationElement.IsKeyboardFocusableProperty, AutomationElement.IsPasswordProperty,
      AutomationElement.ProcessIdProperty, AutomationElement.NativeWindowHandleProperty,
      AutomationElement.FrameworkIdProperty, AutomationElement.ClassNameProperty,
      AutomationElement.IsInvokePatternAvailableProperty, AutomationElement.IsValuePatternAvailableProperty,
      AutomationElement.IsTogglePatternAvailableProperty, AutomationElement.IsExpandCollapsePatternAvailableProperty,
      AutomationElement.IsSelectionItemPatternAvailableProperty, AutomationElement.IsScrollItemPatternAvailableProperty,
      AutomationElement.IsRangeValuePatternAvailableProperty, AutomationElement.IsScrollPatternAvailableProperty,
      ValuePattern.IsReadOnlyProperty, RangeValuePattern.ValueProperty, RangeValuePattern.IsReadOnlyProperty,
      TogglePattern.ToggleStateProperty, ExpandCollapsePattern.ExpandCollapseStateProperty, WindowPattern.IsModalProperty
    };
    foreach (AutomationProperty property in properties) request.Add(property);
    return request;
  }

  static object Cached(AutomationElement element, AutomationProperty property) {
    try {
      object value = element.GetCachedPropertyValue(property, false);
      return value == AutomationElement.NotSupported ? null : value;
    } catch (Exception) {
      return null;
    }
  }

  static bool CachedBool(AutomationElement element, AutomationProperty property) {
    object value = Cached(element, property);
    return value is bool && (bool)value;
  }

  static string CachedString(AutomationElement element, AutomationProperty property) {
    return Cached(element, property) as string;
  }

  static string RawRole(AutomationElement element) {
    ControlType type = Cached(element, AutomationElement.ControlTypeProperty) as ControlType;
    string raw = type == null ? "ControlType.Custom" : type.ProgrammaticName;
    return raw.StartsWith("ControlType.", StringComparison.Ordinal) ? raw.Substring("ControlType.".Length) : raw;
  }

  static string RoleOf(AutomationElement element, string raw) {
    string localized = (CachedString(element, AutomationElement.LocalizedControlTypeProperty) ?? "").ToLowerInvariant();
    string framework = CachedString(element, AutomationElement.FrameworkIdProperty) ?? "";
    string className = CachedString(element, AutomationElement.ClassNameProperty) ?? "";
    if (localized == "heading" || localized == "заголовок") return "heading";
    if (localized == "dialog" || localized == "диалоговое окно") return "dialog";
    switch (raw) {
      case "Button": case "SplitButton":
        return localized.Contains("switch") ? "switch" : "button";
      case "CheckBox": return "checkbox";
      case "RadioButton": return "radio";
      case "Edit": return "text_field";
      case "Document":
        // Документ Chromium/Firefox — это страница, а не многострочное поле.
        return framework == "Chrome" || framework == "Gecko" || className.StartsWith("Chrome_", StringComparison.Ordinal) ? "web_area" : "text_area";
      case "Hyperlink": return "link";
      case "Menu": return "menu";
      case "MenuItem": return "menu_item";
      case "MenuBar": return "menu_bar";
      case "TabItem": return "tab";
      case "Tab": return "tab_list";
      case "List": return "list";
      case "ListItem": return "list_item";
      case "Tree": return "tree";
      case "TreeItem": return "tree_item";
      case "Table": case "DataGrid": return "table";
      case "DataItem": case "Header": return "row";
      case "HeaderItem": return "cell";
      case "ComboBox": return "combo_box";
      case "Slider": return "slider";
      case "Spinner": return "spin_button";
      case "ScrollBar": return "scroll_bar";
      case "Image": return "image";
      case "Text": return "text";
      case "Window": return CachedBool(element, WindowPattern.IsModalProperty) ? "dialog" : "window";
      case "Pane": return CachedBool(element, AutomationElement.IsScrollPatternAvailableProperty) ? "scroll_area" : "group";
      case "Group": case "StatusBar": case "Calendar": return "group";
      case "ToolBar": return "toolbar";
      case "ProgressBar": return "progress";
      default: return "other";
    }
  }

  static List<object> ActionsOf(AutomationElement element) {
    bool invoke = CachedBool(element, AutomationElement.IsInvokePatternAvailableProperty);
    bool toggle = CachedBool(element, AutomationElement.IsTogglePatternAvailableProperty);
    bool selection = CachedBool(element, AutomationElement.IsSelectionItemPatternAvailableProperty);
    bool expandable = CachedBool(element, AutomationElement.IsExpandCollapsePatternAvailableProperty);
    bool hasValue = CachedBool(element, AutomationElement.IsValuePatternAvailableProperty);
    bool hasRange = CachedBool(element, AutomationElement.IsRangeValuePatternAvailableProperty);
    bool scrollItem = CachedBool(element, AutomationElement.IsScrollItemPatternAvailableProperty);
    bool focusable = CachedBool(element, AutomationElement.IsKeyboardFocusableProperty);
    ExpandCollapseState state = ExpandCollapseState.LeafNode;
    object rawState = Cached(element, ExpandCollapsePattern.ExpandCollapseStateProperty);
    if (rawState is ExpandCollapseState) state = (ExpandCollapseState)rawState;
    bool canExpand = expandable && state != ExpandCollapseState.LeafNode;
    bool valueWritable = hasValue && !CachedBool(element, ValuePattern.IsReadOnlyProperty);
    bool rangeWritable = hasRange && !CachedBool(element, RangeValuePattern.IsReadOnlyProperty);
    List<object> actions = new List<object>();
    if (invoke || toggle || selection || canExpand) actions.Add("press");
    if (focusable) actions.Add("focus");
    if (valueWritable || rangeWritable) actions.Add("set_value");
    if (canExpand) actions.Add("show_menu");
    if (rangeWritable) { actions.Add("increment"); actions.Add("decrement"); }
    if (selection) actions.Add("select");
    if (canExpand && state != ExpandCollapseState.Expanded) actions.Add("expand");
    if (canExpand && state != ExpandCollapseState.Collapsed) actions.Add("collapse");
    if (scrollItem) actions.Add("scroll_into_view");
    return actions;
  }

  // ⚠⚠ ЗНАЧЕНИЕ ЧИТАЕТСЯ ОТДЕЛЬНЫМ ВЫЗОВОМ И ТОЛЬКО У НЕСЕКРЕТНЫХ ЭЛЕМЕНТОВ. В пакетный кэш оно не
  // входит нарочно: иначе содержимое поля пароля пересекало бы границу процесса вместе со всем остальным.
  static string ValueOf(AutomationElement element) {
    if (CachedBool(element, AutomationElement.IsPasswordProperty)) return null;
    if (CachedBool(element, AutomationElement.IsValuePatternAvailableProperty)) {
      try {
        string text = element.GetCurrentPropertyValue(ValuePattern.ValueProperty, false) as string;
        if (!string.IsNullOrEmpty(text)) return text;
      } catch (Exception) { }
    }
    if (CachedBool(element, AutomationElement.IsRangeValuePatternAvailableProperty)) {
      object number = Cached(element, RangeValuePattern.ValueProperty);
      if (number is double) return ((double)number).ToString(CultureInfo.InvariantCulture);
    }
    if (CachedBool(element, AutomationElement.IsTogglePatternAvailableProperty)) {
      object toggle = Cached(element, TogglePattern.ToggleStateProperty);
      if (toggle is ToggleState) {
        ToggleState state = (ToggleState)toggle;
        return state == ToggleState.On ? "on" : state == ToggleState.Off ? "off" : "mixed";
      }
    }
    return null;
  }

  static Dictionary<string, object> Describe(AutomationElement element, string id) {
    string raw = RawRole(element);
    bool secure = CachedBool(element, AutomationElement.IsPasswordProperty);
    Dictionary<string, object> described = new Dictionary<string, object>();
    described["id"] = id;
    described["role"] = RoleOf(element, raw);
    described["raw_role"] = raw;
    described["name"] = CachedString(element, AutomationElement.NameProperty) ?? "";
    // secure: значение не читается и не возвращается НИКОГДА — ни в ответе, ни для поиска по query.
    string value = secure ? null : ValueOf(element);
    if (!string.IsNullOrEmpty(value)) described["value"] = value;
    string description = CachedString(element, AutomationElement.HelpTextProperty);
    if (!string.IsNullOrEmpty(description)) described["description"] = description;
    object rawRect = Cached(element, AutomationElement.BoundingRectangleProperty);
    if (rawRect is System.Windows.Rect && !CachedBool(element, AutomationElement.IsOffscreenProperty)) {
      // Процесс осведомлён о DPI (Per-Monitor-V2), поэтому UIA отдаёт физические пиксели — те же, что у ввода.
      System.Windows.Rect rect = (System.Windows.Rect)rawRect;
      if (!rect.IsEmpty && !double.IsNaN(rect.X) && !double.IsNaN(rect.Y) && !double.IsInfinity(rect.Width) &&
          !double.IsInfinity(rect.Height) && rect.Width > 0 && rect.Height > 0) {
        described["bounds"] = Bounds((int)Math.Round(rect.X), (int)Math.Round(rect.Y), (int)Math.Round(rect.Width), (int)Math.Round(rect.Height));
      }
    }
    described["enabled"] = CachedBool(element, AutomationElement.IsEnabledProperty);
    described["focused"] = CachedBool(element, AutomationElement.HasKeyboardFocusProperty);
    described["secure"] = secure;
    described["actions"] = ActionsOf(element);
    return described;
  }

  static bool Matches(Dictionary<string, object> described, string query) {
    foreach (string key in new string[] { "name", "value", "description" }) {
      object text;
      if (described.TryGetValue(key, out text) && text is string && ((string)text).ToLowerInvariant().Contains(query)) return true;
    }
    return false;
  }

  sealed class Collector {
    public readonly object Gate = new object();
    public readonly List<object> Items = new List<object>();
    public readonly Dictionary<string, AutomationElement> Refs = new Dictionary<string, AutomationElement>();
    public volatile bool Stop;
    public bool Truncated;
    public int Epoch;
    public int Max;
    public int Depth;
    public int Visited;
    public string Query;
    public string Role;
    public Stopwatch Clock;
    public Exception Error;
    public Dictionary<string, object> Window;
  }

  static bool Halted(Collector c) {
    if (c.Stop) return true;
    if (c.Clock.ElapsedMilliseconds > ElementsBudgetMs || c.Visited >= 20000) {
      lock (c.Gate) { c.Truncated = true; c.Stop = true; }
      return true;
    }
    return false;
  }

  static void Add(Collector c, AutomationElement element, Dictionary<string, object> described) {
    lock (c.Gate) {
      if (c.Stop) return;
      if (c.Items.Count >= c.Max) { c.Truncated = true; c.Stop = true; return; }
      string id = "e" + c.Epoch + "." + (c.Items.Count + 1);
      described["id"] = id;
      c.Items.Add(described);
      c.Refs[id] = element;
    }
  }

  static void Walk(Collector c, CacheRequest request, AutomationElement element, int depth) {
    if (Halted(c)) return;
    c.Visited++;
    try {
      Dictionary<string, object> described = Describe(element, "");
      bool roleOk = c.Role == null || (string)described["role"] == c.Role;
      if (roleOk && (c.Query == null || Matches(described, c.Query))) Add(c, element, described);
    } catch (Exception) {
      return;
    }
    if (Halted(c)) return;
    AutomationElement child;
    try { child = TreeWalker.ControlViewWalker.GetFirstChild(element, request); } catch (Exception) { return; }
    if (child != null && depth >= c.Depth) {
      lock (c.Gate) { c.Truncated = true; }
      return;
    }
    while (child != null) {
      Walk(c, request, child, depth + 1);
      if (Halted(c)) return;
      try { child = TreeWalker.ControlViewWalker.GetNextSibling(child, request); } catch (Exception) { child = null; }
    }
  }

  static Dictionary<string, object> WindowInfo(IntPtr hwnd, string fallbackTitle) {
    Dictionary<string, object> info = new Dictionary<string, object>();
    info["id"] = hwnd == IntPtr.Zero ? "" : WindowId(hwnd);
    info["title"] = hwnd == IntPtr.Zero ? (fallbackTitle ?? "") : WindowTitle(hwnd);
    return info;
  }

  /// <summary>Элемент под точкой и цепочка его предков (до 8), самый глубокий первым.</summary>
  static void CollectPoint(Collector c, CacheRequest request, int x, int y) {
    AutomationElement hit = AutomationElement.FromPoint(new System.Windows.Point(x, y));
    if (hit == null) return;
    AutomationElement root = AutomationElement.RootElement;
    AutomationElement current = hit.GetUpdatedCache(request);
    AutomationElement top = null;
    int added = 0;
    for (int guard = 0; current != null && guard < 64; guard++) {
      if (Automation.Compare(current, root)) break;
      if (added < 9) { Add(c, current, Describe(current, "")); added++; }
      top = current;
      AutomationElement parent = null;
      try { parent = TreeWalker.ControlViewWalker.GetParent(current, request); } catch (Exception) { }
      current = parent;
    }
    if (top != null) {
      object handle = Cached(top, AutomationElement.NativeWindowHandleProperty);
      IntPtr hwnd = handle is int ? new IntPtr((int)handle) : IntPtr.Zero;
      c.Window = WindowInfo(hwnd, CachedString(top, AutomationElement.NameProperty));
    }
  }

  static object Elements(Dictionary<string, object> p) {
    int max = OptInt(p, "max", 150, 1, 500);
    int depth = OptInt(p, "depth", 12, 0, 30);
    string query = OptString(p, "query");
    if (query != null) { query = query.Trim().ToLowerInvariant(); if (query.Length == 0) query = null; }
    string role = OptString(p, "role");
    if (role != null) { role = role.Trim().ToLowerInvariant(); if (role.Length == 0) role = null; }
    object rawPoint = Get(p, "point");
    Dictionary<string, object> point = rawPoint as Dictionary<string, object>;
    if (rawPoint != null && point == null) throw Bad("point должен быть объектом {x, y}.");
    int px = 0, py = 0;
    List<IntPtr> roots = new List<IntPtr>();
    if (point != null) {
      px = ReqInt(point, "x");
      py = ReqInt(point, "y");
    } else if (OptString(p, "window_id") != null) {
      roots.Add(WindowHandleOf(OptString(p, "window_id")));
    } else if (Get(p, "pid") != null) {
      int pid = ReqInt(p, "pid");
      foreach (IntPtr hwnd in AppWindows()) {
        uint owner;
        GetWindowThreadProcessId(hwnd, out owner);
        if ((int)owner == pid) roots.Add(hwnd);
      }
      if (roots.Count == 0) throw new ChatRepoDriverError("not_found", "У процесса pid=" + pid + " нет видимых окон приложения. Проверьте pid через windows.");
    } else {
      IntPtr foreground = ForegroundRoot();
      if (foreground == IntPtr.Zero) throw new ChatRepoDriverError("not_found", "Нет окна в фокусе. Укажите window_id или pid из метода windows.");
      roots.Add(foreground);
    }

    Collector c = new Collector();
    lock (ElementGate) {
      epoch++;
      c.Epoch = epoch;
      // Ссылки прежней эпохи больше не действительны: их id дадут stale.
      elementCache = new Dictionary<string, AutomationElement>();
    }
    c.Max = max;
    c.Depth = depth;
    c.Query = query;
    c.Role = role;
    c.Clock = Stopwatch.StartNew();
    bool pointMode = point != null;
    ThreadStart work = delegate {
      try {
        CacheRequest request = BuildRequest();
        if (pointMode) {
          CollectPoint(c, request, px, py);
          return;
        }
        foreach (IntPtr hwnd in roots) {
          if (c.Stop) break;
          if (c.Window == null) c.Window = WindowInfo(hwnd, null);
          AutomationElement element = AutomationElement.FromHandle(hwnd);
          Walk(c, request, element.GetUpdatedCache(request), 0);
        }
      } catch (Exception error) {
        c.Error = error;
      }
    };
    bool finished = RunWorker(work, ElementsBudgetMs + 1500);
    Dictionary<string, object> result = new Dictionary<string, object>();
    lock (c.Gate) {
      // Застрявший поток бросается: всё, что он добавит после этой черты, отбрасывается (Stop).
      if (!finished) { c.Stop = true; c.Truncated = true; }
      if (c.Error != null && c.Items.Count == 0) throw MapUiaError(c.Error);
      lock (ElementGate) {
        if (epoch == c.Epoch) elementCache = new Dictionary<string, AutomationElement>(c.Refs);
      }
      result["epoch"] = c.Epoch;
      result["truncated"] = c.Truncated;
      result["window"] = c.Window;
      result["elements"] = new List<object>(c.Items);
    }
    return result;
  }

  static ChatRepoDriverError MapUiaError(Exception error) {
    ChatRepoDriverError known = error as ChatRepoDriverError;
    if (known != null) return known;
    if (error is ElementNotAvailableException) {
      return new ChatRepoDriverError("not_found", "Элемент интерфейса исчез (окно закрылось или перестроилось). Получите элементы заново.");
    }
    if (error is ElementNotEnabledException) {
      return new ChatRepoDriverError("failed", "Элемент выключен (disabled): приложение сейчас не принимает это действие.");
    }
    if (error is UnauthorizedAccessException) {
      return new ChatRepoDriverError("permission", "Windows не дала доступ к элементам окна: обычно это программа с правами " +
        "администратора (UIPI). Управлять ею может только человек.");
    }
    if (error is InvalidOperationException) {
      return new ChatRepoDriverError("unsupported", "Элемент отказался выполнить действие: " + error.Message);
    }
    return new ChatRepoDriverError("failed", "Ошибка UI Automation: " + error.GetType().Name + ": " + error.Message);
  }

  static AutomationElement CachedElement(string id) {
    if (string.IsNullOrEmpty(id)) throw Bad("Нужен id элемента из метода elements.");
    int dot = id.IndexOf('.');
    int idEpoch;
    if (id[0] != 'e' || dot < 2 || !int.TryParse(id.Substring(1, dot - 1), NumberStyles.None, CultureInfo.InvariantCulture, out idEpoch)) {
      throw Bad("id элемента «" + id + "» не похож на e<эпоха>.<номер> из метода elements.");
    }
    lock (ElementGate) {
      if (idEpoch != epoch) {
        throw new ChatRepoDriverError("stale", "Элемент «" + id + "» из прежней выборки (текущая эпоха " + epoch + "): интерфейс мог " +
          "измениться. Получите элементы заново методом elements.");
      }
      AutomationElement element;
      if (!elementCache.TryGetValue(id, out element)) {
        throw new ChatRepoDriverError("not_found", "Элемента «" + id + "» нет в последней выборке. Получите элементы заново методом elements.");
      }
      return element;
    }
  }

  static ChatRepoDriverError Unsupported(AutomationElement fresh, string action) {
    List<object> actions = ActionsOf(fresh);
    StringBuilder list = new StringBuilder();
    foreach (object one in actions) { if (list.Length > 0) list.Append(", "); list.Append(one); }
    string name = CachedString(fresh, AutomationElement.NameProperty) ?? "";
    return new ChatRepoDriverError("unsupported", "Элемент «" + name + "» (" + RawRole(fresh) + ") не поддерживает «" + action + "». " +
      (actions.Count == 0
        ? "Действий UIA у него нет вовсе: кликните по центру его bounds (locate вернёт свежие координаты)."
        : "Доступно: " + list + "."));
  }

  static string ValueText(object value) {
    if (value is string) return (string)value;
    if (value is bool) return (bool)value ? "true" : "false";
    return Convert.ToString(value, CultureInfo.InvariantCulture);
  }

  static Dictionary<string, object> Wrap(Dictionary<string, object> element) {
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["element"] = element;
    return result;
  }

  /// <summary>
  /// read (протокол 1.1): полный текст TextPattern.DocumentRange (иначе ValuePattern.Value) без обрезания
  /// и выделение TextPattern.GetSelection. caret не отдаётся: дёшево из управляемого UIA его не получить.
  /// </summary>
  static Dictionary<string, object> ReadElement(AutomationElement element, AutomationElement fresh, string id) {
    Dictionary<string, object> result = Wrap(Describe(fresh, id));
    // ⚠⚠ СЕКРЕТНОЕ ПОЛЕ НЕ ЧИТАЕТСЯ НИКОГДА: ни текст, ни выделение. Проверка — ДО любого обращения к паттернам.
    if (CachedBool(fresh, AutomationElement.IsPasswordProperty)) {
      result["text"] = null;
      return result;
    }
    string text = null;
    object pattern;
    if (element.TryGetCurrentPattern(TextPattern.Pattern, out pattern)) {
      TextPattern textPattern = (TextPattern)pattern;
      try { text = textPattern.DocumentRange.GetText(-1); } catch (Exception) { text = null; }
      try {
        System.Windows.Automation.Text.TextPatternRange[] selection = textPattern.GetSelection();
        StringBuilder selected = new StringBuilder();
        foreach (System.Windows.Automation.Text.TextPatternRange range in selection) {
          string part = range.GetText(-1);
          if (string.IsNullOrEmpty(part)) continue;
          if (selected.Length > 0) selected.Append('\n');
          selected.Append(part);
        }
        if (selected.Length > 0) {
          result["selected_text"] = selected.ToString();
        }
      } catch (Exception) { }
    }
    if (text == null && element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) {
      try { text = ((ValuePattern)pattern).Current.Value; } catch (Exception) { text = null; }
    }
    result["text"] = text;
    return result;
  }

  /// <summary>
  /// select_text (протокол 1.1): выделение через TextPattern — подстрока (FindText, n-е вхождение), смещения в символах UIA
  /// (MoveEndpointByUnit) или весь текст. Итог читается обратно из GetSelection; поле пароля не трогается.
  /// </summary>
  static Dictionary<string, object> SelectText(AutomationElement element, string id, Dictionary<string, object> p) {
    AutomationElement fresh = element.GetUpdatedCache(BuildRequest());
    if (CachedBool(fresh, AutomationElement.IsPasswordProperty)) {
      throw new ChatRepoDriverError("secure_field", "Поле пароля: его текст не выделяется и не читается.");
    }
    object pattern;
    if (!element.TryGetCurrentPattern(TextPattern.Pattern, out pattern)) {
      throw new ChatRepoDriverError("unsupported", "У элемента нет TextPattern UI Automation: выделение через доступность невозможно.");
    }
    TextPattern textPattern = (TextPattern)pattern;
    UiaText.TextPatternRange document = textPattern.DocumentRange;
    UiaText.TextPatternRange target = null;
    string needle = OptString(p, "text");
    bool caretOnly = false;
    if (needle != null && needle.Length > 0) {
      int wanted = OptInt(p, "occurrence", 1, 1, int.MaxValue);
      UiaText.TextPatternRange rest = document.Clone();
      int found = 0;
      while (found < wanted) {
        UiaText.TextPatternRange match = rest.FindText(needle, false, false);
        if (match == null) break;
        found++;
        target = match;
        rest.MoveEndpointByRange(UiaText.TextPatternRangeEndpoint.Start, match, UiaText.TextPatternRangeEndpoint.End);
      }
      if (found < wanted) {
        throw new ChatRepoDriverError("not_found", "Фрагмент «" + needle + "» (вхождение " + wanted + ") не найден в тексте элемента; вхождений: " + found + ".");
      }
    } else if (Get(p, "all") is bool && (bool)Get(p, "all")) {
      target = document.Clone();
      // В пустом поле «выделить всё» даёт пустое выделение — это и есть верный итог, а не отказ приложения.
      caretOnly = document.GetText(-1).Length == 0;
    } else {
      int start = ReqInt(p, "start");
      int end = OptInt(p, "end", start, start, int.MaxValue);
      caretOnly = end == start;
      target = document.Clone();
      // Схлопнуть диапазон в начало документа, сдвинуть начало (конец идёт за ним) и растянуть конец.
      target.MoveEndpointByRange(UiaText.TextPatternRangeEndpoint.End, document, UiaText.TextPatternRangeEndpoint.Start);
      if (start > 0 && target.MoveEndpointByUnit(UiaText.TextPatternRangeEndpoint.Start, UiaText.TextUnit.Character, start) != start) {
        throw Bad("start за пределами текста.");
      }
      if (end > start && target.MoveEndpointByUnit(UiaText.TextPatternRangeEndpoint.End, UiaText.TextUnit.Character, end - start) != end - start) {
        throw Bad("end за пределами текста.");
      }
    }
    target.Select();
    UiaText.TextPatternRange[] selection = textPattern.GetSelection();
    if (selection == null || selection.Length == 0) {
      throw new ChatRepoDriverError("failed", "Приложение не применило выделение: после запроса выделения нет.");
    }
    string selected = selection[0].GetText(-1);
    if (needle != null && needle.Length > 0 ? string.CompareOrdinal(selected, needle) != 0 : (selected.Length == 0) != caretOnly) {
      throw new ChatRepoDriverError("failed", "Приложение не применило выделение: выделено не то, что запрошено.");
    }
    // Начало — длина текста от начала документа до начала выделения.
    UiaText.TextPatternRange head = textPattern.DocumentRange;
    head.MoveEndpointByRange(UiaText.TextPatternRangeEndpoint.End, selection[0], UiaText.TextPatternRangeEndpoint.Start);
    int startOffset = head.GetText(-1).Length;
    Dictionary<string, object> described = null;
    try { described = Describe(element.GetUpdatedCache(BuildRequest()), id); } catch (ElementNotAvailableException) { }
    Dictionary<string, object> result = Wrap(described);
    Dictionary<string, object> range = new Dictionary<string, object>();
    range["start"] = startOffset;
    range["end"] = startOffset + selected.Length;
    result["selection"] = range;
    result["selected_text"] = selected;
    return result;
  }

  static Dictionary<string, object> PerformElementAction(AutomationElement element, string id, string action, object value) {
    CacheRequest request = BuildRequest();
    AutomationElement fresh = element.GetUpdatedCache(request);
    if (action == "locate") return Wrap(Describe(fresh, id));
    if (action == "read") return ReadElement(element, fresh, id);
    object pattern;
    switch (action) {
      case "press":
        if (element.TryGetCurrentPattern(InvokePattern.Pattern, out pattern)) ((InvokePattern)pattern).Invoke();
        else if (element.TryGetCurrentPattern(TogglePattern.Pattern, out pattern)) ((TogglePattern)pattern).Toggle();
        else if (element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)) ((SelectionItemPattern)pattern).Select();
        else if (element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out pattern)) {
          ExpandCollapsePattern expand = (ExpandCollapsePattern)pattern;
          if (expand.Current.ExpandCollapseState == ExpandCollapseState.LeafNode) throw Unsupported(fresh, action);
          if (expand.Current.ExpandCollapseState == ExpandCollapseState.Expanded) expand.Collapse(); else expand.Expand();
        }
        else throw Unsupported(fresh, action);
        break;
      case "focus":
        if (!CachedBool(fresh, AutomationElement.IsKeyboardFocusableProperty)) throw Unsupported(fresh, action);
        element.SetFocus();
        break;
      case "set_value":
        if (element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) {
          ValuePattern valuePattern = (ValuePattern)pattern;
          if (valuePattern.Current.IsReadOnly) throw Unsupported(fresh, action);
          valuePattern.SetValue(ValueText(value));
        } else if (element.TryGetCurrentPattern(RangeValuePattern.Pattern, out pattern)) {
          RangeValuePattern range = (RangeValuePattern)pattern;
          if (range.Current.IsReadOnly) throw Unsupported(fresh, action);
          double number;
          if (!ToDouble(value, out number)) throw Bad("Для ползунка value должно быть числом.");
          if (number < range.Current.Minimum || number > range.Current.Maximum) {
            throw Bad("value вне диапазона элемента: от " + range.Current.Minimum.ToString(CultureInfo.InvariantCulture) +
              " до " + range.Current.Maximum.ToString(CultureInfo.InvariantCulture) + ".");
          }
          range.SetValue(number);
        } else {
          throw Unsupported(fresh, action);
        }
        break;
      case "show_menu":
        // Контекстного меню (AXShowMenu macOS) в управляемом UIA нет; раскрытие — ближайший честный аналог.
        if (!element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out pattern)) throw Unsupported(fresh, action);
        if (((ExpandCollapsePattern)pattern).Current.ExpandCollapseState == ExpandCollapseState.LeafNode) throw Unsupported(fresh, action);
        ((ExpandCollapsePattern)pattern).Expand();
        break;
      case "increment":
      case "decrement":
        if (!element.TryGetCurrentPattern(RangeValuePattern.Pattern, out pattern)) throw Unsupported(fresh, action);
        RangeValuePattern stepper = (RangeValuePattern)pattern;
        if (stepper.Current.IsReadOnly) throw Unsupported(fresh, action);
        double step = stepper.Current.SmallChange > 0 ? stepper.Current.SmallChange : 1;
        double next = stepper.Current.Value + (action == "increment" ? step : -step);
        next = Math.Max(stepper.Current.Minimum, Math.Min(stepper.Current.Maximum, next));
        stepper.SetValue(next);
        break;
      case "select":
        if (!element.TryGetCurrentPattern(SelectionItemPattern.Pattern, out pattern)) throw Unsupported(fresh, action);
        ((SelectionItemPattern)pattern).Select();
        break;
      case "expand":
      case "collapse":
        if (!element.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out pattern)) throw Unsupported(fresh, action);
        if (((ExpandCollapsePattern)pattern).Current.ExpandCollapseState == ExpandCollapseState.LeafNode) throw Unsupported(fresh, action);
        if (action == "expand") ((ExpandCollapsePattern)pattern).Expand(); else ((ExpandCollapsePattern)pattern).Collapse();
        break;
      case "scroll_into_view":
        if (!element.TryGetCurrentPattern(ScrollItemPattern.Pattern, out pattern)) throw Unsupported(fresh, action);
        ((ScrollItemPattern)pattern).ScrollIntoView();
        break;
      default:
        throw Bad("Неизвестное действие с элементом «" + action + "».");
    }
    Thread.Sleep(60);
    try {
      return Wrap(Describe(element.GetUpdatedCache(request), id));
    } catch (ElementNotAvailableException) {
      // Элемент мог исчезнуть из-за самого действия (закрытое меню, нажатая «ОК»): это успех, а не ошибка.
      return Wrap(null);
    }
  }

  static readonly string[] ElementActions = new string[] {
    "press", "focus", "set_value", "show_menu", "increment", "decrement", "select", "expand", "collapse", "scroll_into_view", "locate", "read", "select_text"
  };

  static object ElementAction(Dictionary<string, object> p) {
    string action = OptString(p, "action");
    if (action == null || Array.IndexOf(ElementActions, action) < 0) {
      throw Bad("element_action: action должен быть одним из: " + string.Join(", ", ElementActions) + ".");
    }
    object value = Get(p, "value");
    if (action == "set_value" && value == null) throw Bad("set_value: нужно поле value.");
    string id = OptString(p, "id");
    AutomationElement element = CachedElement(id);
    Dictionary<string, object> outcome = null;
    Exception failure = null;
    ThreadStart work = delegate {
      try {
        outcome = action == "select_text" ? SelectText(element, id, p) : PerformElementAction(element, id, action, value);
      } catch (Exception error) { failure = error; }
    };
    if (!RunWorker(work, ActionBudgetMs)) {
      if (action == "press") {
        // ⚠ InvokePattern.Invoke у кнопки Win32 не возвращается, пока открытое ею модальное окно
        // не закроют. Нажатие, скорее всего, произошло: говорим об этом прямо, а не «ошибка».
        Dictionary<string, object> pending = Wrap(null);
        pending["note"] = "Нажатие отправлено, но приложение не ответило за 5 с — так ведёт себя кнопка, открывшая " +
          "модальное окно, или зависшее приложение. Посмотрите на экран.";
        return pending;
      }
      throw new ChatRepoDriverError("failed", "Приложение не ответило за 5 с; результат действия «" + action + "» неизвестен. Посмотрите на экран.");
    }
    if (failure != null) throw MapUiaError(failure);
    return outcome;
  }

  // ───────────────────────────── focused ─────────────────────────────

  static object Focused() {
    Dictionary<string, object> described = null;
    int pid = 0;
    Exception failure = null;
    ThreadStart work = delegate {
      try {
        AutomationElement element = AutomationElement.FocusedElement;
        if (element == null) return;
        AutomationElement cached = element.GetUpdatedCache(BuildRequest());
        described = Describe(cached, "");
        object rawPid = Cached(cached, AutomationElement.ProcessIdProperty);
        if (rawPid is int) pid = (int)rawPid;
      } catch (ElementNotAvailableException) {
        described = null;
      } catch (Exception error) {
        failure = error;
      }
    };
    if (!RunWorker(work, ElementsBudgetMs)) {
      throw new ChatRepoDriverError("failed", "Окно в фокусе не ответило UI Automation за 3 с (приложение, возможно, зависло).");
    }
    if (failure != null && !(failure is UnauthorizedAccessException)) throw MapUiaError(failure);
    if (pid == 0) {
      IntPtr foreground = GetForegroundWindow();
      uint owner = 0;
      if (foreground != IntPtr.Zero) GetWindowThreadProcessId(foreground, out owner);
      pid = (int)owner;
    }
    Dictionary<string, object> app = new Dictionary<string, object>();
    app["name"] = pid == 0 ? "" : ProcessName((uint)pid, null);
    app["pid"] = pid;
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["element"] = described;
    result["app"] = app;
    return result;
  }

  // ───────────────────────────── ocr (мост в PowerShell) ─────────────────────────────

  public const string OcrSentinel = "\u0002__CHATREPO_OCR__\u0002";
  static object ocrId;
  static string ocrImage;
  static string[] ocrLangs;
  static List<object> ocrLines;

  /// <summary>Разобрать и проверить запрос ocr, сохранить параметры, вернуть маркер для цикла PowerShell.</summary>
  static string PrepareOcr(object id, Dictionary<string, object> p) {
    if (!ocrAvailable) {
      throw new ChatRepoDriverError("unsupported", "Распознавание текста недоступно: в этой системе Windows нет установленных " +
        "языковых пакетов OCR. Добавьте язык в «Параметры → Время и язык → Язык» (компонент «Распознавание текста»).");
    }
    string image = Get(p, "image_b64") as string;
    if (string.IsNullOrEmpty(image)) throw Bad("ocr: нужно непустое поле image_b64 (PNG или JPEG в base64).");
    List<string> langs = new List<string>();
    foreach (object one in OptList(p, "languages")) {
      string tag = one as string;
      if (tag != null && tag.Trim().Length > 0) langs.Add(tag.Trim());
    }
    ocrId = id;
    ocrImage = image;
    ocrLangs = langs.ToArray();
    return OcrSentinel;
  }

  public static string OcrImage() { return ocrImage; }
  public static string[] OcrLangs() { return ocrLangs == null ? new string[0] : ocrLangs; }
  public static void OcrBegin() { ocrLines = new List<object>(); }

  /// <summary>Одна строка: bounds — объединение боксов слов в пикселях переданной картинки; confidence всегда null.</summary>
  public static void OcrAddLine(string text, int x, int y, int width, int height) {
    if (ocrLines == null) ocrLines = new List<object>();
    Dictionary<string, object> line = new Dictionary<string, object>();
    line["text"] = text;
    line["confidence"] = null;
    if (width > 0 && height > 0) line["bounds"] = Bounds(x, y, width, height);
    ocrLines.Add(line);
  }

  public static string OcrFinish(string text, string engine, string note) {
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["text"] = text == null ? "" : text;
    result["engine"] = engine;
    result["lines"] = ocrLines == null ? new List<object>() : ocrLines;
    if (!string.IsNullOrEmpty(note)) result["note"] = note;
    string reply = Reply(ocrId, "result", result);
    ocrLines = null;
    return reply;
  }

  public static string OcrFail(string code, string message) {
    ocrLines = null;
    return Reply(ocrId, "error", ErrorBody(code, message));
  }

  // ───────────────────────────── clipboard_files ─────────────────────────────

  delegate object ClipJob();
  static object clipResult;
  static Exception clipError;

  // ⚠ Буфер обмена — только из STA-потока. Отдельный поток с таймаутом: чужое приложение может
  // держать буфер, и без предела помощник бы завис.
  static object RunClipboard(ClipJob job) {
    clipResult = null;
    clipError = null;
    Thread thread = new Thread(delegate() {
      try { clipResult = job(); } catch (Exception error) { clipError = error; }
    });
    thread.IsBackground = true;
    thread.SetApartmentState(ApartmentState.STA);
    thread.Start();
    if (!thread.Join(5000)) {
      throw new ChatRepoDriverError("failed", "Буфер обмена не ответил за 5 с: его удерживает другое приложение. Повторите позже.");
    }
    if (clipError != null) {
      ChatRepoDriverError known = clipError as ChatRepoDriverError;
      if (known != null) throw known;
      throw new ChatRepoDriverError("failed", "Не удалось обратиться к буферу обмена: " + clipError.Message);
    }
    return clipResult;
  }

  static bool IsAbsolutePath(string path) {
    if (string.IsNullOrEmpty(path)) return false;
    if (path.Length >= 3 && char.IsLetter(path[0]) && path[1] == ':' && (path[2] == '\\' || path[2] == '/')) return true;
    if (path.StartsWith("\\\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal)) return true;
    return false;
  }

  static object ClipboardFiles(Dictionary<string, object> p) {
    string action = OptString(p, "action");
    if (action == "get") {
      ClipJob job = delegate {
        System.Collections.Specialized.StringCollection files = null;
        // Короткая повторная попытка: OpenClipboard мог быть занят другим процессом миг назад.
        for (int attempt = 0; ; attempt++) {
          try { files = System.Windows.Forms.Clipboard.GetFileDropList(); break; }
          catch (System.Runtime.InteropServices.ExternalException) { if (attempt >= 4) throw; Thread.Sleep(60); }
        }
        List<object> paths = new List<object>();
        if (files != null) { foreach (string one in files) paths.Add(one); }
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["paths"] = paths;
        return result;
      };
      return RunClipboard(job);
    }
    if (action == "set") {
      System.Collections.Specialized.StringCollection collection = new System.Collections.Specialized.StringCollection();
      int count = 0;
      foreach (object one in OptList(p, "paths")) {
        string path = one as string;
        if (path == null) throw Bad("paths должен быть массивом строк-путей.");
        if (!IsAbsolutePath(path)) throw Bad("Путь «" + path + "» не абсолютный. Нужен полный путь вида C:\\Users\\… или \\\\сервер\\доля\\….");
        if (!File.Exists(path) && !Directory.Exists(path)) throw new ChatRepoDriverError("not_found", "Файл или папка «" + path + "» не существует.");
        collection.Add(path);
        count++;
      }
      if (count == 0) throw Bad("paths пуст: укажите хотя бы один файл.");
      int total = count;
      ClipJob job = delegate {
        System.Windows.Forms.DataObject data = new System.Windows.Forms.DataObject();
        data.SetFileDropList(collection);
        // ⚠ Preferred DropEffect = DROPEFFECT_COPY (1): без него Проводник при вставке МОЖЕТ ПЕРЕМЕСТИТЬ
        // файлы, а не скопировать. Значение — DWORD в little-endian через CF_HDROP-совместимый формат.
        MemoryStream effect = new MemoryStream();
        effect.Write(new byte[] { 1, 0, 0, 0 }, 0, 4);
        data.SetData("Preferred DropEffect", effect);
        // Встроенный повтор SetDataObject: 5 попыток с паузой 100 мс, если буфер занят.
        System.Windows.Forms.Clipboard.SetDataObject(data, true, 5, 100);
        Dictionary<string, object> result = new Dictionary<string, object>();
        result["count"] = total;
        return result;
      };
      return RunClipboard(job);
    }
    throw Bad("clipboard_files: action должен быть get или set.");
  }

  // ───────────────────────────── app_at ─────────────────────────────

  static bool RectContains(RECT rect, int x, int y) {
    return x >= rect.Left && x < rect.Right && y >= rect.Top && y < rect.Bottom;
  }

  static object AppAt(Dictionary<string, object> p) {
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    POINT point;
    point.X = x;
    point.Y = y;
    IntPtr target = IntPtr.Zero;
    // Верхнее окно под точкой; если это не обычное окно приложения — идём вниз по Z-порядку.
    IntPtr under = WindowFromPoint(point);
    if (under != IntPtr.Zero) {
      IntPtr root = GetAncestor(under, GA_ROOT);
      if (root == IntPtr.Zero) root = under;
      if (IsAppWindow(root)) target = root;
    }
    if (target == IntPtr.Zero) {
      // AppWindows() уже в Z-порядке спереди назад: первое обычное окно, накрывающее точку.
      foreach (IntPtr hwnd in AppWindows()) {
        RECT rect;
        if (VisibleRect(hwnd, out rect) && RectContains(rect, x, y)) { target = hwnd; break; }
      }
    }
    Dictionary<string, object> result = new Dictionary<string, object>();
    if (target == IntPtr.Zero) {
      result["app"] = null;
      result["window_id"] = null;
      return result;
    }
    uint pid;
    GetWindowThreadProcessId(target, out pid);
    Dictionary<string, object> app = new Dictionary<string, object>();
    app["name"] = ProcessName(pid, null);
    app["pid"] = (int)pid;
    result["app"] = app;
    result["window_id"] = WindowId(target);
    return result;
  }

  // ───────────────────────────── background_input ─────────────────────────────

  const string BackgroundNote = "часть приложений (Chromium/DirectX/UWP) фоновые сообщения игнорирует — проверьте снимком";

  static Dictionary<string, object> Delivered(string note) {
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["delivered"] = true;
    result["method"] = "PostMessage";
    result["note"] = string.IsNullOrEmpty(note) ? BackgroundNote : (BackgroundNote + "; " + note);
    return result;
  }

  static IntPtr MakeLParam(int low, int high) {
    return new IntPtr((high << 16) | (low & 0xFFFF));
  }

  static void Post(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam) {
    if (!PostMessageW(hwnd, msg, wParam, lParam)) {
      int error = Marshal.GetLastWin32Error();
      if (error == 5) {
        throw new ChatRepoDriverError("blocked", "Windows не пропустила фоновое сообщение в окно (UIPI): целевое приложение " +
          "запущено от имени администратора. Управлять им может только человек.");
      }
      throw new ChatRepoDriverError("failed", "PostMessage в окно не удался (код " + error + "). Окно, возможно, закрылось.");
    }
  }

  // lParam для WM_KEYDOWN/WM_KEYUP: счётчик повторов = 1, скан-код, бит расширенной клавиши,
  // для отпускания — биты предыдущего состояния и перехода (0xC0000000).
  static IntPtr KeyLParam(ushort vk, bool extended, bool up) {
    uint scan = MapVirtualKeyW(vk, 0) & 0xFF;
    uint value = 1u | (scan << 16);
    if (extended) value |= 0x01000000u;
    if (up) value |= 0xC0000000u;
    return new IntPtr(unchecked((int)value));
  }

  static IntPtr FocusTarget(IntPtr hwnd) {
    uint pid;
    uint thread = GetWindowThreadProcessId(hwnd, out pid);
    GUITHREADINFO info = new GUITHREADINFO();
    info.cbSize = Marshal.SizeOf(typeof(GUITHREADINFO));
    if (GetGUIThreadInfo(thread, ref info) && info.hwndFocus != IntPtr.Zero) return info.hwndFocus;
    return hwnd;
  }

  static IntPtr DeepestChild(IntPtr root, int screenX, int screenY) {
    IntPtr current = root;
    for (int guard = 0; guard < 32; guard++) {
      POINT pt;
      pt.X = screenX;
      pt.Y = screenY;
      ScreenToClient(current, ref pt);
      IntPtr child = ChildWindowFromPointEx(current, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT);
      if (child == IntPtr.Zero || child == current) break;
      current = child;
    }
    return current;
  }

  static object BackgroundInput(Dictionary<string, object> p) {
    IntPtr hwnd = WindowHandleOf(OptString(p, "window_id"));
    string action = OptString(p, "action");
    if (action == null) throw Bad("background_input: нужно поле action (type, key, click).");
    if (action == "type") {
      string text = Get(p, "text") as string;
      if (text == null) throw Bad("background_input type: нужно строковое поле text.");
      IntPtr focus = FocusTarget(hwnd);
      for (int i = 0; i < text.Length; i++) {
        char c = text[i];
        if (c == '\r' || c == '\n') {
          if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n') i++;
          Post(focus, WM_KEYDOWN, new IntPtr(VK_RETURN), KeyLParam(VK_RETURN, false, false));
          Post(focus, WM_KEYUP, new IntPtr(VK_RETURN), KeyLParam(VK_RETURN, false, true));
        } else {
          // WM_CHAR несёт готовую UTF-16 единицу: суррогатные пары уходят двумя сообщениями сами собой.
          Post(focus, WM_CHAR, new IntPtr((int)c), new IntPtr(1));
        }
      }
      return Delivered(null);
    }
    if (action == "key") {
      List<KeySpec> implied = new List<KeySpec>();
      KeySpec key = KeyOf(OptString(p, "key"), implied);
      IntPtr focus = FocusTarget(hwnd);
      Post(focus, WM_KEYDOWN, new IntPtr(key.Vk), KeyLParam(key.Vk, key.Extended, false));
      Post(focus, WM_KEYUP, new IntPtr(key.Vk), KeyLParam(key.Vk, key.Extended, true));
      // ⚠ МОДИФИКАТОРЫ ФОНОВЫМ ВВОДОМ НЕ ПЕРЕДАТЬ. Состояние Shift/Ctrl/Alt хранит очередь ввода потока,
      // а PostMessage её не трогает: комбинация и системные сочетания в фоне не работают.
      List<KeySpec> modifiers = ModifiersOf(p);
      // Символу нужен Shift/AltGr на этой раскладке — в фоне он тоже не передаётся.
      foreach (KeySpec one in implied) AddUnique(modifiers, one);
      string note = modifiers.Count > 0
        ? "модификаторы в фоновом режиме не передаются (Shift/Ctrl/Alt и системные сочетания не сработают) — для комбинаций используйте обычный input с фокусом"
        : null;
      return Delivered(note);
    }
    if (action == "click") {
      int x = ReqInt(p, "x"), y = ReqInt(p, "y");
      string rawButton = OptString(p, "button");
      string button = rawButton == null ? "left" : rawButton.Trim().ToLowerInvariant();
      int clicks = OptInt(p, "clicks", 1, 1, int.MaxValue);
      uint down, up, dbl;
      int mk;
      if (button == "right") { down = WM_RBUTTONDOWN; up = WM_RBUTTONUP; dbl = WM_RBUTTONDBLCLK; mk = MK_RBUTTON; }
      else if (button == "middle") { down = WM_MBUTTONDOWN; up = WM_MBUTTONUP; dbl = WM_MBUTTONDBLCLK; mk = MK_MBUTTON; }
      else if (button == "left") { down = WM_LBUTTONDOWN; up = WM_LBUTTONUP; dbl = WM_LBUTTONDBLCLK; mk = MK_LBUTTON; }
      else throw Bad("background_input click: кнопка «" + rawButton + "» не поддержана в фоне (только left, right, middle).");
      IntPtr child = DeepestChild(hwnd, x, y);
      POINT client;
      client.X = x;
      client.Y = y;
      ScreenToClient(child, ref client);
      IntPtr lParam = MakeLParam(client.X, client.Y);
      IntPtr wParam = new IntPtr(mk);
      for (int i = 0; i < clicks; i++) {
        // Второй щелчок двойного клика — через WM_*BUTTONDBLCLK, как это делает система.
        uint downMsg = (i == 1) ? dbl : down;
        Post(child, downMsg, wParam, lParam);
        Post(child, up, IntPtr.Zero, lParam);
      }
      return Delivered(null);
    }
    throw Bad("background_input: неизвестное действие «" + action + "» (type, key, click).");
  }

  // ───────────────────────────── touch ─────────────────────────────

  static readonly object touchGate = new object();
  static bool touchInitDone;
  static bool touchInitOk;

  static bool EnsureTouch() {
    lock (touchGate) {
      if (!touchInitDone) {
        touchInitDone = true;
        try { touchInitOk = InitializeTouchInjection(2, TOUCH_FEEDBACK_DEFAULT); }
        catch (Exception) { touchInitOk = false; }
      }
      return touchInitOk;
    }
  }

  static POINTER_TOUCH_INFO Contact(uint id, int x, int y, uint flags) {
    POINTER_TOUCH_INFO c = new POINTER_TOUCH_INFO();
    c.pointerInfo.pointerType = PT_TOUCH;
    c.pointerInfo.pointerId = id;
    c.pointerInfo.pointerFlags = flags;
    c.pointerInfo.ptPixelLocation.X = x;
    c.pointerInfo.ptPixelLocation.Y = y;
    c.touchFlags = 0;
    c.touchMask = TOUCH_MASK_CONTACTAREA | TOUCH_MASK_ORIENTATION | TOUCH_MASK_PRESSURE;
    // Пятно касания ±2 px, давление 32000, ориентация 90 — как советует InjectTouchInput.
    c.rcContact.Left = x - 2;
    c.rcContact.Top = y - 2;
    c.rcContact.Right = x + 2;
    c.rcContact.Bottom = y + 2;
    c.orientation = 90;
    c.pressure = 32000;
    return c;
  }

  const uint TouchDown = POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_DOWN;
  const uint TouchUpdate = POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT | POINTER_FLAG_UPDATE;
  const uint TouchUp = POINTER_FLAG_UP;

  static void Inject(POINTER_TOUCH_INFO[] contacts) {
    if (!InjectTouchInput((uint)contacts.Length, contacts)) {
      int error = Marshal.GetLastWin32Error();
      throw new ChatRepoDriverError("unsupported", "InjectTouchInput отклонён (код " + error + "): у этой системы нет поддержки " +
        "внедрения касаний. Сенсорные жесты доступны только на Windows с сенсорным вводом.");
    }
  }

  static void CheckTouchPoint(int x, int y) {
    POINT point;
    point.X = x;
    point.Y = y;
    if (MonitorFromPoint(point, 0) == IntPtr.Zero) throw Bad("Точка касания (" + x + ", " + y + ") вне всех экранов.");
  }

  static object Touch(Dictionary<string, object> p) {
    string gesture = OptString(p, "gesture");
    if (gesture == null) throw Bad("touch: нужно поле gesture (tap, double_tap, long_press, swipe, pinch, rotate).");
    if (!IsTouchInjectionOs()) {
      throw new ChatRepoDriverError("unsupported", "Внедрение касаний недоступно: нужна Windows 8 или новее.");
    }
    if (!EnsureTouch()) {
      throw new ChatRepoDriverError("unsupported", "InitializeTouchInjection не удалась (код " + Marshal.GetLastWin32Error() + "): " +
        "в этой системе нет поддержки внедрения касаний.");
    }
    int x = ReqInt(p, "x"), y = ReqInt(p, "y");
    CheckTouchPoint(x, y);
    switch (gesture) {
      case "tap": Tap(x, y); break;
      case "double_tap":
        Tap(x, y);
        Thread.Sleep(120);
        Tap(x, y);
        break;
      case "long_press": {
        int hold = OptInt(p, "duration_ms", 800, 0, int.MaxValue);
        POINTER_TOUCH_INFO[] one = new POINTER_TOUCH_INFO[] { Contact(0, x, y, TouchDown) };
        Inject(one);
        // Контакт нужно поддерживать кадрами UPDATE, иначе система сама его отпустит.
        Stopwatch clock = Stopwatch.StartNew();
        while (clock.ElapsedMilliseconds < hold) {
          Thread.Sleep(Math.Min(80, hold));
          one[0] = Contact(0, x, y, TouchUpdate);
          Inject(one);
        }
        one[0] = Contact(0, x, y, TouchUp);
        Inject(one);
        break;
      }
      case "swipe": {
        int toX = ReqInt(p, "toX"), toY = ReqInt(p, "toY");
        CheckTouchPoint(toX, toY);
        int duration = OptInt(p, "duration_ms", 300, 0, int.MaxValue);
        int steps = Math.Max(1, (int)((long)duration * 60 / 1000));
        POINTER_TOUCH_INFO[] one = new POINTER_TOUCH_INFO[] { Contact(0, x, y, TouchDown) };
        Inject(one);
        int pause = duration / steps;
        for (int i = 1; i <= steps; i++) {
          int nx = x + (int)Math.Round((toX - x) * (double)i / steps);
          int ny = y + (int)Math.Round((toY - y) * (double)i / steps);
          one[0] = Contact(0, nx, ny, TouchUpdate);
          Inject(one);
          if (pause > 0) Thread.Sleep(pause);
        }
        one[0] = Contact(0, toX, toY, TouchUp);
        Inject(one);
        break;
      }
      case "pinch": {
        double scale;
        if (!ToDouble(Get(p, "scale"), out scale)) throw Bad("pinch: нужно число scale (положительное; меньше 1 — свести пальцы).");
        if (scale <= 0) throw Bad("pinch: scale должно быть положительным.");
        int duration = OptInt(p, "duration_ms", 300, 0, int.MaxValue);
        TwoContactGesture(x, y, 100.0, 0.0, 100.0 * scale, 0.0, duration);
        break;
      }
      case "rotate": {
        double angle;
        if (!ToDouble(Get(p, "angle"), out angle)) throw Bad("rotate: нужно число angle (в градусах).");
        int duration = OptInt(p, "duration_ms", 300, 0, int.MaxValue);
        // Радиус 100 px, поворот от 0 до angle.
        TwoContactGesture(x, y, 100.0, 0.0, 100.0, angle, duration);
        break;
      }
      default:
        throw Bad("touch: неизвестный жест «" + gesture + "» (tap, double_tap, long_press, swipe, pinch, rotate).");
    }
    Dictionary<string, object> result = new Dictionary<string, object>();
    result["emulated"] = false;
    return result;
  }

  static void Tap(int x, int y) {
    POINTER_TOUCH_INFO[] one = new POINTER_TOUCH_INFO[] { Contact(0, x, y, TouchDown) };
    Inject(one);
    one[0] = Contact(0, x, y, TouchUp);
    Inject(one);
  }

  // Два контакта, симметричных относительно центра: радиус меняется от startRadius к endRadius,
  // угол — от startAngle к startAngle+deltaAngle (градусы). Годится и для pinch, и для rotate.
  static void TwoContactGesture(int cx, int cy, double startRadius, double startAngleDeg, double endRadius, double deltaAngleDeg, int duration) {
    int steps = Math.Max(1, (int)((long)duration * 60 / 1000));
    POINTER_TOUCH_INFO[] both = new POINTER_TOUCH_INFO[2];
    PlaceTwo(both, cx, cy, startRadius, startAngleDeg, TouchDown, TouchDown);
    Inject(both);
    int pause = duration / steps;
    for (int i = 1; i <= steps; i++) {
      double t = (double)i / steps;
      double radius = startRadius + (endRadius - startRadius) * t;
      double angle = startAngleDeg + deltaAngleDeg * t;
      PlaceTwo(both, cx, cy, radius, angle, TouchUpdate, TouchUpdate);
      Inject(both);
      if (pause > 0) Thread.Sleep(pause);
    }
    double finalRadius = endRadius;
    double finalAngle = startAngleDeg + deltaAngleDeg;
    PlaceTwo(both, cx, cy, finalRadius, finalAngle, TouchUp, TouchUp);
    Inject(both);
  }

  static void PlaceTwo(POINTER_TOUCH_INFO[] both, int cx, int cy, double radius, double angleDeg, uint flagsA, uint flagsB) {
    double radians = angleDeg * Math.PI / 180.0;
    int dx = (int)Math.Round(Math.Cos(radians) * radius);
    int dy = (int)Math.Round(Math.Sin(radians) * radius);
    both[0] = Contact(0, cx + dx, cy + dy, flagsA);
    both[1] = Contact(1, cx - dx, cy - dy, flagsB);
  }
}
'@

# ⚠ Сборки — по ПОЛНЫМ именам .NET 4: частичное имя может подтянуть UIAutomationClient 3.0 из
# GAC CLR2, и тогда типы WindowsBase двух версий не сойдутся при компиляции.
$compileError = $null
try {
  $references = @()
  foreach ($assemblyName in @(
      'UIAutomationClient, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35',
      'UIAutomationTypes, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35',
      'WindowsBase, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35',
      'System.Web.Extensions, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35',
      'System.Windows.Forms, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089',
      'System.Drawing, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a')) {
    $references += [System.Reflection.Assembly]::Load($assemblyName).Location
  }
  # Единственная компиляция за жизнь помощника. -IgnoreWarnings: предупреждение компилятора не должно
  # превращаться в отказ всей программы у человека.
  Add-Type -TypeDefinition $driverSource -ReferencedAssemblies $references -Language CSharp -IgnoreWarnings
} catch {
  $compileError = [string]$_.Exception.Message
}

if ($null -ne $compileError) {
  # ⚠ Помощник не падает молча: stderr никто не читает, поэтому на каждый запрос отвечаем честной
  # ошибкой с текстом компилятора — иначе клиент ждал бы ответа вечно и не узнал бы причину.
  $message = 'Помощник Windows не скомпилировался (Add-Type): ' + $compileError
  if ($message.Length -gt 1500) { $message = $message.Substring(0, 1500) }
  while ($null -ne ($line = [Console]::In.ReadLine())) {
    if ($line.Trim().Length -eq 0) { continue }
    $requestId = $null
    try { $requestId = ($line | ConvertFrom-Json).id } catch {}
    $reply = [ordered]@{ id = $requestId; error = [ordered]@{ code = 'failed'; message = $message } }
    [Console]::Out.WriteLine(($reply | ConvertTo-Json -Compress -Depth 4))
    [Console]::Out.Flush()
  }
  return
}

# DPI-осведомлённость объявляется здесь, до первого запроса и до любого вызова ввода или UIA.
[ChatRepoDriver]::Init()

# ─────────────────────── OCR через WinRT (Windows.Media.Ocr) ───────────────────────
# ⚠ OCR ЖИВЁТ В POWERSHELL, А НЕ В C#. Windows.Media.Ocr — это WinRT, чьи метаданные легаси-компилятор
# CodeDOM (C# 5) не подключает. PowerShell 5.1 умеет грузить WinRT-типы и ждать IAsyncOperation через
# System.WindowsRuntimeSystemExtensions.AsTask — это и есть общепринятый путь.
$script:ocrAsTask = $null
function Get-OcrAsTask {
  if ($null -ne $script:ocrAsTask) { return $script:ocrAsTask }
  [void][System.Reflection.Assembly]::Load('System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089')
  $script:ocrAsTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  } | Select-Object -First 1
  return $script:ocrAsTask
}
function Wait-OcrOp($operation, [Type]$resultType) {
  $asTask = Get-OcrAsTask
  $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
  [void]$task.Wait(-1)
  return $task.Result
}

# Проба при старте: доступен ли хоть один OCR-движок. Дёшево и без системных окон.
$script:ocrReady = $false
try {
  $probe = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]::TryCreateFromUserProfileLanguages()
  if ($null -ne $probe) { $script:ocrReady = $true }
} catch { $script:ocrReady = $false }
[ChatRepoDriver]::SetOcrAvailable($script:ocrReady)

function Invoke-ChatRepoOcr {
  try {
    $bytes = [Convert]::FromBase64String([ChatRepoDriver]::OcrImage())
    if ($bytes.Length -eq 0) { return [ChatRepoDriver]::OcrFail('bad_request', 'ocr: image_b64 пуст или не является base64.') }
    # Байты картинки → WinRT-поток → декодер → SoftwareBitmap.
    $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
    $writer = New-Object Windows.Storage.Streams.DataWriter $stream
    $writer.WriteBytes($bytes)
    [void](Wait-OcrOp $writer.StoreAsync() ([uint32]))
    [void]$writer.DetachStream()
    [void]$stream.Seek(0)
    $decoder = Wait-OcrOp ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Wait-OcrOp $decoder.GetSoftwareBitmapAsync() ([Windows.Graphics.Imaging.SoftwareBitmap])
    # ⚠ RecognizeAsync принимает только Bgra8: PNG/JPEG декодируются в разные форматы, поэтому приводим.
    if ($bitmap.BitmapPixelFormat -ne [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8) {
      $bitmap = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8)
    }

    $engineType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $engine = $null
    $missing = @()
    foreach ($tag in [ChatRepoDriver]::OcrLangs()) {
      try {
        $language = New-Object Windows.Globalization.Language $tag
        if ($engineType::IsLanguageSupported($language)) {
          $engine = $engineType::TryCreateFromLanguage($language)
          if ($null -ne $engine) { break }
        } else { $missing += $tag }
      } catch { $missing += $tag }
    }
    if ($null -eq $engine) { $engine = $engineType::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) {
      return [ChatRepoDriver]::OcrFail('unsupported', 'Нет доступного языка распознавания. Установите языковой пакет с компонентом ' +
        '«Распознавание текста» в «Параметры → Время и язык → Язык».')
    }

    $result = Wait-OcrOp $engine.RecognizeAsync($bitmap) ([Windows.Media.Ocr.OcrResult])
    [ChatRepoDriver]::OcrBegin()
    foreach ($ocrLine in $result.Lines) {
      # bounds строки = объединение боксов её слов, в пикселях переданной картинки.
      $minX = [double]::PositiveInfinity; $minY = [double]::PositiveInfinity
      $maxX = [double]::NegativeInfinity; $maxY = [double]::NegativeInfinity
      foreach ($word in $ocrLine.Words) {
        $rect = $word.BoundingRect
        if ($rect.X -lt $minX) { $minX = $rect.X }
        if ($rect.Y -lt $minY) { $minY = $rect.Y }
        if (($rect.X + $rect.Width) -gt $maxX) { $maxX = $rect.X + $rect.Width }
        if (($rect.Y + $rect.Height) -gt $maxY) { $maxY = $rect.Y + $rect.Height }
      }
      if ($maxX -gt $minX -and $maxY -gt $minY) {
        [ChatRepoDriver]::OcrAddLine($ocrLine.Text, [int][Math]::Floor($minX), [int][Math]::Floor($minY),
          [int][Math]::Ceiling($maxX - $minX), [int][Math]::Ceiling($maxY - $minY))
      } else {
        [ChatRepoDriver]::OcrAddLine($ocrLine.Text, 0, 0, 0, 0)
      }
    }
    $text = (@($result.Lines | ForEach-Object { $_.Text })) -join "`n"
    $note = $null
    if ($missing.Count -gt 0) {
      $note = 'Не установлены языки распознавания: ' + ($missing -join ', ') + '. Использован доступный движок.'
    }
    return [ChatRepoDriver]::OcrFinish($text, 'windows-ocr', $note)
  } catch {
    return [ChatRepoDriver]::OcrFail('failed', 'Не удалось распознать текст: ' + [string]$_.Exception.Message)
  }
}

try {
  # Handle() никогда не бросает: любая ошибка одного запроса — это строка error, а не падение процесса.
  while ($null -ne ($line = [ChatRepoDriver]::ReadLine())) {
    if ($line.Trim().Length -eq 0) { continue }
    $out = [ChatRepoDriver]::Handle($line)
    # Маркер OCR: C# разобрал запрос, а распознавание делает PowerShell (WinRT).
    if ($out -eq [ChatRepoDriver]::OcrSentinel) { $out = Invoke-ChatRepoOcr }
    [ChatRepoDriver]::WriteLine($out)
  }
} finally {
  # stdin закрыт — клиент ушёл. Всё зажатое (key_down / mouse_down) отпускается, иначе у человека
  # «залипнет» Shift или левая кнопка мыши.
  [ChatRepoDriver]::ReleaseAll()
}
