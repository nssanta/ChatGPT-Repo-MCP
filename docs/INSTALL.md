# Installation

The server preserves inherited PATH and probes existing standard toolchain directories, including `/usr/local/go/bin`. For service-only toolchains, set `MCP_EXTRA_PATH`; doctor, diagnostics, GitHub tools, commands, and PTY all use that same effective PATH. A target repository's `.venv` is never discovered implicitly. Persistent PTY is enabled by default but registered only with `ACCESS_MODE=full` on Linux/macOS; set `ENABLE_PTY=false` to disable it explicitly. The Go Windows build omits those six tools.

ChatRepo MCP ships two public MCP implementations from the same repository. Install one of them; do not run both on the same host and port. Optional Computer Use is intentionally shared: both implementations call the same `chatrepo-computer-host` companion so desktop behavior cannot drift.

## Shared runtime dependencies

Both implementations expect `git`, `ripgrep` (`rg`), and `bash`. GitHub tools
additionally need an authenticated GitHub CLI (`gh auth login`). Symbol tools
use Universal Ctags when available and fall back to a regex index otherwise.

On native Windows, install [Git for Windows](https://gitforwindows.org/) and
make `bash.exe` available on `PATH`. The Go server deliberately keeps the same
bash command contract on every operating system instead of silently translating
commands to PowerShell.

## Python 3.11+

```bash
git clone https://github.com/nssanta/ChatGPT-Repo-MCP.git chatrepo-mcp
cd chatrepo-mcp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ./python
# Needed only when COMPUTER_USE_ENABLED=true in a source checkout:
make computer-host
cp .env.example .env
python -m chatrepo_mcp
```

## Go release binary

Download the archive for your OS and architecture from GitHub Releases, then
verify the adjacent checksum before extracting it:

```bash
sha256sum -c chatrepo-mcp_0.3.0_linux_amd64.tar.gz.sha256
tar -xzf chatrepo-mcp_0.3.0_linux_amd64.tar.gz
cd chatrepo-mcp_0.3.0_linux_amd64
cp .env.example .env
./chatrepo-mcp
```

Release archives include the matching `chatrepo-computer-host` companion beside the MCP binary. Windows archives use ZIP and `.exe` files. macOS archives also include the native `chatrepo-computer-driver`; users may need to approve unsigned binaries and grant Accessibility / Screen Recording when Computer Use is enabled.

## Build Go from source

Go 1.25 or 1.26 is supported:

```bash
git clone https://github.com/nssanta/ChatGPT-Repo-MCP.git chatrepo-mcp
cd chatrepo-mcp
make build
cp .env.example .env
./bin/chatrepo-mcp
```

## Configuration and verification

Set at least `PROJECT_ROOT` in the shared `.env`. The default endpoint is
`http://127.0.0.1:8000/mcp` for both implementations. Optional binary file-transfer ceilings are `FILE_TRANSFER_IMPORT_MAX_BYTES` (512 MiB by default) and `FILE_TRANSFER_EXPORT_MAX_BYTES` (100 MiB by default); see [File transfer](FILE_TRANSFER.md). Computer Use is disabled by default; see [Computer Use](COMPUTER_USE.md) before enabling `COMPUTER_USE_ENABLED` / `COMPUTER_CONTROL_ENABLED`.

```bash
./scripts/smoke_test.sh
python scripts/check_tools.py http://127.0.0.1:8000/mcp
```

See [Connecting to ChatGPT](CONNECT_CHATGPT.md) and the deployment runbooks for
remote access.
