# ms-teams-interaction

Automate **Microsoft Teams (M365)** in the **browser** only: no Azure app registration, no Microsoft Graph. You sign in once (including MFA); a persistent profile keeps the session.

**Scope (v0.1):** standard **team channels** only, **top-level** posts, **plain text**. You identify channels by **full Teams web URL** (from *Copy link to channel*).

---

## Requirements

- **Python 3.10+** (`python3 --version` or `python --version`)
- **Network access** for `pip install` and for Playwright to download browsers (first time only)
- A **desktop** environment (the automation runs a real browser window; default is not headless)

---

## Installation

These steps assume the project lives at `~/projects/ms-teams-interaction`. Adjust the path if you cloned or copied it elsewhere.

### 1. Go to the project directory

```bash
cd ~/projects/ms-teams-interaction
```

### 2. Create and activate a virtual environment

**Linux / macOS:**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (cmd):**

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

(Optional but recommended after activating the venv:)

```bash
python -m pip install --upgrade pip
```

### 3. Install this package

**Library + CLI** (Typer entry point `teams-interaction`):

```bash
pip install -e ".[cli]"
```

**Library only** (no console script):

```bash
pip install -e .
```

Editable install (`-e`) is recommended while developing or updating selectors; use `pip install .` or `pip install ".[cli]"` for a normal install from a checkout.

### 4. Install Playwright browsers

Playwright needs at least one browser binary. The code uses the Chromium engine (Edge is Chromium-based).

**Minimum (bundled Chromium — good for trying the project):**

```bash
playwright install chromium
```

**Optional — Microsoft Edge channels** (if your org standardizes on Edge, use the channel Playwright supports on your OS):

```bash
# Often works on Windows / macOS with stable Edge:
playwright install msedge

# Linux: stable "msedge" may be unavailable; beta/dev often works:
playwright install msedge-beta
# or: playwright install msedge-dev
```

If `playwright install` fails (proxy, offline), fix network or use [Playwright’s install docs](https://playwright.dev/python/docs/browsers) for air-gapped setups.

### 5. Point the library at Edge (optional)

If you installed an Edge channel via Playwright, set the channel name so launches use it:

**Linux (example — beta):**

```bash
export TEAMS_BROWSER_CHANNEL=msedge-beta
```

**Windows / macOS (example — stable Edge):**

```bash
export TEAMS_BROWSER_CHANNEL=msedge
```

On Windows PowerShell, use `$env:TEAMS_BROWSER_CHANNEL = "msedge"` instead of `export`.

If Playwright cannot find the channel, install the matching browser (`playwright install …` as above) or set a full path:

```bash
export TEAMS_BROWSER_EXECUTABLE=/usr/bin/microsoft-edge-beta
```

(Use the real path to your Edge/Chromium binary.)

### 6. Environment variables (reference)

| Variable | Purpose | Default |
|----------|---------|---------|
| `TEAMS_BROWSER_CHANNEL` | Playwright browser channel: e.g. `msedge`, `msedge-beta`, `msedge-dev`. Omit or use `chromium` if you rely on `playwright install chromium` only. | `msedge` (falls back to bundled Chromium if launch fails) |
| `TEAMS_BROWSER_EXECUTABLE` | Full path to the browser binary; overrides channel when set. | unset |
| `TEAMS_PROFILE_DIR` | Persistent profile directory (cookies, login session). | `~/.cache/ms-teams-interaction/browser-profile` |

### 7. Verify the install

With the venv **activated**:

```bash
python -c "from teams_interaction import TeamsClient; print('OK:', TeamsClient)"
```

If you installed `[cli]`:

```bash
teams-interaction --help
```

You should see subcommands `open`, `send`, and `watch`.

---

## Edge vs Chromium (quick guide)

| OS | Typical setup |
|----|----------------|
| **Windows / macOS** | Install Edge for desktop; `playwright install msedge`; `TEAMS_BROWSER_CHANNEL=msedge`. |
| **Linux** | Stable `msedge` may be missing — use `playwright install msedge-beta` (or `msedge-dev`) and `TEAMS_BROWSER_CHANNEL=msedge-beta`, **or** use `playwright install chromium` only and unset/`chromium` channel behavior (library falls back if `msedge` launch fails). |

---

## Python API

```python
import asyncio
from teams_interaction import TeamsClient, ChannelMessage

async def main():
    client = TeamsClient()
    await client.start()

    async def on_message(msg: ChannelMessage):
        print(msg.author or "?", ":", msg.text[:200])

    client.watch_channel(
        "https://teams.microsoft.com/l/channel/...",
        on_message,
        poll_interval=2.0,
    )
    await client.send_message(
        "https://teams.microsoft.com/l/channel/...",
        "Hello from the SDK",
    )
    try:
        await asyncio.Future()
    finally:
        await client.close()

asyncio.run(main())
```

Selectors on `teams.microsoft.com` change; if watching breaks after a Teams update, adjust `teams_interaction/selectors.py` or open an issue with a snapshot.

---

## CLI

With the venv activated and `pip install -e ".[cli]"` done:

```bash
teams-interaction open --url 'https://teams.microsoft.com/l/channel/...'
teams-interaction send --url '...' --text 'Hello'
teams-interaction watch --url '...' --interval 2
```

Or run the module:

```bash
python -m teams_interaction.cli --help
```

---

## Troubleshooting

- **`playwright: command not found`** — Use `python -m playwright install chromium` from the same venv where you installed the package.
- **`BrowserType.launch_persistent_context: Executable doesn't exist`** — Run the matching `playwright install …` for your `TEAMS_BROWSER_CHANNEL`, or set `TEAMS_BROWSER_EXECUTABLE` to a valid binary.
- **Proxy / corporate TLS** — Configure `pip` and system trust as for any Python project; Playwright browser downloads may need `HTTPS_PROXY` or an internal mirror per Playwright docs.
- **`ModuleNotFoundError: teams_interaction`** — Ensure the venv is activated and `pip install -e .` succeeded from the project root.

---

## Warnings

- UI automation is **fragile** and **not officially supported** by Microsoft.
- Respect your org’s policies; this library does not bypass Conditional Access—it runs **in your browser** as you.
- Aggressive polling (e.g. every second) may be heavy; default is 2 seconds.

---

## License

MIT
