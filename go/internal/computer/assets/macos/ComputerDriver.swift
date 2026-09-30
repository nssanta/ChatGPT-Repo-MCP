// chatrepo-computer — долгоживущий помощник Computer Use для macOS.
// Протокол: JSON-строки по stdin/stdout; клиент — src/host/tools/computer/computer-driver.ts.
//
// ⚠⚠ ЗАЧЕМ НАТИВНЫЙ КОД, А НЕ JXA. Мост JXA не умеет передавать `const UniChar*`, поэтому
// `CGEventKeyboardSetUnicodeString` из osascript молча ничего не пишет, и текст приходилось вводить
// через буфер обмена человека. Здесь это одна строка. Плюс AX (дерево элементов, окна) и отсутствие
// запуска osascript на каждое действие.
//
// ⚠⚠ РАЗРЕШЕНИЯ БЕРУТСЯ У ПРИЛОЖЕНИЯ. Помощник лежит в бандле chatrepo и запускается им как дочерний
// процесс, поэтому TCC (Accessibility, Screen Recording) проверяет «ответственный» процесс — само
// приложение. Отдельной строки в настройках у помощника нет и не должно быть.
//
// ⚠ stdout — только ответы протокола. Ни пикселей, ни введённого текста в выводе нет никогда.

import AppKit
import ApplicationServices
import Carbon
import CoreGraphics
import Foundation
import ImageIO
import Vision

// MARK: - Ошибки и разбор аргументов

struct DriverError: Error {
    let code: String
    let message: String
}

func failure(_ code: String, _ message: String) -> DriverError { DriverError(code: code, message: message) }

typealias JSON = [String: Any]

func number(_ params: JSON, _ key: String) throws -> Double {
    guard let value = params[key] as? NSNumber, value.doubleValue.isFinite else {
        throw failure("bad_request", "поле \(key) должно быть числом")
    }
    return value.doubleValue
}

func optionalNumber(_ params: JSON, _ key: String) throws -> Double? {
    if params[key] == nil || params[key] is NSNull { return nil }
    return try number(params, key)
}

/** Integer representability and action semantics, without an artificial count/duration ceiling. */
func actionCount(_ params: JSON, _ key: String, fallback: Int, minimum: Int) throws -> Int {
    guard let value = try optionalNumber(params, key) else { return fallback }
    guard let count = Int(exactly: value), count >= minimum else {
        throw failure("bad_request", "поле \(key) должно быть представимым целым не меньше \(minimum)")
    }
    return count
}

func string(_ params: JSON, _ key: String) throws -> String {
    guard let value = params[key] as? String else { throw failure("bad_request", "поле \(key) должно быть строкой") }
    return value
}

func stringList(_ params: JSON, _ key: String) throws -> [String] {
    guard let raw = params[key], !(raw is NSNull) else { return [] }
    guard let list = raw as? [Any] else { throw failure("bad_request", "поле \(key) должно быть массивом строк") }
    return try list.map {
        guard let one = $0 as? String else { throw failure("bad_request", "поле \(key) должно быть массивом строк") }
        return one
    }
}

func nonEmptyText(_ text: String?) -> String? {
    guard let text = text else { return nil }
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.isEmpty { return nil }
    return trimmed
}

func rectJSON(_ rect: CGRect) -> JSON {
    ["x": Int(rect.origin.x.rounded()), "y": Int(rect.origin.y.rounded()),
     "width": Int(rect.size.width.rounded()), "height": Int(rect.size.height.rounded())]
}

func sleepMs(_ ms: Double) { if ms > 0 { Thread.sleep(forTimeInterval: ms / 1000) } }

// MARK: - Ввод

let namedKeys: [String: CGKeyCode] = [
    "enter": 36, "return": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51,
    "escape": 53, "esc": 53, "command": 55, "cmd": 55, "meta": 55, "win": 55, "shift": 56, "capslock": 57,
    "option": 58, "alt": 58, "control": 59, "ctrl": 59, "fn": 63, "function": 63,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
    "f18": 79, "f19": 80, "f20": 90,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "forwarddelete": 117, "del": 117,
    "insert": 114, "help": 114, "numlock": 71, "menu": 110, "apps": 110,
    "left": 123, "arrowleft": 123, "right": 124, "arrowright": 124,
    "down": 125, "arrowdown": 125, "up": 126, "arrowup": 126,
    "numpad0": 82, "numpad1": 83, "numpad2": 84, "numpad3": 85, "numpad4": 86, "numpad5": 87,
    "numpad6": 88, "numpad7": 89, "numpad8": 91, "numpad9": 92,
    "multiply": 67, "add": 69, "subtract": 78, "decimal": 65, "divide": 75, "numpadenter": 76,
]

// ⚠ ОДИНОЧНЫЕ СИМВОЛЫ ДЛЯ СОЧЕТАНИЙ — ПО ПОЗИЦИИ ANSI, КАК ДЕЛАЕТ САМА macOS. command+C на русской
// раскладке — это та же физическая клавиша, что и на латинской; меню сопоставляет сочетание по ней.
let ansiKeys: [Character: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26,
    "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "`": 50, " ": 49,
]
let ansiShifted: [Character: Character] = [
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
    "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", "\"": "'", "<": ",", ">": ".", "?": "/", "~": "`",
]
// Медиаклавиши — не виртуальные коды, а системные события NX_KEYTYPE_*: обычное нажатие кода 72
// громкость не меняет.
let mediaKeys: [String: Int] = [
    "volumeup": 0, "volumedown": 1, "volumemute": 7, "mediaplay": 16, "medianext": 17, "mediaprev": 18,
]
// «Стоп» у Mac нет: подменять его «воспроизведением/паузой» — значит включать музыку вместо остановки.
let unsupportedKeys: Set<String> = ["scrolllock", "printscreen", "pause", "mediastop"]

func modifierFlag(_ raw: String) throws -> (CGEventFlags, CGKeyCode) {
    switch raw.lowercased() {
    case "shift": return (.maskShift, 56)
    case "control", "ctrl": return (.maskControl, 59)
    case "option", "alt": return (.maskAlternate, 58)
    case "command", "cmd", "meta", "win": return (.maskCommand, 55)
    case "fn", "function": return (.maskSecondaryFn, 63)
    default: throw failure("bad_request", "неизвестный модификатор «\(raw)»")
    }
}

let modifierKeyFlags: [CGKeyCode: CGEventFlags] = [56: .maskShift, 60: .maskShift, 59: .maskControl, 62: .maskControl,
                                                  58: .maskAlternate, 61: .maskAlternate, 55: .maskCommand, 54: .maskCommand,
                                                  63: .maskSecondaryFn]

enum KeyTarget {
    case code(CGKeyCode, CGEventFlags)
    case media(Int)
    case unicode(String)
}

final class Input {
    /// ⚠⚠ ЧЕЛОВЕК НЕ ДОЛЖЕН ТЕРЯТЬ СВОЮ КЛАВИАТУРУ И МЫШЬ, ПОКА АГЕНТ ПЕЧАТАЕТ. По умолчанию macOS
    /// глушит физический ввод на 0.25 с после каждого синтетического события — во время длинного ввода
    /// это значит, что горячая клавиша «стоп» и мышь человека не доходят. Подавление выключено.
    let source: CGEventSource? = {
        let source = CGEventSource(stateID: .hidSystemState)
        source?.localEventsSuppressionInterval = 0
        let permit: CGEventFilterMask = [.permitLocalMouseEvents, .permitLocalKeyboardEvents, .permitSystemDefinedEvents]
        source?.setLocalEventsFilterDuringSuppressionState(permit, state: .eventSuppressionStateSuppressionInterval)
        source?.setLocalEventsFilterDuringSuppressionState(permit, state: .eventSuppressionStateRemoteMouseDrag)
        return source
    }()
    private var heldKeys = Set<CGKeyCode>()
    /// Модификаторы, зажатые вместе с key_down: отпускаются парным key_up, даже если его прислали без них.
    private var keyModifiers: [CGKeyCode: [CGKeyCode]] = [:]
    private var heldButtons = Set<Int>()
    private var layoutMap: [Character: (CGKeyCode, CGEventFlags)]?
    private var layoutId: String = ""

    var heldFlags: CGEventFlags {
        var flags: CGEventFlags = []
        for key in heldKeys { if let flag = modifierKeyFlags[key] { flags.insert(flag) } }
        return flags
    }

    private func post(_ event: CGEvent?, pause: Double = 8) throws {
        // ⚠ Прерывание (SIGTERM от приложения — стоп человеком или таймаут) обрывает длинный ввод на
        // следующем же событии, а не после того, как допечатается весь текст.
        if interrupted { throw failure("cancelled", "ввод прерван") }
        guard let event = event else { throw failure("failed", "macOS не создала событие ввода") }
        event.post(tap: .cghidEventTap)
        sleepMs(pause)
    }

    // MARK: мышь

    private func buttonNumber(_ raw: String) throws -> Int {
        switch raw.lowercased() {
        case "left": return 0
        case "right": return 1
        case "middle": return 2
        case "back": return 3
        case "forward": return 4
        default: throw failure("bad_request", "неизвестная кнопка мыши «\(raw)»")
        }
    }

    private func types(_ button: Int) -> (CGEventType, CGEventType, CGEventType, CGMouseButton) {
        switch button {
        case 0: return (.leftMouseDown, .leftMouseUp, .leftMouseDragged, .left)
        case 1: return (.rightMouseDown, .rightMouseUp, .rightMouseDragged, .right)
        default: return (.otherMouseDown, .otherMouseUp, .otherMouseDragged, CGMouseButton(rawValue: UInt32(button)) ?? .center)
        }
    }

    private func mouse(_ type: CGEventType, _ point: CGPoint, _ button: CGMouseButton, _ flags: CGEventFlags,
                       clickState: Int64 = 1, pause: Double = 8) throws {
        let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: button)
        event?.flags = flags
        event?.setIntegerValueField(.mouseEventClickState, value: clickState)
        if button.rawValue > 2 { event?.setIntegerValueField(.mouseEventButtonNumber, value: Int64(button.rawValue)) }
        try post(event, pause: pause)
    }

    /// Движение с учётом зажатой кнопки: при удержании приложения ждут Dragged, а не Moved.
    private func moveTo(_ point: CGPoint, _ flags: CGEventFlags, pause: Double = 8) throws {
        if let held = heldButtons.min() {
            let (_, _, drag, button) = types(held)
            try mouse(drag, point, button, flags, pause: pause)
        } else {
            try mouse(.mouseMoved, point, .left, flags, pause: pause)
        }
    }

    private func pressModifiers(_ names: [String]) throws -> (CGEventFlags, [CGKeyCode]) {
        var flags = heldFlags
        var pressed: [CGKeyCode] = []
        for name in names {
            let (flag, code) = try modifierFlag(name)
            flags.insert(flag)
            if heldKeys.contains(code) { continue }
            let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
            event?.flags = flags
            do { try post(event, pause: 4) } catch {
                // Уже зажатое не должно остаться нажатым, если следующий модификатор не прошёл.
                releaseModifiers(pressed)
                throw error
            }
            pressed.append(code)
        }
        return (flags, pressed)
    }

    private func releaseModifiers(_ pressed: [CGKeyCode]) {
        var flags = heldFlags
        for code in pressed { if let flag = modifierKeyFlags[code] { flags.insert(flag) } }
        for code in pressed.reversed() {
            if let flag = modifierKeyFlags[code] { flags.remove(flag) }
            let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
            event?.flags = flags
            event?.post(tap: .cghidEventTap)
            sleepMs(4)
        }
    }

    func perform(_ params: JSON) throws -> JSON {
        let action = try string(params, "action")
        let modifiers = try stringList(params, "modifiers")
        var secureInput = false
        switch action {
        case "move":
            let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            try moveTo(point, heldFlags)
        case "click":
            let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            let button = try buttonNumber((params["button"] as? String) ?? "left")
            let clicks = try actionCount(params, "clicks", fallback: 1, minimum: 1)
            let hold = Double(try actionCount(params, "hold_ms", fallback: 0, minimum: 0))
            let (flags, pressed) = try pressModifiers(modifiers)
            defer { releaseModifiers(pressed) }
            try moveTo(point, flags, pause: 12)
            let (down, up, _, cg) = types(button)
            // Кнопка числится зажатой между down и up: если ввод прервут посередине, releaseAll её отпустит.
            for index in 1...clicks {
                try mouse(down, point, cg, flags, clickState: Int64(index), pause: hold > 0 ? hold : 8)
                heldButtons.insert(button)
                try mouse(up, point, cg, flags, clickState: Int64(index))
                heldButtons.remove(button)
            }
        case "mouse_down", "mouse_up":
            let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            let button = try buttonNumber((params["button"] as? String) ?? "left")
            let flags = heldFlags.union(try modifiers.reduce(CGEventFlags()) { $0.union(try modifierFlag($1).0) })
            let (down, up, _, cg) = types(button)
            if action == "mouse_down" {
                try moveTo(point, flags)
                try mouse(down, point, cg, flags)
                heldButtons.insert(button)
            } else {
                heldButtons.remove(button)
                try mouse(up, point, cg, flags)
            }
        case "drag":
            let from = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            let to = CGPoint(x: try number(params, "toX"), y: try number(params, "toY"))
            let button = try buttonNumber((params["button"] as? String) ?? "left")
            let steps = try actionCount(params, "steps", fallback: 24, minimum: 2)
            let duration = Double(try actionCount(params, "duration_ms", fallback: 400, minimum: 0))
            let (flags, pressed) = try pressModifiers(modifiers)
            defer { releaseModifiers(pressed) }
            let (down, up, dragged, cg) = types(button)
            // ⚠ ПРОМЕЖУТОЧНЫЕ ШАГИ ОБЯЗАТЕЛЬНЫ: перетаскивание начинается по событиям Dragged.
            // Прыжок из точки в точку приложения принимают за обычный клик и ничего не тащат.
            try mouse(.mouseMoved, from, .left, flags, pause: 20)
            try mouse(down, from, cg, flags, pause: 60)
            heldButtons.insert(button)
            var released = false
            defer {
                // Отпускание мимо флага прерывания: прерванное перетаскивание не должно оставить кнопку зажатой.
                if !released {
                    let event = CGEvent(mouseEventSource: source, mouseType: up, mouseCursorPosition: to, mouseButton: cg)
                    event?.flags = flags
                    event?.post(tap: .cghidEventTap)
                    heldButtons.remove(button)
                }
            }
            for step in 1...steps {
                let t = Double(step) / Double(steps)
                let point = CGPoint(x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t)
                try mouse(dragged, point, cg, flags, pause: duration / Double(steps))
            }
            sleepMs(60)
            try mouse(up, to, cg, flags, pause: 40)
            heldButtons.remove(button)
            released = true
        case "scroll":
            let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            let dx = try optionalNumber(params, "deltaX") ?? 0
            let dy = try optionalNumber(params, "deltaY") ?? 0
            let unit: CGScrollEventUnit = (params["unit"] as? String) == "line" ? .line : .pixel
            guard abs(dx) <= 2_147_483_647, abs(dy) <= 2_147_483_647 else { throw failure("bad_request", "delta вне int32") }
            let (flags, pressed) = try pressModifiers(modifiers)
            defer { releaseModifiers(pressed) }
            try moveTo(point, flags, pause: 12)
            // Две оси одним событием: в Swift инициализатор не вариадический, поэтому вторая ось не
            // теряется, как терялась в мосте JXA.
            let event = CGEvent(scrollWheelEvent2Source: source, units: unit, wheelCount: 2,
                                wheel1: Int32(-dy), wheel2: Int32(-dx), wheel3: 0)
            event?.flags = flags
            try post(event)
        case "type":
            let text = try string(params, "text")
            if text.isEmpty { throw failure("bad_request", "текст пуст") }
            secureInput = IsSecureEventInputEnabled()
            try typeText(text)
        case "key", "key_down", "key_up":
            let key = try string(params, "key")
            let repeatCount = try actionCount(params, "repeat", fallback: 1, minimum: 1)
            secureInput = IsSecureEventInputEnabled()
            try pressKey(key, modifiers: modifiers, mode: action, repeatCount: repeatCount)
        default:
            throw failure("bad_request", "неизвестное действие «\(action)»")
        }
        let location = CGEvent(source: nil)?.location ?? .zero
        // ⚠ Вернуть спрятанную при вводе стрелку синтетическим движением НЕЛЬЗЯ: замерено 11.09.2026 на
        // macOS 26.6 — ни движение на месте, ни на 10 px, ни с полями delta через HID/сессию стрелку не
        // показывают, только настоящая мышь. Поэтому указатель подсвечивает кольцо оверлея, а не помощник.
        var result: JSON = ["cursor": ["x": Int(location.x.rounded()), "y": Int(location.y.rounded())]]
        if secureInput { result["secure_input"] = true }
        return result
    }

    // MARK: клавиатура

    /// Обратная карта «символ → клавиша» для ТЕКУЩЕЙ раскладки: нужна для одиночных символов вне ANSI
    /// (например «ж»), которые агент просит нажать как клавишу.
    private func currentLayoutMap() -> [Character: (CGKeyCode, CGEventFlags)] {
        guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue() else { return [:] }
        let id = (TISGetInputSourceProperty(source, kTISPropertyInputSourceID)).map {
            Unmanaged<CFString>.fromOpaque($0).takeUnretainedValue() as String
        } ?? ""
        if let map = layoutMap, id == layoutId { return map }
        var map: [Character: (CGKeyCode, CGEventFlags)] = [:]
        if let raw = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData) {
            let data = Unmanaged<CFData>.fromOpaque(raw).takeUnretainedValue() as Data
            data.withUnsafeBytes { (buffer: UnsafeRawBufferPointer) in
                guard let layout = buffer.baseAddress?.assumingMemoryBound(to: UCKeyboardLayout.self) else { return }
                let variants: [(UInt32, CGEventFlags)] = [(0, []), (UInt32(shiftKey >> 8), .maskShift),
                                                          (UInt32(optionKey >> 8), .maskAlternate),
                                                          (UInt32((shiftKey | optionKey) >> 8), [.maskShift, .maskAlternate])]
                for (modifierState, flags) in variants {
                    for code in 0..<128 {
                        var dead: UInt32 = 0
                        var chars = [UniChar](repeating: 0, count: 4)
                        var length = 0
                        let status = UCKeyTranslate(layout, UInt16(code), UInt16(kUCKeyActionDown), modifierState,
                                                    UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysBit),
                                                    &dead, 4, &length, &chars)
                        if status == noErr, length > 0, let char = String(utf16CodeUnits: chars, count: length).first,
                           map[char] == nil {
                            map[char] = (CGKeyCode(code), flags)
                        }
                    }
                }
            }
        }
        layoutMap = map
        layoutId = id
        return map
    }

    private func target(_ raw: String) throws -> KeyTarget {
        let normalized = raw.lowercased().replacingOccurrences(of: " ", with: "")
        if let code = namedKeys[normalized] { return .code(code, []) }
        if let media = mediaKeys[normalized] { return .media(media) }
        if unsupportedKeys.contains(normalized) {
            throw failure("unsupported", "клавиши «\(raw)» на клавиатуре Mac нет")
        }
        guard raw.count == 1, let char = raw.first else {
            throw failure("bad_request", "неизвестная клавиша «\(raw)»")
        }
        // ⚠⚠ НА ЛАТИНСКИХ РАСКЛАДКАХ — ПО ТЕКУЩЕЙ РАСКЛАДКЕ. macOS сопоставляет сочетания с символом, который
        // даёт текущая раскладка: на AZERTY код 0 — это «q», и command+A по позиции US закрыл бы
        // приложение (command+Q). Позиции US остаются для нелатинских раскладок (русская), где меню
        // сопоставляет сочетание по латинскому символу той же клавиши.
        if currentLayoutIsLatin(), let (code, flags) = currentLayoutMap()[char] { return .code(code, flags) }
        let lower = Character(String(char).lowercased())
        if let code = ansiKeys[lower] { return .code(code, char.isUppercase ? .maskShift : []) }
        if let base = ansiShifted[char], let code = ansiKeys[base] { return .code(code, .maskShift) }
        if let (code, flags) = currentLayoutMap()[char] { return .code(code, flags) }
        return .unicode(String(char))
    }

    private func currentLayoutIsLatin() -> Bool {
        guard let source = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let raw = TISGetInputSourceProperty(source, kTISPropertyInputSourceIsASCIICapable) else { return false }
        return CFBooleanGetValue(Unmanaged<CFBoolean>.fromOpaque(raw).takeUnretainedValue())
    }

    private func postMedia(_ type: Int, down: Bool) throws {
        let flags = NSEvent.ModifierFlags(rawValue: down ? 0xa00 : 0xb00)
        let data1 = (type << 16) | ((down ? 0xa : 0xb) << 8)
        guard let event = NSEvent.otherEvent(with: .systemDefined, location: .zero, modifierFlags: flags, timestamp: 0,
                                             windowNumber: 0, context: nil, subtype: 8, data1: data1, data2: -1)?.cgEvent
        else { throw failure("failed", "macOS не создала медиасобытие") }
        try post(event, pause: 10)
    }

    private func pressKey(_ key: String, modifiers: [String], mode: String, repeatCount: Int) throws {
        let resolved = try target(key)
        switch resolved {
        case .media(let type):
            if mode != "key" { throw failure("unsupported", "медиаклавишу нельзя удерживать") }
            for _ in 0..<repeatCount { try postMedia(type, down: true); try postMedia(type, down: false) }
        case .unicode(let text):
            if mode != "key" { throw failure("unsupported", "символ «\(key)» нет на текущей раскладке; удерживать его нельзя") }
            if !modifiers.isEmpty { throw failure("unsupported", "символ «\(key)» нет на текущей раскладке; сочетание с ним невозможно") }
            for _ in 0..<repeatCount { try typeText(text) }
        case .code(let code, let implied):
            if mode == "key_up" {
                heldKeys.remove(code)
                var flags = heldFlags.union(implied)
                for name in modifiers { flags.insert(try modifierFlag(name).0) }
                if let own = modifierKeyFlags[code] { flags.remove(own) }
                let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
                event?.flags = flags
                try post(event)
                // Модификаторы, зажатые парным key_down, отпускаются здесь же — и названные, и запомненные.
                var modCodes = keyModifiers.removeValue(forKey: code) ?? []
                for name in modifiers { modCodes.append(try modifierFlag(name).1) }
                for modCode in modCodes {
                    if heldKeys.remove(modCode) != nil {
                        let up = CGEvent(keyboardEventSource: source, virtualKey: modCode, keyDown: false)
                        up?.flags = heldFlags
                        try post(up, pause: 4)
                    }
                }
                return
            }
            let (flags, pressed) = try pressModifiers(modifiers)
            var all = flags.union(implied)
            if let own = modifierKeyFlags[code] { all.insert(own) }
            if mode == "key_down" {
                for code in pressed { heldKeys.insert(code) }
                keyModifiers[code, default: []].append(contentsOf: pressed)
                let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
                event?.flags = all
                try post(event)
                heldKeys.insert(code)
                return
            }
            defer { releaseModifiers(pressed) }
            for _ in 0..<repeatCount {
                let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
                down?.flags = all
                try post(down, pause: 6)
                let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
                up?.flags = modifierKeyFlags[code] != nil ? flags.union(implied) : all
                try post(up, pause: 6)
            }
        }
    }

    /// ⚠⚠ ЮНИКОД НАПРЯМУЮ, ПО ОДНОМУ СИМВОЛУ (ГРАФЕМЕ) НА СОБЫТИЕ. Длинная строка в одном событии
    /// часть приложений (терминалы, Electron) принимает только первым символом. Раскладка не важна:
    /// кириллица, эмодзи и любые знаки идут как есть, без буфера обмена человека.
    func typeText(_ text: String) throws {
        let flags = heldFlags
        for char in text {
            if char == "\n" || char == "\r\n" || char == "\r" {
                try tapCode(36, flags)
                continue
            }
            if char == "\t" {
                try tapCode(48, flags)
                continue
            }
            var units = Array(String(char).utf16)
            let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true)
            down?.flags = flags
            down?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
            try post(down, pause: 2)
            let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false)
            up?.flags = flags
            up?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
            try post(up, pause: 3)
        }
    }

    private func tapCode(_ code: CGKeyCode, _ flags: CGEventFlags) throws {
        let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
        down?.flags = flags
        try post(down, pause: 4)
        let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
        up?.flags = flags
        try post(up, pause: 4)
    }

    /// Всё, что осталось зажатым (key_down/mouse_down без пары), отпускается при выходе.
    func releaseAll() {
        interrupted = false
        let location = CGEvent(source: nil)?.location ?? .zero
        for button in heldButtons {
            let (_, up, _, cg) = types(button)
            try? mouse(up, location, cg, [])
        }
        heldButtons.removeAll()
        for code in heldKeys {
            let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
            event?.flags = []
            event?.post(tap: .cghidEventTap)
        }
        heldKeys.removeAll()
    }
}

// MARK: - Доступность (AX)

@_silgen_name("_AXUIElementGetWindow")
func _AXUIElementGetWindow(_ element: AXUIElement, _ identifier: UnsafeMutablePointer<CGWindowID>) -> AXError

func axError(_ error: AXError, _ what: String) -> DriverError {
    switch error {
    case .apiDisabled:
        return failure("permission", "macOS не дала доступ к интерфейсу приложений. Разрешите chatrepo в System Settings → Privacy & Security → Accessibility и повторите.")
    case .invalidUIElement:
        return failure("stale", "элемент исчез из интерфейса (\(what)); получите элементы заново")
    case .actionUnsupported, .attributeUnsupported, .noValue:
        return failure("unsupported", "приложение не поддерживает это действие (\(what))")
    case .cannotComplete:
        return failure("failed", "приложение не ответило вовремя (\(what)); оно может быть занято — повторите")
    case .notImplemented:
        return failure("unsupported", "приложение не реализует доступность (\(what))")
    default:
        return failure("failed", "macOS вернула ошибку доступности \(error.rawValue) (\(what))")
    }
}

func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success ? value : nil
}

func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    guard let value = axValue(element, attribute) else { return nil }
    if let text = value as? String { return text }
    if let number = value as? NSNumber { return number.stringValue }
    return nil
}

func axBool(_ element: AXUIElement, _ attribute: String) -> Bool? {
    (axValue(element, attribute) as? NSNumber)?.boolValue
}

func axElements(_ element: AXUIElement, _ attribute: String) -> [AXUIElement] {
    guard let value = axValue(element, attribute), CFGetTypeID(value) == CFArrayGetTypeID() else { return [] }
    return (value as! [AnyObject]).compactMap { item in
        CFGetTypeID(item) == AXUIElementGetTypeID() ? (item as! AXUIElement) : nil
    }
}

func axElement(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
    guard let value = axValue(element, attribute), CFGetTypeID(value) == AXUIElementGetTypeID() else { return nil }
    return (value as! AXUIElement)
}

func axFrame(_ element: AXUIElement) -> CGRect? {
    guard let position = axValue(element, kAXPositionAttribute), let size = axValue(element, kAXSizeAttribute),
          CFGetTypeID(position) == AXValueGetTypeID(), CFGetTypeID(size) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var extent = CGSize.zero
    guard AXValueGetValue(position as! AXValue, .cgPoint, &point), AXValueGetValue(size as! AXValue, .cgSize, &extent) else { return nil }
    return CGRect(origin: point, size: extent)
}

func axSettable(_ element: AXUIElement, _ attribute: String) -> Bool {
    var settable: DarwinBoolean = false
    return AXUIElementIsAttributeSettable(element, attribute as CFString, &settable) == .success && settable.boolValue
}

func axActions(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success, let list = names as? [String] else { return [] }
    return list
}

func windowNumber(_ element: AXUIElement) -> CGWindowID? {
    var identifier: CGWindowID = 0
    return _AXUIElementGetWindow(element, &identifier) == .success && identifier != 0 ? identifier : nil
}

func normalizedRole(_ role: String, _ subrole: String?) -> String {
    switch role {
    case "AXButton", "AXMenuButton", "AXDisclosureTriangle", "AXColorWell": return "button"
    case "AXPopUpButton", "AXComboBox": return "combo_box"
    case "AXCheckBox": return subrole == "AXSwitch" ? "switch" : "checkbox"
    case "AXRadioButton": return subrole == "AXTabButton" ? "tab" : "radio"
    case "AXTextField", "AXDateField", "AXTimeField": return "text_field"
    case "AXTextArea": return "text_area"
    case "AXLink": return "link"
    case "AXMenu": return "menu"
    case "AXMenuItem", "AXMenuBarItem": return "menu_item"
    case "AXMenuBar": return "menu_bar"
    case "AXTabGroup": return "tab_list"
    case "AXList": return "list"
    case "AXOutline": return "tree"
    case "AXRow": return subrole == "AXOutlineRow" ? "tree_item" : "row"
    case "AXCell": return "cell"
    case "AXTable", "AXGrid": return "table"
    case "AXSlider", "AXLevelIndicator": return "slider"
    case "AXIncrementor", "AXStepper": return "spin_button"
    case "AXScrollBar": return "scroll_bar"
    case "AXScrollArea": return "scroll_area"
    case "AXImage": return "image"
    case "AXStaticText": return "text"
    case "AXHeading": return "heading"
    case "AXWindow": return subrole == "AXDialog" || subrole == "AXSystemDialog" ? "dialog" : "window"
    case "AXSheet", "AXDrawer": return "dialog"
    case "AXGroup", "AXSplitGroup", "AXLayoutArea", "AXRadioGroup", "AXBrowser": return "group"
    case "AXToolbar": return "toolbar"
    case "AXProgressIndicator", "AXBusyIndicator": return "progress"
    case "AXWebArea": return "web_area"
    default: return "other"
    }
}

let interactiveRoles: Set<String> = ["button", "checkbox", "radio", "switch", "text_field", "text_area", "link",
                                     "menu_item", "tab", "combo_box", "slider", "spin_button", "tree_item", "list_item"]
let editableRoles: Set<String> = ["text_field", "text_area", "combo_box", "slider", "spin_button", "checkbox", "switch"]
let namedRoles: Set<String> = ["text", "heading", "image", "dialog", "window", "cell", "row", "tab_list", "menu", "toolbar", "web_area"]

final class Accessibility {
    private var epoch = 0
    private var cache: [String: AXUIElement] = [:]
    private var enhanced = Set<pid_t>()
    private let systemWide = AXUIElementCreateSystemWide()

    /// Общий потолок ожидания ответа приложения: зависшее приложение не должно держать помощник
    /// дольше, чем контроллер ждёт ответа (иначе помощник убивают и теряется всё зажатое).
    init() { AXUIElementSetMessagingTimeout(systemWide, 1.0) }

    func trusted() -> Bool { AXIsProcessTrusted() }

    func requireTrusted() throws {
        if !AXIsProcessTrusted() {
            throw failure("permission", "macOS не дала доступ к интерфейсу приложений. Разрешите chatrepo в System Settings → Privacy & Security → Accessibility и повторите.")
        }
    }

    func application(_ pid: pid_t, enhance: Bool = false) -> AXUIElement {
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 1.0)
        // ⚠ Chromium/Electron строят дерево доступности, только когда видят ассистивную технологию.
        // AXManualAccessibility — мягкий переключатель без побочных эффектов AXEnhancedUserInterface
        // (тот ломает анимации и перемещение окон у части приложений).
        // ⚠ Только когда элементы действительно читают: включённое дерево Chromium держит до конца
        // своей жизни, и список окон не должен нагружать каждый Chrome/Slack/VS Code у человека.
        if enhance, !enhanced.contains(pid) {
            enhanced.insert(pid)
            if AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue) == .success {
                // Electron и старые Chromium: дерево строится асинхронно — короткая пауза один раз.
                sleepMs(250)
            } else if chromiumFamily(pid) {
                // Только браузеры на Chromium: Safari, TextEdit и прочие родные приложения тоже не
                // принимают AXManualAccessibility, но дерево у них и так полное — трогать их незачем.
                // ⚠⚠ Chrome 152 AXManualAccessibility НЕ ПОДДЕРЖИВАЕТ (−25205, замерено): без
                // AXEnhancedUserInterface агент видел только рамку браузера — ни одного элемента страницы.
                // Его побочный эффект (анимации при перемещении окна) снимается в setFrame.
                AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
                let deadline = Date().addingTimeInterval(2.5)
                while Date() < deadline, !hasWebArea(app) { sleepMs(150) }
            }
        }
        return app
    }

    /// Семейство Chromium (Chrome, Edge, Brave, Opera, Vivaldi, Arc, Яндекс): в пакете «<Продукт> Framework.framework».
    private func chromiumFamily(_ pid: pid_t) -> Bool {
        guard let url = NSRunningApplication(processIdentifier: pid)?.bundleURL?.appendingPathComponent("Contents/Frameworks"),
              let items = try? FileManager.default.contentsOfDirectory(atPath: url.path) else { return false }
        return items.contains { $0.hasSuffix(" Framework.framework") }
    }

    /// Построено ли у браузера дерево страницы: есть ли в окнах AXWebArea (обход с ограничением).
    private func hasWebArea(_ app: AXUIElement) -> Bool {
        var seen = Set<AXUIElement>()
        var pending = axElements(app, kAXWindowsAttribute)
        while let element = pending.popLast() {
            if !seen.insert(element).inserted { continue }
            if axString(element, kAXRoleAttribute) == "AXWebArea" { return true }
            pending.append(contentsOf: axElements(element, kAXChildrenAttribute))
        }
        return false
    }

    func focusedApplicationPid() -> pid_t? {
        if AXIsProcessTrusted(), let app = axElement(systemWide, kAXFocusedApplicationAttribute) {
            var pid: pid_t = 0
            if AXUIElementGetPid(app, &pid) == .success { return pid }
        }
        return NSWorkspace.shared.frontmostApplication?.processIdentifier
    }

    func windowElement(pid: pid_t, id: CGWindowID, enhance: Bool = false) -> AXUIElement? {
        axElements(application(pid, enhance: enhance), kAXWindowsAttribute).first { windowNumber($0) == id }
    }

    // MARK: описание элемента

    func describe(_ element: AXUIElement, register: Bool) -> JSON? {
        guard let role = axString(element, kAXRoleAttribute) else { return nil }
        let subrole = axString(element, kAXSubroleAttribute)
        let normalized = normalizedRole(role, subrole)
        let secure = subrole == "AXSecureTextField"
        var name = nonEmptyText(axString(element, kAXTitleAttribute))
            ?? nonEmptyText(axString(element, kAXDescriptionAttribute))
        if name == nil, let title = axElement(element, kAXTitleUIElementAttribute) {
            name = nonEmptyText(axString(title, kAXValueAttribute)) ?? nonEmptyText(axString(title, kAXTitleAttribute))
        }
        if name == nil { name = nonEmptyText(axString(element, kAXPlaceholderValueAttribute)) }
        if name == nil, !secure, normalized == "text" { name = nonEmptyText(axString(element, kAXValueAttribute)) }
        var result: JSON = ["id": "", "role": normalized, "raw_role": subrole.map { "\(role)/\($0)" } ?? role,
                            "name": name ?? "", "secure": secure,
                            "enabled": axBool(element, kAXEnabledAttribute) ?? true,
                            "focused": axBool(element, kAXFocusedAttribute) ?? false]
        // ⚠⚠ ЗНАЧЕНИЕ ПОЛЯ ПАРОЛЯ НЕ ЧИТАЕТСЯ НИКОГДА — даже чтобы потом выбросить.
        if !secure, normalized != "text", let value = nonEmptyText(axString(element, kAXValueAttribute)) { result["value"] = value }
        if let help = nonEmptyText(axString(element, kAXHelpAttribute)), help != name { result["description"] = help }
        if let frame = axFrame(element), frame.width > 0, frame.height > 0 { result["bounds"] = rectJSON(frame) }
        var actions: [String] = []
        let names = axActions(element)
        if names.contains(kAXPressAction as String) || names.contains(kAXConfirmAction as String) { actions.append("press") }
        if axSettable(element, kAXFocusedAttribute) { actions.append("focus") }
        if editableRoles.contains(normalized), axSettable(element, kAXValueAttribute) { actions.append("set_value") }
        if names.contains(kAXShowMenuAction as String) { actions.append("show_menu") }
        if names.contains(kAXIncrementAction as String) { actions.append("increment") }
        if names.contains(kAXDecrementAction as String) { actions.append("decrement") }
        if axSettable(element, kAXSelectedAttribute) || names.contains(kAXPickAction as String) { actions.append("select") }
        if axSettable(element, "AXDisclosing") || axSettable(element, "AXExpanded") { actions.append(contentsOf: ["expand", "collapse"]) }
        if names.contains("AXScrollToVisible") { actions.append("scroll_into_view") }
        result["actions"] = actions
        var pid: pid_t = 0
        if AXUIElementGetPid(element, &pid) == .success { result["pid"] = Int(pid) }
        if register { result["id"] = self.register(element) }
        return result
    }

    private func matches(_ item: JSON, query: String?, role: String?) -> Bool {
        if let role = role, (item["role"] as? String) != role { return false }
        if let query = query {
            let haystack = [item["name"], item["value"], item["description"]].compactMap { $0 as? String }
            return haystack.contains { $0.range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) != nil }
        }
        if role != nil { return true }
        let normalized = item["role"] as? String ?? "other"
        if interactiveRoles.contains(normalized) { return true }
        // Группы Chromium объявляют AXValue и AXFocused изменяемыми у каждого контейнера — это не
        // повод показывать их агенту. Считаются только действия, которые что-то делают.
        let meaningful: Set<String> = ["press", "show_menu", "increment", "decrement", "select", "expand"]
        if !meaningful.isDisjoint(with: (item["actions"] as? [String]) ?? []) { return true }
        return namedRoles.contains(normalized) && !((item["name"] as? String) ?? "").isEmpty
    }

    // MARK: выборка

    func elements(_ params: JSON, windowIds: (pid_t, CGWindowID)? , focusedPid: pid_t?) throws -> JSON {
        try requireTrusted()
        epoch += 1
        cache.removeAll()
        let maxCount = try optionalNumber(params, "max").map { max(Int($0), 1) } ?? Int.max
        let maxDepth = try optionalNumber(params, "depth").map { max(Int($0), 1) } ?? Int.max
        let query = nonEmptyText(params["query"] as? String)
        let role = nonEmptyText(params["role"] as? String)
        var out: [JSON] = []
        var truncated = false
        var scopeWindow: JSON?
        var readErrors: [JSON] = []
        var readErrorCount = 0

        if let point = params["point"] as? JSON {
            let x = try number(point, "x"), y = try number(point, "y")
            var hit: AXUIElement?
            let error = AXUIElementCopyElementAtPosition(systemWide, Float(x), Float(y), &hit)
            guard error == .success, var current = hit else { throw axError(error, "элемент под точкой") }
            var ancestors = Set<AXUIElement>()
            while ancestors.insert(current).inserted {
                if let item = describe(current, register: true) { out.append(item) }
                guard let parent = axElement(current, kAXParentAttribute) else { break }
                if axString(parent, kAXRoleAttribute) == "AXApplication" { break }
                current = parent
            }
            return ["epoch": epoch, "truncated": false, "elements": out]
        }

        var roots: [AXUIElement] = []
        if let (pid, id) = windowIds {
            guard let window = windowElement(pid: pid, id: id, enhance: true) else {
                throw failure("not_found", "окно \(id) не найдено в дереве доступности; обновите список окон")
            }
            roots = [window]
            // macOS owns the menu bar at application level, outside the window tree.
            // A window-scoped menu lookup must still search that window's application.
            if let role = role, ["menu", "menu_item", "menu_bar"].contains(role),
               let bar = axElement(application(pid, enhance: true), kAXMenuBarAttribute) {
                roots.append(bar)
            }
            scopeWindow = ["id": String(id), "title": axString(window, kAXTitleAttribute) ?? ""]
        } else {
            let pid: pid_t
            if let raw = try optionalNumber(params, "pid") { pid = pid_t(raw) } else {
                guard let focused = focusedPid else { throw failure("not_found", "нет приложения в фокусе") }
                pid = focused
            }
            let app = application(pid, enhance: true)
            if params["pid"] == nil, let window = axElement(app, kAXFocusedWindowAttribute) ?? axElement(app, kAXMainWindowAttribute) {
                roots = [window]
                scopeWindow = ["id": windowNumber(window).map { String($0) } ?? "", "title": axString(window, kAXTitleAttribute) ?? ""]
            } else {
                roots = axElements(app, kAXWindowsAttribute)
            }
            // Меню нужны только когда их просят: полная строка меню — сотни пунктов.
            if let role = role, ["menu", "menu_item", "menu_bar"].contains(role), let bar = axElement(app, kAXMenuBarAttribute) {
                roots.append(bar)
            }
            if roots.isEmpty { throw failure("not_found", "у приложения \(pid) нет окон, доступных для чтения") }
        }

        var stack: [(AXUIElement, Int)] = roots.reversed().map { ($0, 0) }
        var visited = Set<AXUIElement>()
        while let (element, depth) = stack.popLast() {
            if !visited.insert(element).inserted { continue }
            if var item = describe(element, register: false), matches(item, query: query, role: role) {
                if out.count >= maxCount { truncated = true; break }
                item["id"] = register(element)
                out.append(item)
            }
            if depth + 1 >= maxDepth { truncated = true; continue }
            // A leaf can legitimately omit AXChildren. A timeout/permission/API
            // failure is different: retain it instead of reporting an empty tree.
            var value: CFTypeRef?
            let error = AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value)
            if error != .success && error != .noValue && error != .attributeUnsupported {
                readErrorCount += 1
                readErrors.append(["attribute": kAXChildrenAttribute,
                    "code": error.rawValue, "role": axString(element, kAXRoleAttribute) ?? "unknown"])
            }
            let children: [AXUIElement]
            if let value = value, CFGetTypeID(value) == CFArrayGetTypeID() {
                children = (value as! [AnyObject]).compactMap {
                    CFGetTypeID($0) == AXUIElementGetTypeID() ? ($0 as! AXUIElement) : nil
                }
            } else { children = [] }
            for child in children.reversed() { stack.append((child, depth + 1)) }
        }
        var result: JSON = ["epoch": epoch, "truncated": truncated, "elements": out]
        result["diagnostics"] = ["visited": visited.count, "read_error_count": readErrorCount,
            "read_errors": readErrors, "complete": !truncated && readErrorCount == 0]
        if let scopeWindow = scopeWindow { result["window"] = scopeWindow }
        return result
    }

    func register(_ element: AXUIElement) -> String {
        let id = "e\(epoch).\(cache.count + 1)"
        cache[id] = element
        return id
    }

    func element(_ id: String) throws -> AXUIElement {
        guard let element = cache[id] else {
            throw failure("stale", "элемент \(id) не из последней выборки; вызовите computer_elements заново")
        }
        return element
    }

    func act(_ params: JSON) throws -> JSON {
        try requireTrusted()
        let id = try string(params, "id")
        let action = try string(params, "action")
        let element = try element(id)
        func perform(_ name: String) throws {
            let error = AXUIElementPerformAction(element, name as CFString)
            // ⚠⚠ «НЕ УСПЕЛО ОТВЕТИТЬ» — НЕ «НЕ НАЖАЛОСЬ». Кнопка, открывшая модальное окно, часто держит
            // ответ, пока окно не закроют. Слова «повторите» тут привели бы ко второму нажатию «Отправить».
            if error == .cannotComplete {
                throw failure("uncertain", "приложение не подтвердило «\(action)» вовремя — действие МОГЛО выполниться (например, открылось окно). Посмотрите экран, прежде чем повторять.")
            }
            if error != .success { throw axError(error, action) }
        }
        func set(_ attribute: String, _ value: CFTypeRef) throws {
            let error = AXUIElementSetAttributeValue(element, attribute as CFString, value)
            if error != .success { throw axError(error, action) }
        }
        let names = axActions(element)
        switch action {
        case "locate": break
        case "press":
            if names.contains(kAXPressAction as String) { try perform(kAXPressAction) }
            else if names.contains(kAXConfirmAction as String) { try perform(kAXConfirmAction) }
            else if names.contains(kAXPickAction as String) { try perform(kAXPickAction) }
            else { throw unsupported(element, action) }
        case "focus": try set(kAXFocusedAttribute, kCFBooleanTrue)
        case "set_value":
            guard let value = params["value"] else { throw failure("bad_request", "для set_value нужно value") }
            if let number = value as? NSNumber, !(value is String) { try set(kAXValueAttribute, number) }
            else { try set(kAXValueAttribute, String(describing: value) as CFString) }
        case "show_menu": try perform(kAXShowMenuAction)
        case "increment": try perform(kAXIncrementAction)
        case "decrement": try perform(kAXDecrementAction)
        case "select":
            if axSettable(element, kAXSelectedAttribute) { try set(kAXSelectedAttribute, kCFBooleanTrue) }
            else { try perform(kAXPickAction) }
        case "expand", "collapse":
            let flag = action == "expand" ? kCFBooleanTrue! : kCFBooleanFalse!
            if axSettable(element, "AXDisclosing") { try set("AXDisclosing", flag) }
            else if axSettable(element, "AXExpanded") { try set("AXExpanded", flag) }
            else { throw unsupported(element, action) }
        case "scroll_into_view": try perform("AXScrollToVisible")
        case "read":
            // Полный текст поля/документа — для проверки, что введено; значение поля пароля не читается никогда.
            var result: JSON = [:]
            var fresh = describe(element, register: false) ?? [:]
            fresh["id"] = id
            result["element"] = fresh
            if (fresh["secure"] as? Bool) == true {
                result["text"] = NSNull()
                return result
            }
            let full = axString(element, kAXValueAttribute) ?? axString(element, kAXTitleAttribute) ?? ""
            result["text"] = full
            if let selected = axString(element, kAXSelectedTextAttribute), !selected.isEmpty { result["selected_text"] = selected }
            if let range = axValue(element, kAXSelectedTextRangeAttribute), CFGetTypeID(range) == AXValueGetTypeID() {
                var cf = CFRange()
                if AXValueGetValue(range as! AXValue, .cfRange, &cf) { result["caret"] = cf.location }
            }
            return result
        case "select_text": return try selectText(element, id: id, params: params)
        default: throw failure("bad_request", "неизвестное действие с элементом «\(action)»")
        }
        var result: JSON = [:]
        if var fresh = describe(element, register: false) {
            fresh["id"] = id
            result["element"] = fresh
        }
        return result
    }

    /// select_text: подстрока (n-е вхождение), смещения или весь текст. Смещения — в единицах UTF-16, как у AXSelectedTextRange;
    /// после записи диапазон читается обратно, чтобы «приняло, но не применило» не выдавалось за успех.
    private func selectText(_ element: AXUIElement, id: String, params: JSON) throws -> JSON {
        var fresh = describe(element, register: false) ?? [:]
        fresh["id"] = id
        if (fresh["secure"] as? Bool) == true { throw failure("secure_field", "поле пароля: его текст не выделяется и не читается") }
        guard axSettable(element, kAXSelectedTextRangeAttribute) else {
            throw failure("unsupported", "элемент не даёт менять выделение текста (AXSelectedTextRange не записывается)")
        }
        let full = (axString(element, kAXValueAttribute) ?? "") as NSString
        var target = CFRange(location: 0, length: 0)
        if let needle = params["text"] as? String, !needle.isEmpty {
            let wanted = try actionCount(params, "occurrence", fallback: 1, minimum: 1)
            var from = 0
            var found = 0
            var match = NSRange(location: NSNotFound, length: 0)
            while from < full.length {
                let range = full.range(of: needle, options: [], range: NSRange(location: from, length: full.length - from))
                if range.location == NSNotFound { break }
                found += 1
                if found == wanted { match = range; break }
                from = range.location + range.length
            }
            if match.location == NSNotFound {
                throw failure("not_found", "фрагмент «\(needle)» (вхождение \(wanted)) не найден в тексте элемента; вхождений: \(found)")
            }
            target = CFRange(location: match.location, length: match.length)
        } else if (params["all"] as? Bool) == true {
            target = CFRange(location: 0, length: full.length)
        } else {
            guard params["start"] != nil else { throw failure("bad_request", "нужно одно из: text, start, all") }
            let start = try actionCount(params, "start", fallback: 0, minimum: 0)
            let end = try actionCount(params, "end", fallback: start, minimum: start)
            if end > full.length { throw failure("bad_request", "end \(end) за пределами текста (длина \(full.length))") }
            target = CFRange(location: start, length: end - start)
        }
        guard let value = AXValueCreate(.cfRange, &target) else { throw failure("failed", "не удалось собрать диапазон выделения") }
        let error = AXUIElementSetAttributeValue(element, kAXSelectedTextRangeAttribute as CFString, value)
        if error != .success { throw axError(error, "select_text") }
        var applied = CFRange(location: -1, length: -1)
        if let raw = axValue(element, kAXSelectedTextRangeAttribute), CFGetTypeID(raw) == AXValueGetTypeID() {
            var read = CFRange()
            if AXValueGetValue(raw as! AXValue, .cfRange, &read) { applied = read }
        }
        if applied.location != target.location || applied.length != target.length {
            throw failure("failed", "приложение приняло запрос, но не применило выделение (сейчас \(applied.location)+\(applied.length), нужно \(target.location)+\(target.length))")
        }
        return ["element": fresh, "selection": ["start": target.location, "end": target.location + target.length],
                "selected_text": axString(element, kAXSelectedTextAttribute) ?? ""]
    }

    private func unsupported(_ element: AXUIElement, _ action: String) -> DriverError {
        let available = (describe(element, register: false)?["actions"] as? [String]) ?? []
        return failure("unsupported", "элемент не поддерживает «\(action)»; доступно: \(available.isEmpty ? "ничего — кликните по координатам центра" : available.joined(separator: ", "))")
    }

    func focused() -> JSON {
        guard AXIsProcessTrusted() else { return ["element": NSNull(), "app": NSNull()] }
        var result: JSON = ["element": NSNull(), "app": NSNull()]
        if let element = axElement(systemWide, kAXFocusedUIElementAttribute), let item = describe(element, register: false) {
            result["element"] = item
        }
        if let pid = focusedApplicationPid() {
            result["app"] = ["name": NSRunningApplication(processIdentifier: pid)?.localizedName ?? "", "pid": Int(pid)]
        }
        return result
    }
}

// MARK: - Окна

final class Windows {
    let ax: Accessibility
    init(ax: Accessibility) { self.ax = ax }

    private func cgList() -> [JSON] {
        (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [JSON]) ?? []
    }

    func info(_ id: CGWindowID) -> JSON? {
        (CGWindowListCopyWindowInfo([.optionIncludingWindow], id) as? [JSON])?.first
    }

    func list() -> JSON {
        let trusted = ax.trusted()
        let focusedPid = ax.focusedApplicationPid()
        var focusedWindow: CGWindowID?
        if trusted, let pid = focusedPid, let window = axElement(ax.application(pid), kAXFocusedWindowAttribute) {
            focusedWindow = windowNumber(window)
        }
        var titles: [pid_t: [CGWindowID: String]] = [:]
        func axTitle(_ pid: pid_t, _ id: CGWindowID) -> String? {
            guard trusted else { return nil }
            if titles[pid] == nil {
                var map: [CGWindowID: String] = [:]
                for window in axElements(ax.application(pid), kAXWindowsAttribute) {
                    if let number = windowNumber(window) { map[number] = axString(window, kAXTitleAttribute) ?? "" }
                }
                titles[pid] = map
            }
            return titles[pid]?[id]
        }
        // ⚠ Имя приложения локализовано («Терминал», а не «Terminal»): уровни доступа сверяются и по
        // bundle id, иначе правило «Terminal» не узнаёт окно русской macOS (живая проверка).
        var apps: [pid_t: (bundle: String, regular: Bool)] = [:]
        func appInfo(_ pid: pid_t) -> (bundle: String, regular: Bool) {
            if let cached = apps[pid] { return cached }
            let app = NSRunningApplication(processIdentifier: pid)
            let value = (bundle: app?.bundleIdentifier ?? "", regular: app?.activationPolicy == .regular)
            apps[pid] = value
            return value
        }
        var windows: [JSON] = []
        var seen = Set<CGWindowID>()
        for raw in cgList() {
            guard (raw[kCGWindowLayer as String] as? Int) == 0,
                  let number = raw[kCGWindowNumber as String] as? Int,
                  let pid = raw[kCGWindowOwnerPID as String] as? Int,
                  let boundsDict = raw[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsDict) else { continue }
            if (raw[kCGWindowAlpha as String] as? Double ?? 1) <= 0.01 || bounds.width < 40 || bounds.height < 20 { continue }
            let id = CGWindowID(number)
            let title = nonEmptyText(raw[kCGWindowName as String] as? String) ?? nonEmptyText(axTitle(pid_t(pid), id)) ?? ""
            seen.insert(id)
            windows.append(["id": String(id), "title": title, "app": raw[kCGWindowOwnerName as String] as? String ?? "",
                            "bundle_id": appInfo(pid_t(pid)).bundle,
                            "pid": pid, "bounds": rectJSON(bounds), "focused": focusedWindow == id, "minimized": false])
        }
        // Свёрнутые окна в экранном списке не видны; их знает только AX.
        if trusted {
            for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
                for window in axElements(ax.application(app.processIdentifier), kAXWindowsAttribute) {
                    guard axBool(window, kAXMinimizedAttribute) == true, let id = windowNumber(window), !seen.contains(id) else { continue }
                    seen.insert(id)
                    windows.append(["id": String(id), "title": axString(window, kAXTitleAttribute) ?? "", "app": app.localizedName ?? "",
                                    "bundle_id": app.bundleIdentifier ?? "",
                                    "pid": Int(app.processIdentifier), "bounds": axFrame(window).map(rectJSON) ?? NSNull(),
                                    "focused": false, "minimized": true])
                }
            }
        }
        // ⚠ Окна на ДРУГОМ рабочем столе (Space) и полноэкранные в экранном списке не значатся — агент их
        // не видел вовсе (живая проверка: полноэкранный Терминал). Отдаются с offscreen: true: снимать их
        // нельзя, пока окно не выведено вперёд (focus переключает рабочий стол). Служебные полосы и окна
        // без заголовка отсекаются размером и именем.
        let everything = (CGWindowListCopyWindowInfo([.optionAll, .excludeDesktopElements], kCGNullWindowID) as? [JSON]) ?? []
        for raw in everything {
            guard (raw[kCGWindowLayer as String] as? Int) == 0,
                  (raw[kCGWindowIsOnscreen as String] as? Bool) != true,
                  let number = raw[kCGWindowNumber as String] as? Int,
                  let pid = raw[kCGWindowOwnerPID as String] as? Int,
                  let boundsDict = raw[kCGWindowBounds as String] as? NSDictionary,
                  let bounds = CGRect(dictionaryRepresentation: boundsDict) else { continue }
            let id = CGWindowID(number)
            if seen.contains(id) || (raw[kCGWindowAlpha as String] as? Double ?? 1) <= 0.01 || bounds.width < 100 || bounds.height < 60 { continue }
            let info = appInfo(pid_t(pid))
            guard info.regular, let title = nonEmptyText(raw[kCGWindowName as String] as? String) ?? nonEmptyText(axTitle(pid_t(pid), id)), !title.isEmpty else { continue }
            seen.insert(id)
            windows.append(["id": String(id), "title": title, "app": raw[kCGWindowOwnerName as String] as? String ?? "",
                            "bundle_id": info.bundle, "pid": pid, "bounds": rectJSON(bounds),
                            "focused": false, "minimized": false, "offscreen": true])
        }
        var result: JSON = ["windows": windows]
        if let pid = focusedPid, let app = NSRunningApplication(processIdentifier: pid) {
            result["frontmost_app"] = ["name": app.localizedName ?? "", "pid": Int(pid), "bundle_id": app.bundleIdentifier ?? ""]
        }
        return result
    }

    func owner(_ id: CGWindowID) throws -> pid_t {
        if let raw = info(id), let pid = raw[kCGWindowOwnerPID as String] as? Int { return pid_t(pid) }
        // ⚠ Окно на другом рабочем столе: запрос по одному окну его не отдаёт, полный список — отдаёт
        // (живая проверка на Telegram: список окон его видел, а «вывести вперёд» падало на «не найдено»).
        let everything = (CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [JSON]) ?? []
        if let raw = everything.first(where: { ($0[kCGWindowNumber as String] as? Int) == Int(id) }),
           let pid = raw[kCGWindowOwnerPID as String] as? Int { return pid_t(pid) }
        // Свёрнутое окно в CG может не отдаваться; ищем по AX.
        for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
            if ax.windowElement(pid: app.processIdentifier, id: id) != nil { return app.processIdentifier }
        }
        throw failure("not_found", "окно \(id) не найдено; обновите список окон через computer_windows")
    }

    func control(_ params: JSON) throws -> JSON {
        try ax.requireTrusted()
        guard let id = CGWindowID(try string(params, "id")) else { throw failure("bad_request", "id окна должен быть числом-строкой") }
        let action = try string(params, "action")
        let pid = try owner(id)
        // ⚠ Свежеоткрытое окно появляется в списке окон раньше, чем в дереве доступности приложения:
        // без ожидания «передвинь только что открытое окно» падало на гонке (замечено живой проверкой).
        var found = ax.windowElement(pid: pid, id: id)
        let deadline = Date().addingTimeInterval(1.5)
        while found == nil, Date() < deadline {
            sleepMs(100)
            found = ax.windowElement(pid: pid, id: id)
        }
        if found == nil, action == "focus" || action == "restore" {
            // Окно на другом рабочем столе: дерево доступности его не отдаёт, пока стол не переключится.
            // Вывести приложение вперёд — macOS сама перейдёт на его рабочий стол — и искать окно снова.
            // Скрытое приложение (⌘H) тоже «вне экрана»: сначала показать.
            if let running = NSRunningApplication(processIdentifier: pid), running.isHidden { running.unhide() }
            AXUIElementSetAttributeValue(ax.application(pid), kAXFrontmostAttribute as CFString, kCFBooleanTrue)
            NSRunningApplication(processIdentifier: pid)?.activate(options: [])
            let later = Date().addingTimeInterval(2.5)
            while found == nil, Date() < later {
                sleepMs(120)
                found = ax.windowElement(pid: pid, id: id)
            }
        }
        guard let window = found else {
            throw failure("not_found", "окно \(id) не доступно для управления: приложение не отдало его в дерево доступности; повторите через секунду")
        }
        func set(_ attribute: String, _ value: CFTypeRef) throws {
            let error = AXUIElementSetAttributeValue(window, attribute as CFString, value)
            if error != .success { throw axError(error, action) }
        }
        func press(_ buttonAttribute: String) throws {
            guard let button = axElement(window, buttonAttribute) else { throw failure("unsupported", "у окна нет такой кнопки (\(action))") }
            let error = AXUIElementPerformAction(button, kAXPressAction as CFString)
            if error != .success { throw axError(error, action) }
        }
        switch action {
        case "focus", "restore":
            if axBool(window, kAXMinimizedAttribute) == true { try set(kAXMinimizedAttribute, kCFBooleanFalse) }
            // ⚠ activate(options:) с 14-й версии не отбирает фокус у активного приложения, а мы —
            // фоновый процесс. Надёжный путь — AXFrontmost приложения и AXRaise окна.
            AXUIElementSetAttributeValue(ax.application(pid), kAXFrontmostAttribute as CFString, kCFBooleanTrue)
            AXUIElementPerformAction(window, kAXRaiseAction as CFString)
            AXUIElementSetAttributeValue(window, kAXMainAttribute as CFString, kCFBooleanTrue)
            if action == "focus" {
                let deadline = Date().addingTimeInterval(1)
                while Date() < deadline, ax.focusedApplicationPid() != pid { sleepMs(50) }
                if ax.focusedApplicationPid() != pid {
                    throw failure("blocked", "macOS не вывела окно на передний план; попробуйте кликнуть по нему")
                }
            }
        case "minimize": try set(kAXMinimizedAttribute, kCFBooleanTrue)
        case "maximize":
            guard let frame = axFrame(window) else { throw failure("unsupported", "у окна нет геометрии") }
            let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
            let screen = NSScreen.screens.first { screen in
                let quartz = CGRect(x: screen.frame.minX, y: primaryHeight - screen.frame.maxY, width: screen.frame.width, height: screen.frame.height)
                return quartz.contains(CGPoint(x: frame.midX, y: frame.midY))
            } ?? NSScreen.main
            guard let visible = screen?.visibleFrame else { throw failure("failed", "не найден экран окна") }
            let target = CGRect(x: visible.minX, y: primaryHeight - visible.maxY, width: visible.width, height: visible.height)
            try setFrame(window, target)
        case "close": try press(kAXCloseButtonAttribute)
        case "set_bounds":
            guard let bounds = params["bounds"] as? JSON else { throw failure("bad_request", "для set_bounds нужны bounds") }
            let target = CGRect(x: try number(bounds, "x"), y: try number(bounds, "y"),
                                width: try number(bounds, "width"), height: try number(bounds, "height"))
            if target.width < 20 || target.height < 20 { throw failure("bad_request", "слишком маленький размер окна") }
            try setFrame(window, target)
        default: throw failure("bad_request", "неизвестное действие с окном «\(action)»")
        }
        sleepMs(120)
        let entry = (list()["windows"] as? [JSON])?.first { ($0["id"] as? String) == String(id) }
        return ["window": entry ?? ["id": String(id), "closed": action == "close"]]
    }

    private func setFrame(_ window: AXUIElement, _ rect: CGRect) throws {
        var origin = rect.origin
        var size = rect.size
        guard let position = AXValueCreate(.cgPoint, &origin), let extent = AXValueCreate(.cgSize, &size) else {
            throw failure("failed", "не удалось подготовить геометрию окна")
        }
        // AXEnhancedUserInterface (включается для Chrome ради дерева страницы) превращает перемещение окна
        // в анимацию, и размер «не доезжает» — на время перемещения выключается и возвращается обратно.
        var pid: pid_t = 0
        AXUIElementGetPid(window, &pid)
        let app = AXUIElementCreateApplication(pid)
        let enhancedUI = axBool(app, "AXEnhancedUserInterface") == true
        if enhancedUI { AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanFalse) }
        defer { if enhancedUI { AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue) } }
        // Позиция — до и после размера: иначе окно у края экрана упирается и не растягивается.
        AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, position)
        let error = AXUIElementSetAttributeValue(window, kAXSizeAttribute as CFString, extent)
        AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, position)
        if error != .success { throw axError(error, "размер окна") }
    }
}

// MARK: - Запуск приложений

func resolveApplication(_ name: String) -> URL? {
    let fm = FileManager.default
    let expanded = (name as NSString).expandingTildeInPath
    if expanded.hasPrefix("/"), fm.fileExists(atPath: expanded) { return URL(fileURLWithPath: expanded) }
    if name.contains("."), !name.contains(" "), !name.contains("/"),
       let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: name) { return url }
    let wanted = name.lowercased().replacingOccurrences(of: ".app", with: "")
    let dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities", "/Applications/Utilities",
                NSHomeDirectory() + "/Applications", "/System/Library/CoreServices"]
    var partial: URL?
    for dir in dirs {
        guard let items = try? fm.contentsOfDirectory(atPath: dir) else { continue }
        for item in items where item.hasSuffix(".app") {
            let path = dir + "/" + item
            let base = String(item.dropLast(4)).lowercased()
            let display = fm.displayName(atPath: path).lowercased().replacingOccurrences(of: ".app", with: "")
            if base == wanted || display == wanted { return URL(fileURLWithPath: path) }
            if partial == nil, base.hasPrefix(wanted) || display.hasPrefix(wanted) { partial = URL(fileURLWithPath: path) }
        }
    }
    if let partial = partial { return partial }
    // Локализованные имена («Калькулятор») знает Spotlight.
    let escaped = name.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "'", with: "\\'")
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/mdfind")
    process.arguments = ["kMDItemContentType == 'com.apple.application-bundle' && (kMDItemDisplayName == '\(escaped)*'cd || kMDItemFSName == '\(escaped)*'cd)"]
    let pipe = Pipe()
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    do { try process.run() } catch { return nil }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let first = String(data: data, encoding: .utf8)?.split(separator: "\n").first.map(String.init)
    return first.map { URL(fileURLWithPath: $0) }
}

func launch(_ params: JSON) throws -> JSON {
    let name = try string(params, "app").trimmingCharacters(in: .whitespaces)
    if name.isEmpty { throw failure("bad_request", "не указано приложение") }
    let args = try stringList(params, "args")
    guard let url = resolveApplication(name) else {
        throw failure("not_found", "приложение «\(name)» не найдено в /Applications и Spotlight; уточните имя или путь")
    }
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.arguments = args
    configuration.activates = true
    let done = DispatchSemaphore(value: 0)
    var launched: NSRunningApplication?
    var launchError: Error?
    NSWorkspace.shared.openApplication(at: url, configuration: configuration) { app, error in
        launched = app
        launchError = error
        done.signal()
    }
    if done.wait(timeout: .now() + 20) == .timedOut {
        throw failure("failed", "приложение «\(name)» не запустилось за 20 секунд")
    }
    if let error = launchError { throw failure("failed", "macOS не запустила «\(name)»: \(error.localizedDescription)") }
    var result: JSON = ["app": launched?.localizedName ?? FileManager.default.displayName(atPath: url.path)]
    if let pid = launched?.processIdentifier { result["pid"] = Int(pid) }
    return result
}

// MARK: - Фоновый ввод, жесты

extension Input {
    /**
     * ⚠⚠ ФОНОВЫЙ ВВОД НЕ ТРОГАЕТ МЫШЬ И ФОКУС ЧЕЛОВЕКА: события адресуются процессу окна
     * (`postToPid`), а не общему потоку ввода. Это лучшее усилие — часть приложений чужие события в
     * фоне не принимает, поэтому ответ честно говорит «доставлено», а не «сделано».
     */
    func background(pid: pid_t, windowId: CGWindowID, params: JSON) throws -> JSON {
        let action = try string(params, "action")
        switch action {
        case "type":
            let text = try string(params, "text")
            let clear = params["clear"] as? Bool ?? false
            let submit = params["submit"] as? Bool ?? false
            let newline = params["newline"] as? String ?? "enter"
            if text.isEmpty && !clear && !submit { throw failure("bad_request", "текст пуст") }
            if clear {
                try postKey(pid, 0, .maskCommand, true); try postKey(pid, 0, .maskCommand, false)
                if text.isEmpty { try postKey(pid, 51, [], true); try postKey(pid, 51, [], false) }
            }
            for char in text {
                if interrupted { throw failure("cancelled", "ввод прерван") }
                if char == "\n" || char == "\r\n" || char == "\r" {
                    let code: CGKeyCode = newline == "none" ? 49 : 36
                    let flags: CGEventFlags = newline == "shift_enter" ? .maskShift : []
                    try postKey(pid, code, flags, true); try postKey(pid, code, flags, false); continue
                }
                if char == "\t" { try postKey(pid, 48, [], true); try postKey(pid, 48, [], false); continue }
                var units = Array(String(char).utf16)
                for down in [true, false] {
                    guard let event = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { throw failure("failed", "macOS не создала событие") }
                    event.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
                    event.postToPid(pid)
                    sleepMs(3)
                }
            }
            if submit { try postKey(pid, 36, [], true); try postKey(pid, 36, [], false) }
        case "key":
            let key = try string(params, "key")
            var flags: CGEventFlags = []
            for name in try stringList(params, "modifiers") { flags.insert(try modifierFlag(name).0) }
            switch try target(key) {
            case .code(let code, let implied):
                for _ in 0..<(try actionCount(params, "repeat", fallback: 1, minimum: 1)) {
                if interrupted { throw failure("cancelled", "ввод прерван") }
                try postKey(pid, code, flags.union(implied), true)
                try postKey(pid, code, flags.union(implied), false)
                }
            case .unicode(let text):
                for _ in 0..<(try actionCount(params, "repeat", fallback: 1, minimum: 1)) {
                    _ = try background(pid: pid, windowId: windowId, params: ["action": "type", "text": text])
                }
            case .media:
                throw failure("unsupported", "медиаклавиши в фоне не адресуются окну")
            }
        case "click":
            let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
            let clicks = try actionCount(params, "clicks", fallback: 1, minimum: 1)
            var flags: CGEventFlags = []
            for name in try stringList(params, "modifiers") { flags.insert(try modifierFlag(name).0) }
            let raw = (params["button"] as? String) ?? "left"
            let button = raw == "right" ? 1 : raw == "middle" ? 2 : 0
            let (down, up, _, cg) = types(button)
            for index in 1...clicks {
                for type in [down, up] {
                    guard let event = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point, mouseButton: cg) else {
                        throw failure("failed", "macOS не создала событие")
                    }
                    event.setIntegerValueField(.mouseEventClickState, value: Int64(index))
                    event.flags = flags
                    // Окно-получатель: без этих полей приложение ищет окно под ФИЗИЧЕСКИМ курсором.
                    event.setIntegerValueField(CGEventField(rawValue: 91)!, value: Int64(windowId))
                    event.setIntegerValueField(CGEventField(rawValue: 92)!, value: Int64(windowId))
                    event.postToPid(pid)
                    sleepMs(12)
                }
            }
        default:
            throw failure("bad_request", "фоновое действие должно быть type, key или click")
        }
        return ["delivered": true, "method": "postToPid",
                "note": "События отправлены процессу окна в фоне. Часть приложений (игры, некоторые Electron/Java) фоновый ввод не принимает — проверьте результат снимком."]
    }

    private func postKey(_ pid: pid_t, _ code: CGKeyCode, _ flags: CGEventFlags, _ down: Bool) throws {
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: down) else { throw failure("failed", "macOS не создала событие") }
        event.flags = flags
        event.postToPid(pid)
        sleepMs(6)
    }

    /// Сенсорного экрана у Mac нет: касание, двойное касание, долгое нажатие и смахивание
    /// эмулируются мышью (и так и названы в ответе); щипок и поворот — честный отказ.
    func touch(_ params: JSON) throws -> JSON {
        let gesture = try string(params, "gesture")
        let x = try number(params, "x"), y = try number(params, "y")
        switch gesture {
        case "tap": _ = try perform(["action": "click", "x": x, "y": y, "button": "left", "clicks": 1])
        case "double_tap": _ = try perform(["action": "click", "x": x, "y": y, "button": "left", "clicks": 2])
        case "long_press":
            _ = try perform(["action": "click", "x": x, "y": y, "button": "left", "clicks": 1,
                             "hold_ms": try optionalNumber(params, "duration_ms") ?? 800])
        case "swipe":
            _ = try perform(["action": "drag", "x": x, "y": y, "toX": try number(params, "toX"), "toY": try number(params, "toY"),
                             "button": "left", "duration_ms": try optionalNumber(params, "duration_ms") ?? 300])
        case "pinch", "rotate":
            throw failure("unsupported", "у Mac нет сенсорного экрана, а жесты трекпада (щипок, поворот) синтезировать нельзя; для масштаба используйте command и +/-, или прокрутку с command")
        default:
            throw failure("bad_request", "неизвестный жест «\(gesture)»")
        }
        return ["emulated": true]
    }
}

// MARK: - Распознавание текста, буфер обмена, окно под точкой

/// Vision: текст с боксами в пикселях переданной картинки. Русский — с macOS 13, английский — всегда.
func recognizeText(_ params: JSON) throws -> JSON {
    let encoded = try string(params, "image_b64")
    guard let data = Data(base64Encoded: encoded), let source = CGImageSourceCreateWithData(data as CFData, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw failure("bad_request", "image_b64 не является картинкой PNG или JPEG")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let wanted = (try stringList(params, "languages")).map { $0 == "ru" ? "ru-RU" : $0 == "en" ? "en-US" : $0 }
    let supported = (try? request.supportedRecognitionLanguages()) ?? ["en-US"]
    let desired = wanted.isEmpty ? ["ru-RU", "en-US"] : wanted
    let languages = desired.filter { supported.contains($0) }
    request.recognitionLanguages = languages.isEmpty ? ["en-US"] : languages
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch {
        throw failure("failed", "Vision не распознал текст: \(error.localizedDescription)")
    }
    let width = Double(image.width), height = Double(image.height)
    var lines: [JSON] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let rect = CGRect(x: box.minX * width, y: (1 - box.maxY) * height, width: box.width * width, height: box.height * height)
        lines.append(["text": candidate.string, "confidence": Double(candidate.confidence), "bounds": rectJSON(rect)])
    }
    lines.sort { a, b in
        let ra = a["bounds"] as! JSON, rb = b["bounds"] as! JSON
        let ay = ra["y"] as! Int, by = rb["y"] as! Int
        if abs(ay - by) > 6 { return ay < by }
        return (ra["x"] as! Int) < (rb["x"] as! Int)
    }
    var result: JSON = ["engine": "vision", "lines": lines, "text": lines.compactMap { $0["text"] as? String }.joined(separator: "\n")]
    let missing = desired.filter { !supported.contains($0) }
    if !missing.isEmpty { result["note"] = "Vision на этой macOS не знает языки: \(missing.joined(separator: ", "))" }
    return result
}

func clipboardFiles(_ params: JSON) throws -> JSON {
    let pasteboard = NSPasteboard.general
    switch try string(params, "action") {
    case "get":
        let urls = pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        return ["paths": urls.map { $0.path }]
    case "set":
        let paths = try stringList(params, "paths")
        if paths.isEmpty { throw failure("bad_request", "нужен хотя бы один путь") }
        var urls: [NSURL] = []
        for path in paths {
            guard path.hasPrefix("/") else { throw failure("bad_request", "путь должен быть абсолютным: \(path)") }
            guard FileManager.default.fileExists(atPath: path) else { throw failure("not_found", "файл не найден: \(path)") }
            urls.append(NSURL(fileURLWithPath: path))
        }
        pasteboard.clearContents()
        guard pasteboard.writeObjects(urls) else { throw failure("failed", "macOS не приняла файлы в буфер обмена") }
        return ["count": urls.count]
    default:
        throw failure("bad_request", "action должен быть get или set")
    }
}

/// Запущенные приложения с интерфейсом (Dock): то, что видит NSWorkspace, включая скрытые и без окон.
func runningApps() -> JSON {
    let front = NSWorkspace.shared.frontmostApplication?.processIdentifier
    var apps: [JSON] = []
    for app in NSWorkspace.shared.runningApplications where app.activationPolicy == .regular {
        var item: JSON = ["name": app.localizedName ?? "", "pid": Int(app.processIdentifier),
                          "hidden": app.isHidden, "focused": app.processIdentifier == front]
        if let bundle = app.bundleIdentifier { item["bundle_id"] = bundle }
        if let path = app.bundleURL?.path { item["path"] = path }
        apps.append(item)
    }
    return ["apps": apps]
}

func appAt(_ params: JSON) throws -> JSON {
    let point = CGPoint(x: try number(params, "x"), y: try number(params, "y"))
    let list = (CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [JSON]) ?? []
    for raw in list {
        guard (raw[kCGWindowLayer as String] as? Int) == 0,
              (raw[kCGWindowAlpha as String] as? Double ?? 1) > 0.01,
              let boundsDict = raw[kCGWindowBounds as String] as? NSDictionary,
              let bounds = CGRect(dictionaryRepresentation: boundsDict), bounds.contains(point),
              let pid = raw[kCGWindowOwnerPID as String] as? Int,
              let number = raw[kCGWindowNumber as String] as? Int else { continue }
        let app = NSRunningApplication(processIdentifier: pid_t(pid))
        return ["app": ["name": app?.localizedName ?? (raw[kCGWindowOwnerName as String] as? String ?? ""), "pid": pid,
                        "bundle_id": app?.bundleIdentifier ?? ""], "window_id": String(number)]
    }
    return ["app": NSNull(), "window_id": NSNull()]
}

// MARK: - Диспетчер

/// Поднимается по SIGTERM; читается циклами ввода. Гонка безвредна: худшее — ещё одно событие.
nonisolated(unsafe) var interrupted = false
let input = Input()
let accessibility = Accessibility()
let windows = Windows(ax: accessibility)


func desktopBounds() -> CGRect {
    var ids = [CGDirectDisplayID](repeating: 0, count: 32)
    var count: UInt32 = 0
    let error = CGGetActiveDisplayList(UInt32(ids.count), &ids, &count)
    if error != .success || count == 0 {
        return CGDisplayBounds(CGMainDisplayID())
    }
    var combined = CGRect.null
    for id in ids.prefix(Int(count)) {
        combined = combined.union(CGDisplayBounds(id))
    }
    return combined.isNull ? CGDisplayBounds(CGMainDisplayID()) : combined
}

func capture(_ params: JSON) throws -> JSON {
    guard CGPreflightScreenCaptureAccess() else {
        throw failure("permission", "macOS не выдала разрешение «Запись экрана» для chatrepo-mcp")
    }
    var rect = desktopBounds()
    if let raw = params["region"] as? JSON {
        let x = try number(raw, "x")
        let y = try number(raw, "y")
        let width = try number(raw, "width")
        let height = try number(raw, "height")
        guard width > 0, height > 0 else { throw failure("bad_request", "region width/height должны быть положительными") }
        let requested = CGRect(x: x, y: y, width: width, height: height)
        guard rect.contains(requested) else { throw failure("bad_request", "region выходит за границы рабочего стола macOS") }
        rect = requested
    }
    guard let image = CGWindowListCreateImage(rect, [.optionOnScreenOnly], kCGNullWindowID, [.bestResolution]) else {
        throw failure("failed", "CoreGraphics не смог снять экран")
    }
    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil) else {
        throw failure("failed", "не удалось создать PNG encoder")
    }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination) else {
        throw failure("failed", "не удалось закодировать снимок в PNG")
    }
    return [
        "image_b64": (data as Data).base64EncodedString(),
        "mime_type": "image/png",
        "image_width": image.width,
        "image_height": image.height,
        "bounds": rectJSON(rect),
        "backend": "macos-native",
    ]
}

func hello() -> JSON {
    let trusted = accessibility.trusted()
    let screen = CGPreflightScreenCaptureAccess()
    var notes: [String] = []
    if !trusted { notes.append("Управление и элементы интерфейса требуют разрешения Accessibility для chatrepo (System Settings → Privacy & Security → Accessibility).") }
    if !screen { notes.append("Названия чужих окон и снимки экрана требуют разрешения «Запись экрана» для chatrepo.") }
    return ["protocol": 1, "platform": "darwin", "backend": "macos-native",
            "capabilities": ["input": true, "unicode_type": true, "cursor": true, "windows": true,
                             "window_control": true, "elements": true, "launch": true,
                             "ocr": true, "clipboard_files": true, "app_at": true, "background_input": true, "touch": true,
                             "select_text": true, "apps": true, "capture": true],
            "permissions": ["accessibility": trusted ? "granted" : "denied", "screen": screen ? "granted" : "denied"],
            "notes": notes]
}

func handle(_ method: String, _ params: JSON) throws -> Any {
    switch method {
    case "hello": return hello()
    case "capture": return try capture(params)
    case "input":
        try accessibility.requireTrusted()
        return try input.perform(params)
    case "cursor":
        let location = CGEvent(source: nil)?.location ?? .zero
        return ["x": Int(location.x.rounded()), "y": Int(location.y.rounded())]
    case "windows": return windows.list()
    case "window": return try windows.control(params)
    case "launch": return try launch(params)
    case "elements":
        var scope: (pid_t, CGWindowID)?
        if let raw = params["window_id"] as? String, !raw.isEmpty {
            guard let id = CGWindowID(raw) else { throw failure("bad_request", "window_id должен быть числом-строкой") }
            scope = (try windows.owner(id), id)
        }
        return try accessibility.elements(params, windowIds: scope, focusedPid: accessibility.focusedApplicationPid())
    case "element_action": return try accessibility.act(params)
    case "focused": return accessibility.focused()
    case "ocr": return try recognizeText(params)
    case "clipboard_files": return try clipboardFiles(params)
    case "app_at": return try appAt(params)
    case "apps": return runningApps()
    case "touch":
        try accessibility.requireTrusted()
        return try input.touch(params)
    case "background_input":
        try accessibility.requireTrusted()
        guard let id = CGWindowID(try string(params, "window_id")) else { throw failure("bad_request", "window_id должен быть числом-строкой") }
        return try input.background(pid: try windows.owner(id), windowId: id, params: params)
    default: throw failure("bad_request", "неизвестный метод «\(method)»")
    }
}

func respond(_ object: JSON) {
    var payload = object
    if !JSONSerialization.isValidJSONObject(payload) {
        payload = ["id": object["id"] ?? NSNull(), "error": ["code": "failed", "message": "ответ помощника не сериализуется"]]
    }
    guard var data = try? JSONSerialization.data(withJSONObject: payload, options: []) else { return }
    data.append(0x0A)
    FileHandle.standardOutput.write(data)
}

func dispatch(_ line: String) {
    guard let data = line.data(using: .utf8), let request = (try? JSONSerialization.jsonObject(with: data)) as? JSON else {
        respond(["id": NSNull(), "error": ["code": "bad_request", "message": "запрос не является JSON-объектом"]])
        return
    }
    let id = request["id"] ?? NSNull()
    do {
        let method = try string(request, "method")
        let params = (request["params"] as? JSON) ?? [:]
        respond(["id": id, "result": try handle(method, params)])
    } catch let error as DriverError {
        respond(["id": id, "error": ["code": error.code, "message": error.message]])
    } catch {
        respond(["id": id, "error": ["code": "failed", "message": String(describing: error)]])
    }
}

/// Самопроверка без ввода: события создаются и читаются обратно, но не постятся.
func selfTest() -> JSON {
    var units = Array("Привет, мир 👋".utf16)
    let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
    event?.keyboardSetUnicodeString(stringLength: units.count, unicodeString: &units)
    var read = [UniChar](repeating: 0, count: 64)
    var length = 0
    event?.keyboardGetUnicodeString(maxStringLength: 64, actualStringLength: &length, unicodeString: &read)
    let scroll = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: -70, wheel2: -50, wheel3: 0)
    return ["unicode": String(utf16CodeUnits: read, count: length),
            "axis1": scroll?.getIntegerValueField(.scrollWheelEventPointDeltaAxis1) ?? 0,
            "axis2": scroll?.getIntegerValueField(.scrollWheelEventPointDeltaAxis2) ?? 0,
            "roles": ["AXButton": normalizedRole("AXButton", nil), "AXCheckBox/AXSwitch": normalizedRole("AXCheckBox", "AXSwitch"),
                      "AXRadioButton/AXTabButton": normalizedRole("AXRadioButton", "AXTabButton")]]
}

if CommandLine.arguments.contains("--selftest") {
    respond(["id": 0, "result": selfTest()])
    exit(0)
}

setvbuf(stdout, nil, _IONBF, 0)
signal(SIGTERM, SIG_IGN)
signal(SIGPIPE, SIG_IGN)
// ⚠⚠ СИГНАЛ ОБРАБАТЫВАЕТСЯ НЕ НА ГЛАВНОЙ ОЧЕРЕДИ: она может быть занята вводом длинного текста, и
// обработчик на ней дождался бы конца ввода. Здесь поднимается флаг, ввод обрывается на ближайшем
// событии, затем главная очередь отпускает всё зажатое и процесс выходит.
let terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
terminate.setEventHandler {
    interrupted = true
    DispatchQueue.main.async {
        input.releaseAll()
        exit(0)
    }
    DispatchQueue.global().asyncAfter(deadline: .now() + 1) { exit(0) }
}
terminate.resume()

Thread.detachNewThread {
    while let line = readLine(strippingNewline: true) {
        if line.trimmingCharacters(in: .whitespaces).isEmpty { continue }
        DispatchQueue.main.sync { dispatch(line) }
    }
    DispatchQueue.main.sync { input.releaseAll() }
    exit(0)
}

RunLoop.main.run()
