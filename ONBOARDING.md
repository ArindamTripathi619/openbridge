# TeleWatch Onboarding Guide 🚀

Welcome to **TeleWatch** (also known as `telewatch`), your intelligent companion for remote process monitoring. This tool doesn't just "tail logs"—it understands them, tracks progress, and alerts you intelligently via Telegram.

---

## 📋 Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Configuration (The Wizard)](#3-configuration-the-wizard)
4. [Running TeleWatch](#4-running-telewatch)
5. [Operating Modes](#5-operating-modes)
6. [Command Reference](#6-command-reference)
7. [Advanced Features](#7-advanced-features)
8. [Troubleshooting](#8-troubleshooting)

---

## 🛠️ 1. Prerequisites

Before you start, ensure you have:

*   **Python**: Version 3.8 or higher (`python3 --version`).
*   **Telegram Bot Token**: Created via [@BotFather](https://t.me/botfather).
*   **Chat ID**: Your personal ID, retrieved via [@userinfobot](https://t.me/userinfobot) (usually a 9-10 digit number).
*   **LLM API Key** (Optional but Recommended):
    *   **Groq** (Fastest, Free Tier available) - *Recommended*
    *  - **Option 1-3**: Cloud providers (OpenAI, Anthropic, Groq). Requires an API Key.
- **Option 4**: Ollama (Local). 100% private, runs on your hardware.
- **Option 5**: Local API Rotator. Points to a LiteLLM proxy with 50+ models across 5 providers (Groq, Mistral, etc.).

---

## 📥 2. Installation

### Option A: Install from Source (Developers)

Best for active development or if you want the latest features.

```bash
# 1. Clone the repository
git clone https://github.com/DevCrewX/TelegramRemoteProgressBot.git
cd TelegramRemoteProgressBot

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install the package in editable mode
pip install -e .
```

### Option B: Using the Standalone Binary (Coming Soon)
Download the `telewatch` binary from the Releases page and make it executable:
```bash
chmod +x telewatch
./telewatch --help
```

---

## 🪄 3. Configuration (The Wizard)

The fastest way to configure TeleWatch is using the interactive setup wizard. It validates your keys in real-time.

```bash
telewatch setup
```

**What the Wizard does:**
1.  **Validates Telegram Credentials**: Sends a test message to your chat.
2.  **Checks LLM Access**: Verifies your API key works.
3.  **Creates Config**: Generates a secure `config.yaml` at `~/.telewatch/config.yaml`.
4.  **Sets Permissions**: Enforces `chmod 600` on the config file for security.

---

## 🏃 4. Running TeleWatch

### Basic Usage
Start monitoring interactively. You will see a live dashboard in your terminal.

```bash
telewatch start
```

### Daemon Mode (Background)
Run TeleWatch in the background. It will detach from your terminal and keep running even if you logout.

```bash
telewatch start --daemon
```
*   **Logs**: Output is redirected to `~/.telewatch/telewatch.log`
*   **Control**: Use `telewatch status` or `telewatch stop` to manage it.

### Turbo Mode (High Performance) ⚡
For ultra-lean deployments (e.g., Raspberry Pi, small VPS) where you only need basic monitoring without heavy analysis.

```bash
telewatch start --turbo
```
*   **Disables**: Log Profiler (No structural learning), Anomaly Detector (No spike/stall checks).
*   **Enables**: Basic pattern matching, keyword alerts, progress tracking.
*   **Resource Usage**: Minimal CPU/RAM footprint.

---

## 🕹️ 5. Operating Modes

| Mode | Description | Best For | Flags |
| :--- | :--- | :--- | :--- |
| **Interactive** | Live TUI dashboard with real-time logs. | Debugging, initial set up. | `start` |
| **Daemon** | Runs silently in background. | Long-running servers, training jobs. | `start --daemon` |
| **Turbo** | Minimal resource usage, core features only. | Edge devices, low-latency needs. | `start --turbo` |

---

## 📖 6. Command Reference

All commands are prefixed with `telewatch`.

| Command | Description | Key Options |
| :--- | :--- | :--- |
| `setup` | Run configuration wizard. | None |
| `start` | Start the monitoring process. | `--daemon` (`-d`), `--turbo`, `--config PATH` |
| `status` | Check if daemon is running. | None |
| `stop` | Stop the background daemon. | None |
| `notify` | Send a one-off test message. | `--message "Hello"` |

**Example:**
```bash
# Start in background with custom config
telewatch start --daemon --config ./my_config.yaml
```

---

## 💬 7. In-Chat Commands

Control TeleWatch directly from Telegram!

*   **`/status`**: "How is it going?"
    *   Returns: Progress bar, current stage, and a 1-sentence LLM summary.
*   **`/logs`**: "Show me details."
    *   Returns: The last 15 lines of logs.
*   **`/pause`**: "Hold on."
    *   Action: Pauses alerts and LLM analysis. Useful if you're manually fixing something and don't want spam.
*   **`/resume`**: "Continue."
    *   Action: Re-enables monitoring.

---

## 🧠 8. Advanced Features

### 🔍 Auto-Profiling & Drift Detection
TeleWatch learns the "shape" of your logs (JSON, CSV, Syslog).
*   **Profiling**: The first 50-100 lines build a baseline.
*   **Drift**: If logs suddenly change format (e.g., app crashes and prints stack traces instead of JSON), it alerts you.

### 📈 Anomaly Detection
*   **Spikes**: Alerts if log volume explodes (e.g., infinite loop).
*   **Stalls**: Alerts if logs stop coming for `stall_seconds` (default: 300s).
*   **Novelty**: If a **new type** of error appears that hasn't been seen before, it triggers an LLM analysis. Known errors are cached to save tokens.

### 📊 Multi-Stage Tracking
Track specific phases of your job in `config.yaml`:
```yaml
process:
  stages:
    - name: "Data Loading"
      start_pattern: "Loading dataset"
      weight: 1
    - name: "Training"
      start_pattern: "Epoch"
      weight: 8
```
TeleWatch will notify you as each stage begins!

---

## ❓ 8. Troubleshooting

**Q: "Config not found!"**
A: Run `telewatch setup` or specify a file with `--config path/to/config.yaml`.

**Q: Telegram commands aren't working.**
A: Ensure your bot has `Privacy Mode` **disabled** in @BotFather so it can read your messages, or strictly address it in a group. In 1-on-1 chats, it should work by default.

**Q: I'm getting too many alerts.**
A: Adjust `rate_limit_per_hour` in `config.yaml`. The default is 50.

**Q: It's using too many tokens.**
A: Enable optimizations in config or use specific patterns to skip LLM calls. Alternatively, used `--turbo` mode to disable heavy analysis.

---

*Found a bug? Open an issue on GitHub!*
