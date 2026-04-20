# Telegram OpenCode Bridge

Minimal Telegram application that forwards a chat message to `opencode`, waits for the result, and sends the final output back to the same chat.

The recommended workflow is the built-in setup wizard. It collects the bot token, model, working dir, timeout, allowed chat ids, log level, and optional decorated-output settings, then writes the config file for you.

## Quick Start

1. Run `telewatch setup`.
2. Follow the wizard and let it write `~/.config/telewatch/bridge.env` for you.
3. Choose whether to start the app immediately.
4. Use `telewatch start` for the normal background run.
5. Use `telewatch start --foreground --debug` when you want to watch the bridge interactively.

## Install

```bash
cd /home/DevCrewX/Projects/TelegramRemoteProgressBot
source .venv/bin/activate
./.venv/bin/python -m pip install -e .
```

## Setup Wizard

The wizard is the normal way to configure the app. It is the fastest route for first run and the easiest way to change token, model, or chat restrictions later.

The wizard writes a config file at `~/.config/telewatch/bridge.env`. You do not need to edit it by hand unless you want to.

The wizard configures:

- Telegram bot token
- OpenCode model
- OpenCode working directory
- timeout seconds
- allowed chat ids
- log level
- optional decorated-output settings

If you want to bootstrap from the shell for reference, [config/opencode-bridge.env.example](config/opencode-bridge.env.example) shows the same keys the wizard manages.

If you want systemd supervision, use [config/telewatch.service.example](config/telewatch.service.example) as the starting point, or let `telewatch install-systemd` create the user unit for you.

Optional decorated output post-processor settings:

```bash
export TELEWATCH_DECORATOR_ENABLED="1"
export TELEWATCH_DECORATOR_API_KEY="sk-..."
export TELEWATCH_DECORATOR_MODEL="your-free-model-name"
export TELEWATCH_DECORATOR_BASE_URL="https://your-provider.example/v1"
export TELEWATCH_DECORATOR_TIMEOUT_SECONDS="30"
```

## Run

```bash
telewatch setup
telewatch start
```

The app runs in the background by default. For interactive debugging:

```bash
telewatch start --foreground --debug
```

## Systemd

Install the user systemd service after setup:

```bash
telewatch install-systemd --start
```

Write the unit without enabling it:

```bash
telewatch install-systemd --no-enable
```

Remove the unit later with:

```bash
telewatch uninstall-systemd
```

Logs are written to `~/.config/telewatch/telewatch.log`.
Sensitive Telegram API tokens are redacted from those logs.

Status commands:

```bash
telewatch status
telewatch stop
```

## Bot Commands

Telegram commands available from the bot:

```text
/start
/help
/health
/stats
```

`/health` reports whether the bridge is healthy and configured, and `/stats` reports runtime counters for received prompts, completed jobs, failures, and quota fallbacks.

## Test

```bash
./.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

## Notes

- The bot is intentionally small and single-purpose.
- This branch exposes the application and the bridge command.
- If the configured model hits quota or rate limits, the bridge automatically retries with `opencode/minimax-m2.5-free` and then `opencode/nemotron-3-super-free`.
- If the decorator is enabled and healthy, replies are reformatted into Telegram-friendly HTML sections.
- `telewatch install-systemd` writes a user unit to `~/.config/systemd/user/telewatch.service` and enables it unless you pass `--no-enable`.
- `telewatch uninstall-systemd` removes that user unit and reloads the user systemd daemon.
