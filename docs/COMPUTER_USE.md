# Computer Use: desktop vision and control

ChatRepo MCP can expose the connected desktop to ChatGPT as an optional,
cross-platform Computer Use surface. The same feature is used by both public
implementations:

```text
ChatGPT
   |
   | MCP computer_* tools
   v
Python MCP  ----\
                 > chatrepo-computer-host ---- platform driver
Go MCP      ----/                           |
                                              +-- Linux X11 / AT-SPI
                                              +-- Linux Wayland portal / PipeWire / AT-SPI
                                              +-- Windows UI Automation / Win32
                                              +-- macOS AX / CoreGraphics / Vision
```

The MCP implementations do **not** contain two independent desktop-control
engines. They are thin clients of the same long-lived
`chatrepo-computer-host`, so snapshot semantics, coordinate mapping, stale
protection, action verification, limits, and error kinds stay identical.

## Enable it

Computer Use is off by default.

Read-only desktop vision:

```env
COMPUTER_USE_ENABLED=true
COMPUTER_CONTROL_ENABLED=false
```

Full desktop vision + input requires trusted-machine mode:

```env
ACCESS_MODE=full
COMPUTER_USE_ENABLED=true
COMPUTER_CONTROL_ENABLED=true
```

`COMPUTER_CONTROL_ENABLED=true` is rejected unless both
`COMPUTER_USE_ENABLED=true` and `ACCESS_MODE=full` are set. This is separate
from ChatGPT's app permission selector.

The remaining controls are:

| Variable | Default | Meaning |
|---|---:|---|
| `COMPUTER_SNAPSHOT_TTL_SECONDS` | 30 | Coordinate snapshot lifetime |
| `COMPUTER_ACTION_TIMEOUT_MS` | 30000 | Per-driver action timeout |
| `COMPUTER_IDLE_TIMEOUT_SECONDS` | 300 | Release desktop drivers/PipeWire/in-RAM frames after idle |
| `COMPUTER_MAX_SEQUENCE_STEPS` | 20 | Maximum `computer_sequence` length |
| `COMPUTER_CAPTURE_MAX_EDGE` | 1568 | Maximum long edge returned to the model |

`COMPUTER_CAPTURE_MAX_EDGE` accepts 256..8192. The host keeps the native
desktop geometry separately, so downscaling a large or multi-monitor screenshot
does not change click mapping. Use `computer_zoom` when a small region needs
more pixels.

## Tools

The canonical catalog contains seventeen Computer Use tools.

Read-only "eyes":

- `computer_status` — platform, backend, permissions, capture/input capability.
- `computer_observe` — fresh screenshot plus windows, focus, cursor and
  accessibility scene.
- `computer_share_snapshot` — expose a fresh RAM-only snapshot back to the chat as both `ImageContent` and a short-lived PNG `ResourceLink`.
- `computer_zoom` — recapture a region from an existing snapshot.
- `computer_windows` — native application windows.
- `computer_elements` — AX/UIA/AT-SPI accessibility elements.
- `computer_wait` — wait for stable UI, window, element, text, or a short time.

Full-mode "hands":

- `computer_element` — semantic element press/focus/set/read actions.
- `computer_click` — element click or exact screenshot-coordinate click.
- `computer_move` — move/hover the real mouse without clicking.
- `computer_type` — Unicode text, optional clear and submit.
- `computer_key` — key presses and shortcuts.
- `computer_scroll` — portable line scrolling or raw deltas.
- `computer_drag` — pointer drag with intermediate motion.
- `computer_window` — focus/minimize/maximize/restore/close/set bounds.
- `computer_launch` — launch a native application without a shell.
- `computer_sequence` — short dependent UI sequences, stopping on first error.

With Computer Use disabled, the runtime catalog remains the existing baseline:
94 tools in safe mode and 100 tools in full POSIX mode. Enabling only the eyes
adds seven tools. Enabling full control adds all seventeen, for 117 tools on a
full POSIX deployment. Windows omits the six POSIX PTY tools, so its maximum is
111.

## Screenshots are native MCP images

`computer_observe`, `computer_zoom`, and control calls that produce a fresh
scene return the screenshot as MCP `ImageContent`. The PNG is **not** copied
into `structuredContent`.

The structured result contains values such as:

```json
{
  "ok": true,
  "snapshot_id": "...",
  "mime_type": "image/png",
  "image_width": 1366,
  "image_height": 768,
  "bounds": {"x": 0, "y": 0, "width": 1366, "height": 768},
  "scene": {
    "cursor": {"x": 500, "y": 300},
    "active": {"window": {}, "focused": {}},
    "windows": [],
    "accessibility": {}
  }
}
```

The image pixels shown to the model and the `snapshot_id` belong to the same
capture. Coordinate actions use those exact image pixels.

`computer_share_snapshot(snapshot_id)` is the human-facing path: it returns the same PNG as `ImageContent` and also exposes a five-minute `chatrepo-screen://` `ResourceLink`. The bytes remain RAM-only on the connected machine; at most four shared frames are retained.

## Snapshot and stale-state rules

Pixel input never accepts an unbound coordinate. It requires a current
`snapshot_id`. The host stores the native desktop bounds that correspond to
that image and maps model pixels back to native coordinates.

A snapshot is rejected with `stale_snapshot` when it expires. On platforms
where the active-window identity is available, foreground input is also rejected
when the active window changed after the screenshot. The model must observe
again instead of guessing.

Accessibility element ids are independently ephemeral. AX/UIA/AT-SPI drivers
return a stale/not-found error when the application rebuilt the referenced UI.

After a control action the host captures a new scene. For coordinate actions it
also compares the before/after image and reports verification such as
`confirmed_changed` or `uncertain_unchanged`. Delivery of an OS input event
alone is not treated as proof that the application did what the model intended.

## Multiple monitors

### Linux X11

The root window is captured as one virtual desktop, including all X11 monitors.
Negative/native monitor geometry is normalized only for the model image;
coordinate mapping is preserved.

### Windows

The driver uses the DPI-aware Windows virtual screen
(`SM_XVIRTUALSCREEN/...CYVIRTUALSCREEN`) and captures it as one desktop.

### macOS

The driver unions all active `CGDisplay` bounds and captures that desktop.
Retina pixels and Quartz logical coordinates are mapped by the common host.

### Linux Wayland

The driver uses the official XDG RemoteDesktop + ScreenCast portal. It requests
multiple monitor sources and composes every monitor the user approves into one
virtual screenshot. Pointer/touch coordinates are routed back to the matching
portal stream.

On the first permission prompt, select every monitor you want ChatGPT to see.
A portal `restore_token` is stored in the private runtime cache so a supported
desktop can restore the approved selection until the user revokes it.

## Platform runtime

The platform drivers are embedded inside `chatrepo-computer-host`. On first
use the host extracts **only the current OS assets** into a content-hashed user
cache:

- Linux: `~/.cache/chatrepo-mcp/computer/<hash>/`
- macOS: the platform user cache directory under `chatrepo-mcp/computer/<hash>/`
- Windows: the platform user cache directory under `chatrepo-mcp/computer/<hash>/`

The directory and extracted helpers are private to the current user. A new
driver payload produces a new hash directory rather than silently mutating an
old runtime.

The Go build always includes the companion:

```bash
make build
# bin/chatrepo-mcp
# bin/chatrepo-computer-host
```

For the Python MCP implementation built from this source tree:

```bash
make computer-host
python -m chatrepo_mcp
```

Both processes then use that same companion. `COMPUTER_HOST_PATH` is an
advanced override for packaged/custom deployments.

Release archives contain `chatrepo-computer-host` beside the MCP binary.
macOS release archives also contain a precompiled native
`chatrepo-computer-driver`; a source checkout can fall back to `swiftc`
when that prebuilt driver is absent.

## Linux

### X11

The bundled Linux driver uses Xlib/XTest for capture/input and AT-SPI for
semantic UI elements. Typical distro requirements are Python 3 with
`python3-gi`, AT-SPI GIR bindings, X11 and Xtst libraries. OCR is optional and
uses Tesseract when installed.

The MCP service must run in the logged-in desktop user's graphical environment
(`DISPLAY`, `XAUTHORITY`, session D-Bus). `computer_status` reports a
clear non-input backend when these are missing.

### Wayland

Wayland does not permit arbitrary global screenshots/input through X11-style
APIs. ChatRepo uses XDG Desktop Portal, PipeWire and GStreamer for capture and
RemoteDesktop portal notifications for pointer/keyboard/touch. AT-SPI remains
the semantic accessibility backend.

The compositor's sharing/control indicator is authoritative while a portal
session is active.

## Windows

The long-lived Windows helper is PowerShell + one in-process C# driver. It uses:

- Per-Monitor-V2 DPI awareness;
- `System.Drawing.CopyFromScreen` for the virtual desktop image;
- UI Automation for elements and semantic actions;
- `SendInput` / Unicode input for foreground keyboard and pointer actions;
- Win32 window management.

UIPI/UAC and lock-screen restrictions are reported as `blocked` or
`permission`; the driver does not claim success when Windows drops input.

## macOS

The native Swift helper uses AX Accessibility, CoreGraphics input/capture,
AppKit and Vision OCR. macOS requires user approval for Accessibility and Screen
Recording. `computer_status` reports the missing permission instead of trying
to click through system privacy dialogs.

## Sensitive UI

Password/secure accessibility elements are marked `secure`. Their value,
selection, and caret text are not returned by semantic read operations.

Raw screenshots can naturally contain whatever is visible on the desktop.
Computer screenshots are **RAM-only**: they are returned to the active MCP
request and are never written to `COMMAND_JOBS_DIR`, the audit log, or the
bounded-output artifact store. After `COMPUTER_IDLE_TIMEOUT_SECONDS` without a
`computer_*` call, the common host releases platform drivers, PipeWire sessions,
and all retained snapshot bytes.

## Cancellation and failure behavior

The MCP client, common host, and platform driver are all long-lived processes.
Requests are serialized. On cancellation or timeout the affected child protocol
is invalidated/terminated rather than reusing a stream that may still emit a
late reply. Driver shutdown releases held keys, mouse buttons and touch points.

The public error surface includes typed errors such as:

- `bad_request`
- `unsupported`
- `permission`
- `not_found`
- `stale` / `stale_snapshot`
- `blocked`
- `secure_field`
- `timeout`
- `failed`

## Optional visible control indicator

A custom ChatRepo overlay is intentionally **not required by the control
protocol**. Windows/macOS/X11 can support one later, but Wayland compositors
control global overlays and already provide their own portal sharing indicator.
Keeping the overlay out of the correctness path means desktop control still
works on every supported backend even when a custom indicator cannot be drawn.
