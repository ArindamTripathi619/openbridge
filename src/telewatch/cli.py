"""Main CLI interface for telewatch."""

import argparse
import sys
import time
import signal
import os
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from .config import Config, ConfigError, create_example_config
from .monitors import FileMonitor, PIDMonitor, JournalMonitor, BaseMonitor
from .analyzers import create_llm_client, EventAnalyzer, ROTATOR_PRESETS
from .notifiers import TelegramNotifier
from .core.logging import get_logger
from .storm_detector import StormDetector
from .opencode_runner import OpenCodeRunner

logger = get_logger("manager")


class MonitorManager:
    """Manages all monitors and orchestrates the monitoring loop."""
    
    def __init__(self, config: Config, turbo: bool = False):
        """Initialize manager.
        
        Args:
            config: Configuration object.
        """
        self.config = config
        self.monitors: List[BaseMonitor] = []
        self.running = False
        self.paused = False  # New paused state
        self.process_completed = False  # Track if we detected completion
        
        # Initialize LLM and notifier
        llm_config = config.get_llm_config()
        self.llm_client = create_llm_client(llm_config)
        
        # Get optimization config and initialize analyzer
        # Get optimization config
        optimization_config = config.get_llm_optimization_config()
        
        # Initialize Shared Token Tracker
        from .analyzers.token_tracker import TokenUsageTracker
        self.token_tracker = TokenUsageTracker()
        
        # Initialize analyzer with shared tracker
        self.analyzer = EventAnalyzer(
            self.llm_client, 
            optimization_config=optimization_config,
            token_tracker=self.token_tracker,
            turbo=turbo
        )
        logger.info("LLM optimizations enabled (cache, patterns, context trimming)")
        
        telegram_config = config.get_telegram_config()
        notification_config = config.get_notification_config()
        self.notifier = TelegramNotifier(
            bot_token=telegram_config["bot_token"],
            chat_id=telegram_config["chat_id"],
            rate_limit_per_hour=notification_config["rate_limit_per_hour"],
            debounce_seconds=notification_config["debounce_seconds"]
        )
        
        # Initialize Opencode Integration
        op_cfg = config.get_opencode_config()
        self.opencode_enabled = op_cfg.get("enabled", False)
        if self.opencode_enabled:
            if OpenCodeRunner.is_available():
                self.storm_detector = StormDetector(
                    threshold=op_cfg.get("storm_threshold", 10),
                    window_seconds=op_cfg.get("storm_window_seconds", 60),
                    cooldown_seconds=op_cfg.get("cooldown_seconds", 300)
                )
                self.opencode_runner = OpenCodeRunner(
                    working_dir=op_cfg.get("working_dir", "."),
                    timeout=op_cfg.get("timeout_seconds", 120),
                    model=op_cfg.get("model")
                )
                logger.info("OpenCode framework initialized for alert gating.")
            else:
                logger.warning("OpenCode enabled in config but binary not found in PATH.")
                self.opencode_enabled = False
        
        # Initialize progress tracking if enabled
        from .tracker import ProgressTracker
        from .generators import StatusReportGenerator
        from .notifiers.telegram_listener import TelegramListener
        from .core.history import HistoryManager
        
        self.history_manager = HistoryManager()
        progress_config = config.get_progress_tracking_config()
        process_config = config.get_process_config()
        interactive_config = config.get_interactive_config()
        
        self.progress_enabled = progress_config["enabled"]
        self.progress_tracker = None
        self.report_generator = None
        self.message_listener = None
        self.ui_callback = None  # Callback for UI/State updates
        
        if self.progress_enabled:
            # Merge configs for tracker
            tracker_config = {**process_config, **progress_config}
            self.progress_tracker = ProgressTracker(tracker_config)
            # Initialize generator with shared token tracker
            self.report_generator = StatusReportGenerator(
                self.llm_client,
                token_tracker=self.token_tracker
            )
            # Use historical duration if not in config
            if not self.progress_tracker.expected_duration:
                avg_duration = self.history_manager.get_average_duration(process_config['name'])
                if avg_duration > 0:
                    self.progress_tracker.expected_duration = avg_duration / 60  # minutes
                    logger.info(f"Using historical average duration: {self.progress_tracker.expected_duration:.1f}m")
            
            logger.info(f"Progress tracking enabled for: {process_config['name']}")
        
        # Initialize message listener if interactive features enabled
        if interactive_config["listen_for_messages"]:
            self.message_listener = TelegramListener(
                bot_token=telegram_config["bot_token"],
                chat_id=telegram_config["chat_id"]
            )
            # Set callback for status reports
            self.message_listener.set_message_callback(self._handle_user_message)
            logger.info("Telegram message listening enabled")
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def set_ui_callback(self, callback):
        """Set callback for UI updates (progress, status)."""
        self.ui_callback = callback

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Shutdown signal received ({signum})")
        self.stop()
        sys.exit(0)
    
    def setup_monitors(self):
        """Create and configure monitors from config."""
        monitor_configs = self.config.get_monitors()
        
        for mon_config in monitor_configs:
            mon_type = mon_config["type"]
            name = mon_config.get("name", f"{mon_type}_monitor")
            
            try:
                if mon_type == "file":
                    # Inject progress patterns if tracking enabled
                    if self.progress_enabled:
                        common_patterns = [
                            r"(\d+(?:\.\d+)?)%",
                            r"(\d+)\s*/\s*(\d+)",
                            r"progress:\s*(\d+(?:\.\d+)?)",
                            r"completed:\s*(\d+(?:\.\d+)?)%",
                            r"INFO:.*Progress",  # Specific to our format
                        ]
                        mon_config["progress_regexes"] = common_patterns
                        
                    monitor = FileMonitor(name, mon_config)
                elif mon_type == "pid":
                    monitor = PIDMonitor(name, mon_config)
                elif mon_type == "journal":
                    monitor = JournalMonitor(name, mon_config)
                else:
                    print(f"Warning: Unknown monitor type '{mon_type}', skipping")
                    continue
                
                self.monitors.append(monitor)
                logger.info(f"Configured {mon_type} monitor: {name}")
            
            except Exception as e:
                logger.error(f"Failed to setup {mon_type} monitor '{name}': {e}")
    
    def start(self):
        """Start all monitors and main loop."""
        if not self.monitors:
            print("No monitors configured!")
            return
        
        logger.info(f"Starting {len(self.monitors)} monitor(s)...")
        
        # Start all monitors
        for monitor in self.monitors:
            try:
                monitor.start()
                logger.debug(f"  ▶ {monitor.name} started")
            except Exception as e:
                logger.error(f"Failed to start {monitor.name}: {e}")
        
        self.running = True
        logger.info("Monitoring active. Press Ctrl+C to stop.")
        
        # Main event loop
        self._event_loop()
    
    def _event_loop(self):
        """Main event processing loop."""
        import time
        import psutil
        last_progress_check = time.time()
        last_process_check = time.time()
        progress_check_interval = 2  # Check progress every 2 seconds
        process_check_interval = 10  # Check process alive status every 10s
        
        # Initial UI update
        if self.ui_callback:
            self.ui_callback(0.0, "Starting monitors...")
        
        while self.running:
            if self.paused:
                time.sleep(1)
                continue
                
            current_time = time.time()
            
            # Collect events from all monitors
            for monitor in self.monitors:
                if not monitor.is_running():
                    continue
                
                events = monitor.get_events()
                
                for event in events:
                    try:
                        # Feed log line to progress tracker
                        if self.progress_tracker and hasattr(event, 'content'):
                            self.progress_tracker.add_log_line(event.content)
                        
                        # Check for completion flags
                        if getattr(event, 'metadata', {}).get("is_completion") and not self.process_completed:
                            self._handle_process_completion()
                            continue
                        
                        # Process terminated event from PIDMonitor
                        if hasattr(event, 'content') and "has terminated" in str(event.content) and not self.process_completed:
                            self._handle_process_completion()
                        
                        # Analyze event (skip if it's just a progress update)
                        if getattr(event, 'metadata', {}).get("is_progress"):
                            continue
                            
                        analysis = self.analyzer.analyze_event(event)
                        
                        # Handle main analysis
                        if self._dispatch_alert(analysis, log_tail=str(getattr(event, 'content', ''))):
                            logger.info(f"Analysis sent or gated.")
                        
                        # Handle any side anomalies (e.g. spikes)
                        while getattr(self.analyzer, 'pending_anomalies', []):
                            side_analysis = self.analyzer.pending_anomalies.pop(0)
                            if self._dispatch_alert(side_analysis):
                                logger.info(f"Side-analysis sent or gated.")
                    
                    except Exception as e:
                        logger.error(f"Error processing event: {e}")
            
            # Periodic behavioral metrics and stall tracking
            if (current_time - last_progress_check) >= progress_check_interval:
                last_progress_check = current_time
                
                try:
                    # 1. Update UI/State with behavioral metrics
                    p_val = 0.0
                    p_msg = "Monitoring active"
                    
                    if self.progress_tracker:
                        p_val = self.progress_tracker.estimate_progress() or 0.0
                        
                        # Check for milestones and stall (specific to progress)
                        if self.progress_tracker.should_send_update():
                            self._handle_milestone()
                        
                        if self.progress_tracker.is_stalled():
                            self._handle_progress_stall()
                    
                    if self.ui_callback:
                        self.ui_callback(p_val, p_msg)
                            
                    # 2. Check for general log stream stall
                    stall_analysis = self.analyzer.check_stall()
                    if stall_analysis:
                        self._dispatch_alert(stall_analysis)
                        logger.warning(f"Stall alert evaluated.")
                
                except Exception as e:
                    logger.error(f"Periodic check error: {e}")
                    
            # Structural check if process is still alive
            if (current_time - last_process_check) >= process_check_interval:
                last_process_check = current_time
                if self.progress_tracker and self.progress_tracker.process_name and not self.process_completed:
                    try:
                        target = self.progress_tracker.process_name.lower()
                        is_running = False
                        
                        for p in psutil.process_iter(['name', 'cmdline']):
                            try:
                                cmd = " ".join(p.info.get('cmdline') or [])
                                name = p.info.get('name') or ""
                                if target in name.lower() or target in cmd.lower():
                                    # Ignore our own tools
                                    if "telewatch" not in cmd and "tail" not in cmd and "nano" not in cmd and "vi" not in cmd:
                                        is_running = True
                                        break
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                pass
                                
                        # If we have tracked some progress (i.e. it 'started' successfully) but now it's gone
                        if not is_running and self.progress_tracker.current_percentage > 0:
                            logger.info(f"Target process '{target}' is no longer running in psutil.")
                            self._handle_process_completion()
                    except Exception as e:
                        logger.debug(f"Process check error: {e}")
            
            # Poll for user messages
            if self.message_listener:
                try:
                    self.message_listener.poll_once()
                except Exception as e:
                    logger.error(f"Message polling error: {e}")
                    # Error backoff to prevent API spam and conflict loops
                    time.sleep(2)
            
            # Frequent UI update (every loop ~1s)
            if self.ui_callback:
                current_pct = self.progress_tracker.current_percentage if self.progress_tracker else 0.0
                self.ui_callback(current_pct, "Monitoring active")
            
            # Sleep briefly
            time.sleep(1)
    
    def _handle_milestone(self):
        """Handle progress milestone reached."""
        current_pct = self.progress_tracker.current_percentage
        milestone_pct = int(current_pct)
        milestone = (milestone_pct // 10) * 10  # Round to nearest 10%
        
        if current_pct >= 99.9: # Treat 99.9+ as complete
            # Process Complete!
            report = self.report_generator.generate_completion_report(self.progress_tracker)
            print(f"🎉 Process completed!")
            
            # Record in history
            duration = (datetime.now() - self.progress_tracker.start_time).total_seconds()
            self.history_manager.record_run(
                self.progress_tracker.process_name,
                self.progress_tracker.start_time,
                duration,
                "completed"
            )
        else:
            # Generate and send milestone report with analysis
            report = self.report_generator.generate_milestone_report(
                self.progress_tracker,
                milestone,
                include_llm_summary=True
            )
            print(f"📊 Progress update sent: {milestone}%")
        
        self.notifier.send_message(report)
        self.progress_tracker.mark_update_sent()
        
    def _handle_progress_stall(self):
        """Handle progress tracker specifically stalled."""
        stall_msg = f"⚠️ **Warning:** {self.progress_tracker.process_name} appears stalled at {self.progress_tracker.current_percentage:.1f}%"
        self._dispatch_alert(stall_msg)
        logger.warning(f"Progress stall alert evaluated for {self.progress_tracker.process_name}")

    def _handle_user_message(self, text: str, is_command: bool = False, cmd: str = None, args: List[str] = None):
        """Handle incoming user message or command."""
        if not is_command:
            # Default behavior for non-commands (if configured)
            interactive_config = self.config.get_interactive_config()
            if interactive_config.get("status_on_any_message"):
                self._send_status_report()
            return

        logger.info(f"Telegram command received: /{cmd} {args}")
        
        if cmd == "status":
            self._send_status_report()
        elif cmd == "pause":
            self.paused = True
            self.notifier.send_message("⏸️ **Monitoring Paused.** Monitors are still active but analysis is suspended.")
            logger.info("Monitoring state changed to PAUSED")
        elif cmd == "resume":
            self.paused = False
            self.notifier.send_message("▶️ **Monitoring Resumed.**")
            logger.info("Monitoring state changed to RUNNING")
        elif cmd == "logs":
            self._send_recent_logs()
        elif cmd == "stop":
            self.notifier.send_message("🛑 <b>TeleWatch stopped remotely.</b>\nMonitoring has been terminated.")
            logger.info("Remote /stop command received – shutting down")
            self.stop()
        elif cmd == "run":
            if getattr(self, "opencode_enabled", False) and getattr(self, "opencode_runner", None):
                query = " ".join(args) if args else ""
                if not query:
                    self.notifier.send_message("❌ Please provide a command (e.g. /run check disk usage)")
                    return
                self.notifier.send_message(f"🏃 Running: `{query}`...")
                
                import asyncio
                from .opencode_prompts import manual_prompt
                proc_name = self.progress_tracker.process_name if getattr(self, "progress_tracker", None) else "unknown"
                prompt = manual_prompt(query, "telewatch logs", proc_name)
                
                try:
                    result = asyncio.run(self.opencode_runner.run(prompt))
                    chunks = self.opencode_runner.chunk(result)
                    for c in chunks:
                        self.notifier.send_message(f"💻 **Output:**\n```\n{c}\n```")
                except Exception as e:
                    self.notifier.send_message(f"❌ OpenCode error: {e}")
            else:
                self.notifier.send_message("❌ OpenCode is not available/enabled.")
        else:
            self.notifier.send_message(f"❓ **Unknown command:** /{cmd}\nAvailable: /status, /pause, /resume, /logs, /run, /stop")

    def _dispatch_alert(self, item, log_tail: str = "") -> bool:
        """
        Intercept outgoing alerts (strings or Analysis objects) applying storm gating.
        Executes OpenCode tier-2 evaluation if a storm is triggered.
        """
        import asyncio
        
        # 1. Storm Gating
        if getattr(self, "opencode_enabled", False) and getattr(self, "storm_detector", None):
            is_new_storm = self.storm_detector.record_alert()
            
            if self.storm_detector.is_storming():
                if is_new_storm:
                    logger.warning("Alert STORM detected. Escalating to OpenCode...")
                    # Build prompt
                    from .opencode_prompts import storm_prompt
                    prompt = storm_prompt(
                        log_path="monitored process",
                        log_tail=log_tail,
                        alert_count=self.storm_detector.threshold,
                        window_seconds=self.storm_detector.window_seconds,
                        keywords=[]
                    )
                    
                    self.notifier.send_message(f"🌪️ **Alert Storm Detected!** ({self.storm_detector.threshold} alerts in {self.storm_detector.window_seconds}s). OpenCode is investigating...")
                    
                    try:
                        # Synchronous execution of async OpenCode
                        result = asyncio.run(self.opencode_runner.run(prompt))
                        # Send the result chunks
                        chunks = self.opencode_runner.chunk(result)
                        for c in chunks:
                            self.notifier.send_message(f"🤖 **OpenCode Diagnosis:**\n```\n{c}\n```")
                    except Exception as e:
                        logger.error(f"OpenCode run failed: {e}")
                        self.notifier.send_message("❌ **OpenCode Failed:** Returning to normal alerts.")
                    
                    # Enter cooldown after reporting
                    self.storm_detector.enter_cooldown()
                    
                else:
                    # We are in storm/cooldown, suppress individual alert.
                    logger.debug("Alert suppressed due to storm cooldown.")
                return True # Handled/suppressed
            
        # 2. Normal dispatch
        if hasattr(item, "summary"):
            return self.notifier.send_analysis(item)
        else:
            return self.notifier.send_message(item)

    def _send_status_report(self):
        """Generate and send full status report."""
        try:
            if self.progress_tracker and self.report_generator:
                report = self.report_generator.generate_report(
                    self.progress_tracker,
                    include_llm_summary=True
                )
                self.notifier.send_message(report)
            else:
                self.notifier.send_message("✓ TeleWatch is running and active.")
        except Exception as e:
            print(f"Error sending status: {e}")

    def _send_recent_logs(self):
        """Send a snippet of recent logs."""
        if not self.progress_tracker or not self.progress_tracker.recent_logs:
            self.notifier.send_message("No logs available yet.")
            return
            
        logs = "\n".join(self.progress_tracker.recent_logs[-15:])
        self.notifier.send_message(f"📋 **Recent Logs:**\n```\n{logs}\n```")

    def _handle_process_completion(self):
        """Handle detected process completion. Send final summary with LLM stats."""
        self.process_completed = True
        logger.info("Process completion detected in logs!")
        
        lines = ["🎉 <b>Process Completed Successfully!</b>", ""]
        
        # Include progress info if available
        if self.progress_tracker:
            duration = (datetime.now() - self.progress_tracker.start_time).total_seconds()
            mins, secs = divmod(int(duration), 60)
            lines.append(f"⏱️ <b>Duration:</b> {mins}m {secs}s")
            lines.append(f"📈 <b>Final Progress:</b> {self.progress_tracker.current_percentage:.1f}%")
            
            # Record in history
            self.history_manager.record_run(
                self.progress_tracker.process_name,
                self.progress_tracker.start_time,
                duration,
                "completed"
            )
        
        # Include LLM analysis summary
        if self.token_tracker:
            stats = self.token_tracker.get_stats("current")
            lines.append("")
            lines.append("📊 <b>LLM Analysis Summary:</b>")
            lines.append(f"  • Total Requests: {stats['total_requests']}")
            lines.append(f"  • LLM Calls: {stats['llm_calls']}")
            lines.append(f"  • Cache Hits: {stats['cached_calls']} ({stats['cache_hit_rate']}%)")
            lines.append(f"  • Pattern Matches: {stats['pattern_matched']} ({stats['pattern_match_rate']}%)")
            lines.append(f"  • Tokens Used: {stats['total_tokens']:,}")
        
        message = "\n".join(lines)
        self.notifier.send_message(message)
        print("🎉 Process completed! Final summary sent.")

    def stop(self):
        """Stop all monitors and Telegram polling."""
        self.running = False
        
        logger.info("Stopping monitors...")
        for monitor in self.monitors:
            try:
                monitor.stop()
            except Exception as e:
                logger.error(f"Error stopping {monitor.name}: {e}")
        
        # Stop Telegram listener polling
        if self.message_listener:
            try:
                self.message_listener.stop_polling()
                logger.info("Telegram listener stopped")
            except Exception as e:
                logger.debug(f"Listener stop: {e}")


def cmd_setup(args):
    """Interactive setup wizard."""
    print("🤖 TeleWatch Setup Wizard")
    print("=" * 40)
    print()
    
    config_data = {
        "telegram": {},
        "llm": {},
        "notification": {
            "debounce_seconds": 300,
            "rate_limit_per_hour": 50,
            "severity_levels": ["critical", "warning", "info"]
        },
        "monitors": []
    }
    
    # Step 1: Telegram Configuration
    print("[1/5] Telegram Bot Configuration")
    print("-" * 40)
    print("To create a Telegram bot:")
    print("  1. Open Telegram and search for @BotFather")
    print("  2. Send: /newbot")
    print("  3. Follow instructions to create bot")
    print("  4. Copy the bot token")
    print()
    
    # Get and validate Telegram credentials
    while True:
        bot_token = input("Enter Telegram Bot Token: ").strip()
        if not bot_token:
            print("❌ Bot token cannot be empty")
            continue
        
        chat_id = input("Enter your Telegram Chat ID: ").strip()
        print("  (Don't know? Send /start to @userinfobot)")
        if not chat_id:
            print("❌ Chat ID cannot be empty")
            continue
        
        # Validate Telegram setup
        print("✓ Validating Telegram setup...")
        try:
            notifier = TelegramNotifier(bot_token, chat_id)
            if notifier.send_test_message():
                print("✓ Success! Test message sent to Telegram!")
                config_data["telegram"]["bot_token"] = bot_token
                config_data["telegram"]["chat_id"] = chat_id
                break
            else:
                print("❌ Failed to send test message. Check your bot token and chat ID.")
        except Exception as e:
            print(f"❌ Telegram validation failed: {e}")
            retry = input("Try again? [Y/n]: ").strip().lower()
            if retry == 'n':
                print("Setup cancelled.")
                sys.exit(1)
    
    # Step 2: LLM Provider Configuration
    print()
    print("[2/5] LLM Provider Configuration")
    print("-" * 40)
    print("Choose your LLM provider:")
    print("  1) OpenAI (Recommended, ~$0.15/1M tokens)")
    print("  2) Anthropic (High quality, ~$0.25/1M tokens)")
    print("  3) Groq (FREE tier, fast)")
    print("  4) Ollama (Local, private, FREE)")
    print("  5) Local API Rotator (LiteLLM Proxy - Resilient & Rotated)")
    print()
    
    provider_map = {"1": "openai", "2": "anthropic", "3": "groq", "4": "ollama", "5": "local-rotator"}
    model_map = {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-3-5-haiku-20241022",
        "groq": "llama-3.3-70b-versatile",
        "ollama": "llama3.2",
        "local-rotator": "groq-llama"
    }
    
    while True:
        choice = input("Choice [1-5]: ").strip()
        provider = provider_map.get(choice)
        if provider:
            break
        print("❌ Invalid choice. Please enter 1, 2, 3, 4, or 5")
    
    config_data["llm"]["provider"] = provider
    config_data["llm"]["model"] = model_map[provider]
    
    # Get API key for non-local providers
    if provider != "ollama":
        print(f"\nSetting up {provider.capitalize()}...")
        if provider == "groq":
            print("Get FREE API key: https://console.groq.com/keys")
        elif provider == "openai":
            print("Get API key: https://platform.openai.com/api-keys")
        elif provider == "anthropic":
            print("Get API key: https://console.anthropic.com/")
        print()
        
        while True:
            api_key = input(f"Enter {provider.capitalize()} API Key: ").strip()
            if not api_key:
                print("❌ API key cannot be empty")
                continue
            
            # Validate LLM setup
            print("✓ Testing API key...")
            try:
                from .analyzers import create_llm_client
                test_config = {
                    "provider": provider,
                    "api_key": api_key,
                    "model": config_data["llm"]["model"]
                }
                if provider == "local-rotator":
                    test_config["base_url"] = config_data["llm"].get("base_url", "http://localhost:8000/v1")
                
                client = create_llm_client(test_config)
                # Make a simple test call
                response = client.analyze("Respond with 'OK' if you can read this.")
                print(f"✓ API key validated! Model: {config_data['llm']['model']}")
                config_data["llm"]["api_key"] = api_key
                break
            except Exception as e:
                print(f"❌ API validation failed: {e}")
                retry = input("Try again? [Y/n]: ").strip().lower()
                if retry == 'n':
                    print("Setup cancelled.")
                    sys.exit(1)
    elif provider == "local-rotator":
        print("\n[Local API Rotator Setup]")
        base_url = input("Enter Local Rotator URL [http://localhost:8000/v1]: ").strip() or "http://localhost:8000/v1"
        config_data["llm"]["base_url"] = base_url
        
        print("\nSelect Rotator Provider:")
        providers = list(ROTATOR_PRESETS.keys())
        for i, p in enumerate(providers, 1):
            print(f"  {i}) {p}")
        print(f"  {len(providers)+1}) Custom")
        
        p_choice = input(f"Choice [1-{len(providers)+1}]: ").strip()
        
        if p_choice.isdigit() and 1 <= int(p_choice) <= len(providers):
            provider_key = providers[int(p_choice) - 1]
            models = ROTATOR_PRESETS[provider_key]
            
            print(f"\nSelect {provider_key} Model:")
            for i, (m_id, m_desc) in enumerate(models, 1):
                print(f"  {i}) {m_id} - {m_desc}")
            
            m_choice = input(f"Choice [1-{len(models)}]: ").strip()
            if m_choice.isdigit() and 1 <= int(m_choice) <= len(models):
                config_data["llm"]["model"] = models[int(m_choice) - 1][0]
            else:
                config_data["llm"]["model"] = models[0][0] # Default to first
        else:
            cust_model = input("Enter custom model name: ").strip()
            if cust_model:
                config_data["llm"]["model"] = cust_model
            else:
                config_data["llm"]["model"] = "groq-llama"

        config_data["llm"]["api_key"] = "sk-local-rotator" # Default dummy key
        print(f"\n✓ Local Rotator configured: {base_url}")
        print(f"  Model: {config_data['llm']['model']}")
    elif provider == "ollama":
        print("\n✓ Using Ollama (local)")
        config_data["llm"]["base_url"] = "http://localhost:11434"
        print(f"  Model: {config_data['llm']['model']}")
    
    # Step 3: Process Information (NEW)
    print()
    print("[3/7] Process Information")
    print("-" * 40)
    print("Help telewatch understand what you're monitoring:")
    print()
    
    process_name = input("Process name (e.g., 'Data Migration', 'Model Training') [My Process]: ").strip()
    if not process_name:
        process_name = "My Process"
    
    print("\nProvide a brief description of what this process does:")
    description = input("Description: ").strip()
    
    print("\nKeywords to watch for in logs (comma-separated):")
    print("  Examples: 'progress, processed, complete, error, batch'")
    keywords_input = input("Keywords: ").strip()
    keywords = [k.strip() for k in keywords_input.split(",")] if keywords_input else []
    
    expected_min = input("\nExpected duration in minutes (optional, for better estimates) [skip]: ").strip()
    expected_duration = None
    if expected_min:
        try:
            expected_duration = int(expected_min)
        except ValueError:
            print("⚠️ Invalid number, skipping duration estimate")
    
    config_data["process"] = {
        "name": process_name,
        "description": description,
        "keywords": keywords,
        "expected_duration_minutes": expected_duration
    }
    
    # Enable progress tracking by default
    config_data["progress_tracking"] = {
        "enabled": True,
        "update_interval_percent": 10,
        "min_update_interval_seconds": 60
    }
    
    # Enable interactive features
    config_data["interactive"] = {
        "listen_for_messages": True,
        "status_on_any_message": True
    }
    
    print(f"\n✓ Process configured: {process_name}")
    if keywords:
        print(f"✓ Watching for keywords: {', '.join(keywords[:3])}{'...' if len(keywords) > 3 else ''}")
    
    # Step 4: Monitor Configuration
    print()
    print("[4/7] Monitor Configuration")
    print("-" * 40)
    print("What would you like to monitor?")
    print()
    
    # File monitor
    add_file = input("Monitor log files? [Y/n]: ").strip().lower()
    if add_file != 'n':
        while True:
            file_path = input("  File path: ").strip()
            if not file_path:
                break
            
            # Validate file exists and is readable
            path_obj = Path(file_path).expanduser()
            if not path_obj.exists():
                print(f"  ❌ File not found: {file_path}")
                continue
            if not path_obj.is_file():
                print(f"  ❌ Not a file: {file_path}")
                continue
            if not os.access(path_obj, os.R_OK):
                print(f"  ❌ File not readable: {file_path}")
                continue
            
            print(f"  ✓ File found and readable")
            
            keywords_input = input("  Keywords to watch (comma-separated) [ERROR,FATAL,Exception]: ").strip()
            keywords = [k.strip() for k in keywords_input.split(",")] if keywords_input else ["ERROR", "FATAL", "Exception"]
            
            name = input(f"  Monitor name [{path_obj.name}]: ").strip() or path_obj.name
            
            config_data["monitors"].append({
                "type": "file",
                "name": name,
                "path": str(path_obj),
                "keywords": keywords
            })
            print(f"  ✓ File monitor configured: {name}")
            
            another = input("\n  Add another file? [y/N]: ").strip().lower()
            if another != 'y':
                break
    
    # PID monitor
    print()
    add_pid = input("Monitor process by PID? [y/N]: ").strip().lower()
    if add_pid == 'y':
        while True:
            try:
                pid_input = input("  PID: ").strip()
                if not pid_input:
                    break
                
                pid = int(pid_input)
                
                # Validate PID exists
                import psutil
                if not psutil.pid_exists(pid):
                    print(f"  ❌ Process {pid} not found")
                    continue
                
                try:
                    proc = psutil.Process(pid)
                    proc_name = proc.name()
                    print(f"  ✓ Found process: {proc_name} (PID {pid})")
                except:
                    print(f"  ❌ Cannot access process {pid}")
                    continue
                
                interval = input("  Check interval in seconds [30]: ").strip()
                interval = int(interval) if interval else 30
                
                name = input(f"  Monitor name [{proc_name}]: ").strip() or proc_name
                
                config_data["monitors"].append({
                    "type": "pid",
                    "name": name,
                    "pid": pid,
                    "check_interval": interval
                })
                print(f"  ✓ PID monitor configured: {name}")
                
                another = input("\n  Add another PID? [y/N]: ").strip().lower()
                if another != 'y':
                    break
            except ValueError:
                print("  ❌ Invalid PID")
    
    # Journal monitor
    print()
    add_journal = input("Monitor systemd service? [y/N]: ").strip().lower()
    if add_journal == 'y':
        # Check if journalctl is available
        import subprocess
        try:
            subprocess.run(["journalctl", "--version"], capture_output=True, check=True)
        except:
            print("  ❌ journalctl not available on this system")
        else:
            while True:
                unit = input("  Service unit (e.g., nginx.service): ").strip()
                if not unit:
                    break
                
                name = input(f"  Monitor name [{unit}]: ").strip() or unit
                
                config_data["monitors"].append({
                    "type": "journal",
                    "name": name,
                    "unit": unit,
                    "since": "now"
                })
                print(f"  ✓ Journal monitor configured: {name}")
                
                another = input("\n  Add another service? [y/N]: ").strip().lower()
                if another != 'y':
                    break
    
    if not config_data["monitors"]:
        print("\n⚠️  No monitors configured! You can add them later in the config file.")
    
    # Step 5: Notification Settings
    print()
    print("[5/7] Notification Settings")
    print("-" * 40)
    
    rate_input = input(f"Max notifications per hour [10]: ").strip()
    if rate_input:
        try:
            config_data["notification"]["rate_limit_per_hour"] = int(rate_input)
        except ValueError:
            pass
    
    print(f"✓ Rate limit: {config_data['notification']['rate_limit_per_hour']}/hour")
    
    # Step 6: OpenCode Integration
    print()
    print("[6/7] OpenCode Intelligence")
    print("-" * 40)
    
    use_opencode = input("Enable OpenCode for advanced error handling and /run commands? [Y/n]: ").strip().lower()
    if use_opencode != 'n':
        # Quick check if it's installed
        import shutil
        if shutil.which("opencode"):
            print("  ✓ opencode binary found!")
        else:
            print("  ⚠️ opencode not found in PATH. You can install it later:")
            print("     curl -fsSL https://opencode.ai/install | sh")
            
        config_data["opencode"] = {
            "enabled": True,
            "working_dir": ".",
            "timeout_seconds": 120,
            "storm_threshold": 10,
            "storm_window_seconds": 60,
            "cooldown_seconds": 300
        }
    else:
        config_data["opencode"] = {"enabled": False}
        
    # Step 7: Save Configuration
    print()
    print("[7/7] Save Configuration")
    print("-" * 40)
    
    # Display summary
    print("\nConfiguration Summary:")
    print(f"  • Telegram: ✓ Bot configured")
    print(f"  • LLM: ✓ {provider.capitalize()} ({config_data['llm']['model']})")
    print(f"  • Monitors: {len(config_data['monitors'])} configured")
    print(f"  • Rate limit: {config_data['notification']['rate_limit_per_hour']}/hour")
    print(f"  • OpenCode: {'Enabled' if config_data['opencode']['enabled'] else 'Disabled'}")
    print()
    
    # Create config directory
    config_dir = Path.home() / ".telewatch"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.yaml"
    
    save = input(f"Save to {config_path}? [Y/n]: ").strip().lower()
    if save == 'n':
        print("Setup cancelled.")
        sys.exit(0)
    
    # Write config file
    import yaml
    with open(config_path, 'w') as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
    
    print(f"\n✓ Configuration saved to: {config_path}")
    print()
    
    # Offer to start
    print("🚀 Setup complete! What next?")
    print("  1) Start monitoring now")
    print("  2) Exit (start manually later)")
    print()
    
    next_choice = input("Choice [1-2]: ").strip()
    if next_choice == "1":
        print("\nStarting telewatch...\n")
        # Load config and start
        args.config = config_path
        cmd_start(args)
    else:
        print("\n✓ All done! Start monitoring with:")
        print(f"   telewatch start")
        print()



def cmd_test_notification(args):
    """Send a test notification."""
    try:
        config = Config(args.config)
        telegram_config = config.get_telegram_config()
        
        notifier = TelegramNotifier(
            bot_token=telegram_config["bot_token"],
            chat_id=telegram_config["chat_id"]
        )
        
        if notifier.send_test_message():
            print("✓ Test message sent successfully!")
        else:
            print("✗ Failed to send test message")
            sys.exit(1)
    
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_start(args):
    """Start monitoring."""
    try:
        config = Config(args.config)
        manager = MonitorManager(config)
        manager.setup_monitors()
        manager.start()
    
    except ConfigError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nStopped by user")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="TeleWatch - Universal process monitoring with LLM analysis"
    )
    
    parser.add_argument(
        "-c", "--config",
        type=Path,
        help="Path to configuration file"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Commands")
    
    # Setup command
    subparsers.add_parser("setup", help="Interactive setup wizard")
    
    # Start command
    subparsers.add_parser("start", help="Start monitoring")
    
    # Test notification command
    subparsers.add_parser("test-notification", help="Send test notification")
    
    args = parser.parse_args()
    
    # Default to start if no command
    if not args.command:
        args.command = "start"
    
    # Route to command handlers
    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "test-notification":
        cmd_test_notification(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
