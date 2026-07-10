# Installation

The server preserves inherited PATH and probes existing standard toolchain directories, including `/usr/local/go/bin`. For service-only toolchains, set `MCP_EXTRA_PATH`. Persistent PTY is supported on Linux/macOS and requires `ACCESS_MODE=full` plus `ENABLE_PTY=true`; the Go Windows build omits those six tools.

ChatRepo MCP ships two independent implementations from the same repository.
Install one of them; do not run both on the same host and port.

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

Windows archives use ZIP and include `chatrepo-mcp.exe`. macOS users may need
to approve an unsigned binary in System Settings when installing outside a
package manager.

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
`http://127.0.0.1:8000/mcp` for both implementations.

```bash
./scripts/smoke_test.sh
python scripts/check_tools.py http://127.0.0.1:8000/mcp
```

See [Connecting to ChatGPT](CONNECT_CHATGPT.md) and the deployment runbooks for
remote access.
