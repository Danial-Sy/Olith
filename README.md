# Aether

A local AI desktop app. Chat and a real coding agent, both running on your own
machine through Ollama.

Two tabs. **Chat** is a normal conversation, with a deeper reasoning mode and
vision if the model has it. **Agentic** is a tool-calling agent that reads and
writes files in a project you point it at, runs shell commands, keeps a task
list, and asks you when it needs a decision.

Nothing leaves your computer. Ollama downloads the weights and they are not part
of this repo. Web search is optional and off by default.

## Models

Any Ollama model works, and any GGUF on Hugging Face. The installer reads your
hardware and suggests models that fit. After that there is a model browser
inside Aether covering both libraries, searchable and filtered to what your
machine can hold. Picking a model shows every version of it with real download
sizes, and says what that model is missing. Ollama does the download either way,
so a Hugging Face model is probed, assigned and run by the same code as any
other.

Every model is assigned to Chat, Agentic, or both. Capabilities are read from
Ollama, so a model that cannot call tools is never offered for agentic work.

The defaults are Qwen3.8 27B and Qwen3-Coder 30B. That is what Aether was built
and tuned on, and it needs a 24 GB card. Smaller setups still work, and every
model gets the same tools. Giving smaller models a smaller tool surface was
tried, measured against five models, and it made all of them worse.

## What it can do

- Read, write and edit files in a registered project, with anchored edits, a
  syntax check before every write, and automatic backups
- Read one function or class by name, or a file's outline, instead of guessing
  line numbers
- Run shell commands, auto-running safe work and asking about anything that
  could change the machine
- Keep a visible plan and work through it without supervision
- Ask you a question when it needs one answered
- Hand survey work on large projects to a read-only sub-agent
- Handle images and attachments
- Drive a browser and the desktop in computer use mode
- Compact its own context so long runs do not fall over

[ARCHITECTURE.md](ARCHITECTURE.md) covers how all of that works.

### Computer use

This one is still rough. It works best with a model trained to point at things
on a screen, like the qwen3-vl family, and Aether picks one of those when it has
one. A general vision model can read the screen fine and still click in the
wrong place, so expect a lot of misses if that is what you give it. Inside a web
page the browser tool skips pixels entirely and clicks real elements, which is
why it is the more reliable of the two.

## Hardware

Aether runs on what you have. The installer reads your GPU, memory and free
disk, then suggests models sized for it:

| Usable memory | Suggested |
|---|---|
| under 6 GB | one 4B model doing both jobs |
| 6 to 12 GB | 8B for chat, 7B coder for agentic |
| 12 to 20 GB | 14B pair |
| 20 to 64 GB | Qwen3.8 27B and Qwen3-Coder 30B, the tuned default |
| 64 GB and up | adds a 70B for chat |
| no GPU | 8B and 7B, sized for speed |

VRAM is what counts when you have a GPU. Without one the models run on the CPU
and the suggestion stays well below what would technically fit, because 128 GB
of RAM will hold a 70B and it will answer at about a word a second. Aether also
works out the largest context window each model can have on your machine and
offers exactly those.

A small model (`qwen2.5:0.5b`, about 400 MB) comes with every choice. It only
writes chat titles, on the CPU, so naming a conversation never interrupts the
model you are talking to.

## Windows

1. Download and extract the latest release.
2. Double-click **`Install-Aether.bat`**.
3. Choose Default, Recommended for your PC, or type your own model tag.
4. Launch from the new Desktop or Start Menu shortcut.

The installer is a wizard. It installs Python 3.11+ and Ollama if they are
missing, checks your hardware, pulls only the models you do not already have,
builds an isolated `.venv`, and makes the shortcuts. Running it again is safe.

For unattended installs:

```bat
Install-Aether.bat -Models Chat -NonInteractive
Install-Aether.bat -Models Full -NonInteractive
Install-Aether.bat -SkipModels -NonInteractive
```

To move to a newer version later, double-click **`Update-Aether.bat`**.

## Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-gi \
  gir1.2-gtk-3.0 gir1.2-webkit2-4.1
curl -fsSL https://ollama.com/install.sh | sh

chmod +x setup.sh aether.sh install-desktop.sh update.sh
./setup.sh
./aether.sh
```

If `gir1.2-webkit2-4.1` is not available, install `gir1.2-webkit2-4.0`.

`setup.sh` asks which models you want: Default, Recommended for your machine, or
a tag you type. Use `--yes` to take the recommendation without being asked,
`--tags qwen3:8b` to name models directly, or `--skip-models` if Ollama already
has the weights.

`setup.bat` and `setup.py` are the direct entry points if Python and Ollama are
already set up and you do not want the wizard.

## Updating

```bash
./update.sh          # Ubuntu
```

```bat
Update-Aether.bat
```

`data/` and `.venv/` are never touched, so chats, memory, projects, settings and
uploads survive an update.

## Your data

Chats, memory, projects, settings and uploads live under `data/`, which is
gitignored so a commit or a release cannot publish your conversations.

The agent can only touch registered projects, Aether's own folder, Documents,
Desktop, and files you upload. Shell commands are classified before they run.
Reads, builds, tests and installs go through. Anything that could destroy data,
escalate privilege or change the system asks first. You can widen or narrow that
in Settings.

## Configuration

Defaults assume a standard local Ollama install. For custom layouts:

| Variable | Purpose |
|---|---|
| `AETHER_OLLAMA` | Path to `ollama` or `ollama.exe` |
| `AETHER_OLLAMA_MODELS` | Custom Ollama model directory |
| `AETHER_AI_ROOT` | A workspace containing a sibling `ollama` directory |
| `OLLAMA_HOST` | Ollama API address |
| `AETHER_UPDATE_REPO` | GitHub repository the updater tracks |
| `GITHUB_TOKEN` | Raises the updater's GitHub rate limit |

Model metadata, context sizes and system prompts live in `config.py`. Per-user
settings are written to `data/settings/settings.json` at runtime.

## Development

```bash
python3 setup.py --skip-models
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 7878
```

On Windows use `py -3` and `.venv\Scripts\python.exe`.

Tests:

```bash
.venv/bin/python -m unittest discover -s tests
```

The suite skips the server tests when FastAPI is not importable, so run it from
the venv rather than a bare `python3`. Quick static checks:

```bash
python3 -m compileall -q -x '(^|/)(data|\.venv)(/|$)' .
node --check static/app.js
```

## Releases

CI runs the Python and JavaScript checks on every push, and a Windows runner
parses the PowerShell wizard and runs its model-detection tests. Pushing a
version tag builds a source ZIP and publishes it as a GitHub release:

```bash
git tag v1.5.0
git push origin v1.5.0
```

The archive leaves out `.venv`, model weights, and everything under `data/`.
That release is what `update.py` compares against, so bump `VERSION` in
`config.py` and the version in `aether.json` in the same commit as the tag. The
updater reads `config.py` to decide whether a copy is current.

## License

Aether is licensed under the GNU Affero General Public License v3.0. The full
text is in [LICENSE](LICENSE).

Copyright (C) 2026 Danial Syed

In plain terms: use it, modify it and share it freely, including running it
yourself for whatever you like. If you distribute a modified version, or run one
as a service other people can use over a network, you have to publish your
source under the same license. That summary is not a substitute for the license
text.
