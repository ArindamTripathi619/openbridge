from rich.console import Console
from rich.prompt import Prompt, Confirm, IntPrompt
from rich.panel import Panel
from rich.text import Text
from pathlib import Path
import yaml
import sys
import os

from ..config import Config
from ..notifiers import TelegramNotifier
from ..analyzers import create_llm_client

console = Console()

class SetupWizard:
    """Interactive setup wizard for TeleWatch."""
    
    def __init__(self):
        self.config_data = {
            "telegram": {},
            "llm": {},
            "notification": {
                "debounce_seconds": 300,
                "rate_limit_per_hour": 10,
                "severity_levels": ["critical", "warning", "info"]
            },
            "monitors": [],
            "process": {},
            "progress_tracking": {},
            "interactive": {}
        }

    def run(self):
        """Run the setup wizard."""
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]TeleWatch Setup Wizard[/bold cyan]\n"
            "[dim]Configure your remote progress monitor[/dim]",
            border_style="cyan"
        ))
        
        try:
            self._step_1_telegram()
            self._step_2_llm()
            self._step_3_process_info()
            self._step_4_monitors()
            self._step_5_save()
        except KeyboardInterrupt:
            console.print("\n[bold red]Setup cancelled.[/bold red]")
            sys.exit(1)

    def _step_1_telegram(self):
        console.print("\n[bold]1. Telegram Configuration[/bold]", style="cyan")
        console.print("[dim]You need a bot token from @BotFather and your Chat ID.[/dim]")
        
        while True:
            token = Prompt.ask("🤖 Bot Token")
            chat_id = Prompt.ask("🆔 Chat ID")
            
            with console.status("[bold green]Validating credentials...[/bold green]"):
                try:
                    notifier = TelegramNotifier(token, chat_id)
                    if notifier.send_test_message():
                        console.print("✅ [green]Success! Test message sent.[/green]")
                        self.config_data["telegram"] = {"bot_token": token, "chat_id": chat_id}
                        break
                    else:
                        console.print("❌ [red]Failed to verify credentials.[/red]")
                except Exception as e:
                    console.print(f"❌ [red]Error: {e}[/red]")
            
            if not Confirm.ask("Try again?", default=True):
                sys.exit(1)

    def _step_2_llm(self):
        console.print("\n[bold]2. AI Analyzer Configuration[/bold]", style="cyan")
        
        provider = Prompt.ask(
            "Select AI Provider",
            choices=["openai", "anthropic", "groq", "ollama"],
            default="openai"
        )
        
        # Defaults
        models = {
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-haiku-20241022",
            "groq": "llama-3.3-70b-versatile",
            "ollama": "llama3.2"
        }
        
        config = {"provider": provider, "model": models[provider]}
        
        if provider == "ollama":
            config["base_url"] = Prompt.ask("Ollama URL", default="http://localhost:11434")
            console.print("✅ [green]Using local Ollama.[/green]")
        else:
            if provider == "groq":
                console.print("[dim]Get free key: https://console.groq.com/keys[/dim]")
            
            while True:
                key = Prompt.ask(f"🔑 {provider.capitalize()} API Key", password=True)
                config["api_key"] = key
                
                # Validation
                if Confirm.ask("Validate API Key?", default=True):
                    with console.status("[bold green]Testing API key...[/bold green]"):
                        try:
                            # Mock config for factory
                            test_config = config.copy()
                            client = create_llm_client(test_config)
                            client.analyze("Test")
                            console.print("✅ [green]API Key valid.[/green]")
                            break # Success, exit loop
                        except Exception as e:
                            console.print(f"❌ [red]Validation failed: {e}[/red]")
                            if not Confirm.ask("Retry entering key?", default=True):
                                # User chose not to retry, break regardless of validity
                                console.print("[yellow]Proceeding with unvalidated key.[/yellow]")
                                break
                else:
                    break
                    
        self.config_data["llm"] = config

    def _step_3_process_info(self):
        console.print("\n[bold]3. Process Details[/bold]", style="cyan")
        
        name = Prompt.ask("Process Name", default="My Job")
        desc = Prompt.ask("Description", default="Long running task")
        
        keywords = Prompt.ask("Keywords to track (comma separated)", default="error,completed,progress")
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        
        self.config_data["process"] = {
            "name": name,
            "description": desc,
            "keywords": kw_list
        }
        
        # Enable tracking features
        self.config_data["progress_tracking"] = {
            "enabled": True,
            "update_interval_percent": 10,
            "min_update_interval_seconds": 300
        }
        self.config_data["interactive"] = {
            "listen_for_messages": True,
            "status_on_any_message": True
        }

    def _step_4_monitors(self):
        console.print("\n[bold]4. Monitoring Targets[/bold]", style="cyan")
        
        # File Monitor
        if Confirm.ask("Monitor a log file?", default=True):
            path = Prompt.ask("Log File Path")
            path = os.path.expanduser(path)
            
            self.config_data["monitors"].append({
                "type": "file",
                "name": "Log Monitor",
                "path": path,
                "keywords": ["ERROR", "Exception", "Traceback"]
            })
            console.print(f"✅ [green]Added file monitor: {path}[/green]")

        # PID Monitor
        if Confirm.ask("Monitor a specific PID?", default=False):
            pid = IntPrompt.ask("Process PID")
            self.config_data["monitors"].append({
                "type": "pid",
                "name": f"PID {pid}",
                "pid": pid,
                "check_interval": 30
            })
            console.print(f"✅ [green]Added PID monitor: {pid}[/green]")

    def _step_5_save(self):
        console.print("\n[bold]5. Finalize[/bold]", style="cyan")
        
        config_dir = Path.home() / ".config" / "telewatch"
        # Create directory with 0o700 (rwx------)
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path = config_dir / "config.yaml"
        
        if config_path.exists():
            if not Confirm.ask(f"Overwrite existing config at {config_path}?", default=False):
                console.print("[yellow]Setup cancelled via overwrite denial.[/yellow]")
                return

        # Save with restricted permissions (0o600: rw-------)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        with os.fdopen(os.open(config_path, flags, 0o600), 'w') as f:
            yaml.dump(self.config_data, f, sort_keys=False)
            
        console.print(Panel.fit(
            f"✅ Configuration saved to:\n[bold]{config_path}[/bold]\n\n"
            "Run [bold green]telewatch start[/bold green] to begin!",
            border_style="green"
        ))
