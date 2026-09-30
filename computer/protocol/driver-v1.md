# Протокол помощника Computer Use (`chatrepo-computer`), версия 1

Долгоживущий процесс на пользовательскую сессию вместо запуска `osascript` / `powershell.exe` /
`xdotool` на каждое действие. Один помощник на платформу:

| ОС | Файл | Как запускается |
|---|---|---|
| macOS | `go/internal/computer/assets/macos/ComputerDriver.swift` | release: prebuilt `chatrepo-computer-driver`; source checkout: one-time `swiftc` fallback |
| Windows | `go/internal/computer/assets/windows/computer_driver.ps1` | `powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ...` |
| Linux | `go/internal/computer/assets/linux/computer_driver.py` | `/usr/bin/python3 -u <path>` |

На Wayland ввод и захват по-прежнему идут через `wayland_portal.py` (единственный законный путь);
помощник Linux на Wayland отвечает только за дерево доступности и запуск приложений.

## Транспорт

- stdin/stdout, UTF-8, **одна JSON-строка на сообщение** (без переносов внутри).
- Запрос: `{"id": <int>, "method": "<name>", "params": {...}}`.
- Ответ: ровно один на запрос, с тем же `id`:
  `{"id": <int>, "result": <any>}` или `{"id": <int>, "error": {"code": "<code>", "message": "<текст>"}}`.
- Клиент шлёт запросы строго по одному (ждёт ответа перед следующим).
- Помощник завершается, когда закрыт stdin. Любая необработанная ошибка одного запроса — это
  ответ `error`, а не падение процесса.
- **stderr не читается и не логируется.** Помощник никогда не пишет в stdout/stderr пиксели или
  введённый текст.
- `message` — по-русски, для агента и человека: что случилось и что делать.

Коды ошибок: `bad_request` (неверные аргументы), `unsupported` (не умеет на этой ОС/сессии),
`permission` (ОС не дала права), `not_found` (окно/элемент/приложение не найдено), `stale`
(идентификатор элемента из прошлой выборки), `blocked` (ОС приняла не все события — UIPI,
защищённый ввод, политика), `failed` (прочее).
`uncertain` — приложение не подтвердило действие вовремя, но оно МОГЛО выполниться (кнопка открыла
модальное окно и держит ответ): клиент не повторяет вслепую, а смотрит экран. `cancelled` — ввод
прерван человеком или приложением (SIGTERM / закрытый stdin).

## Координаты

Все точки и прямоугольники — в **родном глобальном пространстве ввода платформы**:
- macOS — точки Quartz (начало — левый верх основного экрана, y вниз);
- Windows — физические пиксели виртуального рабочего стола; процесс объявляет
  Per-Monitor-V2 DPI awareness до любого вызова;
- Linux X11 — пиксели корневого окна.

Прямоугольник: `{"x", "y", "width", "height"}`. Общий `chatrepo-computer-host` хранит родные границы кадра и переводит пиксели MCP-снимка в координаты платформы; модель не угадывает native coordinates.

## Методы

### `hello` → сведения о помощнике
```json
{"protocol": 1, "platform": "darwin|win32|linux", "backend": "macos-native|windows-uia|linux-x11|linux-wayland-atspi",
 "capabilities": {"input": true, "unicode_type": true, "cursor": true, "windows": true, "window_control": true,
                  "elements": true, "launch": true},
 "permissions": {"accessibility": "granted|denied|not_applicable"},
 "notes": ["строки для человека: чего не хватает и как это исправить"]}
```
`hello` обязан быть дешёвым и не запрашивать прав у ОС (никаких системных окон).

### `input` → `{"cursor": {"x", "y"}}`
`params` — одно действие:

| action | поля |
|---|---|
| `move` | `x, y` |
| `click` | `x, y, button, clicks (>=1), modifiers[], hold_ms? (>=0, удержание перед отпусканием)` |
| `mouse_down` / `mouse_up` | `x, y, button, modifiers[]` (удержание кнопки между вызовами) |
| `drag` | `x, y, toX, toY, button, modifiers[], steps? (по умолчанию 24), duration_ms? (по умолчанию 400)` |
| `scroll` | `x, y, deltaX, deltaY, modifiers[], unit: "pixel" \| "line"` |
| `type` | `text` |
| `key` / `key_down` / `key_up` | `key, modifiers[], repeat? (>=1, только key)` |

- `button`: `left | right | middle | back | forward`.
- `modifiers`: `shift, control|ctrl, option|alt, command|cmd|meta|win, fn|function` (`fn` — только
  macOS; на других ОС — `unsupported`). Модификаторы нажимаются до действия и отпускаются после
  (для `key_down`/`mouse_down` — остаются зажатыми до парного `*_up`).
- `scroll`: положительный `deltaY` — прокрутка вниз (содержимое уезжает вверх), положительный
  `deltaX` — вправо. `unit: "pixel"` — пиксели (Windows: единицы колеса 1/120 деления, X11: одно
  деление на каждые 120, минимум одно); `unit: "line"` — число строк/делений.
- `type`: текст вводится **юникодом напрямую** (без буфера обмена и без зависимости от раскладки):
  macOS — `CGEventKeyboardSetUnicodeString` порциями по ≤20 UTF-16 единиц; Windows —
  `KEYEVENTF_UNICODE` (суррогатные пары — двумя событиями); X11 — keysym, для символов вне раскладки —
  временное переназначение свободного keycode. `\n` → Return, `\t` → Tab.
- `drag`: обязательны промежуточные шаги с зажатой кнопкой — иначе приложения видят клик.
- Имена клавиш (регистр и пробелы не важны): `enter|return, tab, space, backspace, delete (=backspace на
  macOS, Delete на Windows/X11 — как в текущем коде), forwarddelete|del, escape|esc, insert, home, end,
  pageup, pagedown, left|arrowleft, right|arrowright, up|arrowup, down|arrowdown, f1..f20, capslock,
  numlock, scrolllock, printscreen, pause, menu|apps, shift, control|ctrl, option|alt,
  command|cmd|meta|win, numpad0..numpad9, multiply, add, subtract, decimal, divide, numpadenter,
  volumeup, volumedown, volumemute, medianext, mediaprev, mediaplay, mediastop`, а также любой
  одиночный печатный символ — через **текущую** раскладку (Ctrl+C работает и на русской).
- Если ОС приняла не все события — `blocked`, а не успех.

### `cursor` → `{"x", "y"}`
Позиция физического указателя.

### `windows` → список окон приложений спереди назад
```json
{"windows": [{"id": "строка", "title": "…", "app": "…", "pid": 123, "bounds": {…},
              "focused": true, "minimized": false, "bundle_id": "…?", "offscreen": true?}],
 "frontmost_app": {"name": "…", "pid": 123, "bundle_id": "…?"}}
```
Только обычные окна приложений (без меню, док-панелей, прозрачных служебных окон, облачённых
окон Windows). `id` стабилен, пока окно живо. На Wayland — `unsupported`.

### `window` → `{"window": <элемент списка windows после действия>}`
`params`: `{"id", "action": "focus|minimize|maximize|restore|close|set_bounds", "bounds"?}`.
Если ОС не дала поднять окно (Windows ограничивает кражу фокуса) — `blocked` с объяснением.

### `launch` → `{"app": "имя", "pid"?: 123}`
`params`: `{"app": "имя | bundle id | путь | .desktop id", "args"?: [строки]}`. Без оболочки:
аргументы передаются списком. Не найдено — `not_found`.

### `elements` → элементы дерева доступности
`params`:
```json
{"window_id"?: "…", "pid"?: 123, "point"?: {"x","y"}, "query"?: "подстрока", "role"?: "button",
 "max"?: 150, "depth"?: 12}
```
- Область: `window_id` → это окно; иначе `pid` → окна приложения; иначе — окно в фокусе
  (переднее приложение).
- `point` — вернуть элемент под точкой и цепочку его предков (до 8), самый глубокий первым.
- `query` — без учёта регистра по `name`, `value`, `description`; `role` — по нормализованной роли.
- Пределы: `max` ≤ 500, `depth` ≤ 30, бюджет времени ~3 с; при превышении — `truncated: true`.

Ответ:
```json
{"epoch": 7, "truncated": false, "window": {"id": "…", "title": "…"},
 "elements": [{"id": "e7.12", "role": "button", "raw_role": "AXButton", "name": "Сохранить",
               "value": "…?", "description": "…?", "bounds": {…}?, "enabled": true, "focused": false,
               "secure": false, "actions": ["press", "show_menu"]}]}
```
- Нормализованные роли: `button, checkbox, radio, switch, text_field, text_area, link, menu,
  menu_item, menu_bar, tab, tab_list, list, list_item, tree, tree_item, table, row, cell, combo_box,
  slider, spin_button, scroll_bar, scroll_area, image, text, heading, window, dialog, group, toolbar,
  progress, web_area, other`.
- `secure: true` — поле пароля (`AXSecureTextField`, UIA `IsPassword`, AT-SPI `password-text`);
  **его `value` не читается и не возвращается никогда**.
- `actions` — из нормализованного набора: `press, focus, set_value, show_menu, increment,
  decrement, select, expand, collapse, scroll_into_view`.
- `id` = `e<epoch>.<n>`; помощник держит ссылки на элементы **последней** выборки. Идентификатор
  из прежней эпохи — ошибка `stale` («получите элементы заново»).
- Длинные строки обрезаются до 300 символов.
- Chromium/Electron строят дерево только при включённых ассистивных технологиях: macOS —
  `AXManualAccessibility`/`AXEnhancedUserInterface` на приложении; Linux — `org.a11y.Status.IsEnabled`.

### `element_action` → `{"element": <элемент после действия, если ещё существует>}`
`params`: `{"id", "action": "press|focus|set_value|show_menu|increment|decrement|select|expand|collapse|scroll_into_view|locate", "value"?}`.
- `locate` — ничего не делает, возвращает свежие `bounds` (приложение кликает по центру само,
  когда у элемента нет `press`).
- Действие, которое элемент не поддерживает, — `unsupported` с перечнем того, что поддерживает.

### `focused` → `{"element": <элемент | null>, "app": {"name", "pid"}}`
Элемент в фокусе клавиатуры (без присвоения эпохи — `id` пустой).

## Версия 1.1 — дополнительные методы

`hello.capabilities` дополняется флагами `ocr`, `clipboard_files`, `app_at`, `background_input`,
`touch` (true — метод реализован и может сработать в этой сессии). `protocol` остаётся `1`:
клиент проверяет флаг, а не номер. Нет метода — `bad_request`, нет возможности — `unsupported`.

### `ocr` → распознанный текст с боксами
`params`: `{"image_b64": "<PNG или JPEG>", "languages"?: ["ru", "en"]}`.
```json
{"text": "весь текст построчно через \n", "engine": "vision|windows-ocr|tesseract",
 "lines": [{"text": "Сохранить", "confidence": 0.98, "bounds": {"x": 10, "y": 20, "width": 80, "height": 18}}]}
```
- `bounds` — в пикселях ПЕРЕДАННОЙ картинки (левый верх — начало). Строки — сверху вниз, слева направо.
- `confidence` 0..1 (если движок не даёт — `null`).
- Языки по умолчанию: русский и английский (сколько поддерживает движок).
- macOS: Vision `VNRecognizeTextRequest` (accurate). Windows: `Windows.Media.Ocr` (языки, установленные в
  системе; если нужного нет — первый доступный и `note`). Linux: `tesseract` CLI (`tsv`); нет — `unsupported`
  с текстом «установите пакет tesseract-ocr и tesseract-ocr-rus».

### `clipboard_files`
`{"action": "get"}` → `{"paths": ["/абсолютный/путь", …]}` — файлы в буфере обмена (скопированные в
Finder/Проводнике/файловом менеджере); `{"action": "set", "paths": [...]}` → `{"count": N}` — положить
файлы в буфер так, чтобы их можно было вставить (⌘V / Ctrl+V) в приложение или файловый менеджер.
Пути абсолютные и существуют (иначе `not_found`). Windows — `CF_HDROP` (STA-поток), macOS —
`NSPasteboard` с `NSURL`, Linux — `text/uri-list` + `x-special/gnome-copied-files`.

### `app_at` → что под точкой
`params`: `{"x", "y"}` → `{"app": {"name", "pid", "bundle_id"?} | null, "window_id": "…" | null}` —
верхнее окно обычного приложения в этой точке (без служебных окон chatrepo-оверлея).

### `background_input` → ввод в окно, не трогая мышь и фокус человека
`params`: `{"window_id", "action": "type" | "key" | "click", "text"?, "key"?, "modifiers"?, "x"?, "y"?,
"button"?, "clicks"?}` (x/y — глобальные координаты, как в `input`).
→ `{"delivered": true, "method": "postToPid | PostMessage | XSendEvent", "note"?: "…"}`.
- Лучшее усилие: часть приложений синтетический фоновый ввод игнорирует — `note` говорит об этом, а
  клиент обязан проверить результат снимком. Физический курсор и активное окно НЕ меняются.
- macOS: `CGEvent.postToPid`; Windows: `PostMessage` `WM_CHAR`/`WM_KEYDOWN`/`WM_*BUTTON*` в окно
  (для ввода — в фокусный дочерний элемент потока окна через `GetGUIThreadInfo`, координаты — в
  клиентские); Linux X11: `XSendEvent`; Wayland — `unsupported`.

### `touch` → сенсорный жест
`params`: `{"gesture": "tap" | "double_tap" | "long_press" | "swipe" | "pinch" | "rotate", "x", "y",
"toX"?, "toY"?, "scale"? (pinch: >0, <1 — свести пальцы), "angle"? (rotate, градусы), "duration_ms"?}`.
- Windows: `InjectTouchInput` (1–2 контакта; `InitializeTouchInjection` один раз). Linux Wayland: портал
  (устройство `TOUCHSCREEN`, `NotifyTouchDown/Motion/Up`). macOS и Linux X11: сенсорного ввода нет —
  `tap`/`double_tap`/`long_press`/`swipe` эмулируются мышью (в ответе `"emulated": true`), `pinch`/`rotate`
  → `unsupported`.
- Ответ: `{"emulated": false}` или `{"emulated": true}`.

### Wayland: `windows` через AT-SPI
На Wayland `windows` отдаёт окна приложений из AT-SPI (фреймы): `id` вида `atspi:<pid>:<n>`, `bounds: null`
(координат Wayland не даёт), `focused` по состоянию ACTIVE. `window` на Wayland поддерживает только
`focus` (через AT-SPI: действие окна `activate`/`grab_focus`); остальное — `unsupported` с подсказкой
клавиш (Super+↑, Super+H, Alt+F4) через портал.

### Wayland-портал (`wayland_portal.py`) — дополнения
- Запуск с `--token-file <путь>`: разрешение сохраняется (`persist_mode: 2`, `restore_token`), и
  повторный прогон не показывает системное окно, пока человек не отзовёт доступ. Файл — только токен,
  права 0600.
- `capture` принимает `{"region"?: {x, y, width, height}}` в логических координатах — кусок кадра в разрешении потока (для приближения), ограниченный `COMPUTER_CAPTURE_MAX_EDGE` по длинной стороне; без region отдаётся весь разрешённый виртуальный desktop. Ответ добавляет `region` (фактически отданная область, логические координаты).
- `action` с `{"action": "touch", ...}` — жесты по разделу `touch` выше, если портал выдал TOUCHSCREEN.

### `element_action` → `read` (протокол 1.1)
`{"id", "action": "read"}` → `{"element": {...}, "text": "полный текст/значение, без обрезания", "selected_text"?: "…",
"caret"?: <int — позиция курсора ввода>, "truncated"?: true}`. Для `secure: true` текст и выделение НЕ читаются никогда
(`text: null`). macOS — `AXValue` без обрезки, `AXSelectedText`, `AXSelectedTextRange`; Windows — `TextPattern.DocumentRange`
(иначе `ValuePattern.Value`) и `TextPattern.GetSelection`; Linux — AT-SPI `Text.get_text(0, -1)`, `get_selection`, `caret_offset`.

### Нормализация клавиш и модификаторов (делает приложение, помощник получает уже чистое)
Приложение само разбирает сочетания вида `ctrl+shift+t`, псевдонимы (`Page_Down`, `PageDown`, `pgdn`, `Return`,
`BackSpace`, `super`, `KP_Enter`, `ArrowLeft`, `Escape`, `Meta`, `prtsc`, `ins`…) и отдаёт помощнику каноническое имя из
списка выше. `delete`/`del` → `forwarddelete` (как в xdotool и DOM), `backspace` — отдельно. Одиночная буква с
модификаторами передаётся строчной (Shift — только явно). Помощник обязан: (1) для `key_up` отпускать модификаторы,
зажатые парным `key_down`, даже если `key_up` пришёл без них; (2) для одиночного символа, которому на текущей раскладке
нужен Shift (например `!`), добавлять Shift самому.
