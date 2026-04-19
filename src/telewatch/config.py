"""Configuration management for telewatch."""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, List, Optional
import re


class ConfigError(Exception):
    """Configuration error."""
    pass


class Config:
    """Configuration manager."""
    
    CONFIG_SEARCH_PATHS = [
        Path.home() / ".telewatch" / "config.yaml",
        Path.home() / ".config" / "telewatch" / "config.yaml",
        Path("/etc/telewatch/config.yaml"),
        Path("config.yaml"),
    ]
    
    def __init__(self, config_path: Optional[Path] = None):
        """Initialize configuration.
        
        Args:
            config_path: Optional path to config file. If None, searches default locations.
        """
        self.config_path = config_path
        self.data: Dict[str, Any] = {}
        
        if config_path:
            if not config_path.exists():
                raise ConfigError(f"Config file not found: {config_path}")
            self._load(config_path)
        else:
            self._load_from_search_paths()
    
    def _load_from_search_paths(self):
        """Load config from default search paths."""
        for path in self.CONFIG_SEARCH_PATHS:
            if path.exists():
                self._load(path)
                return
        raise ConfigError(
            f"No configuration file found. Searched: {', '.join(str(p) for p in self.CONFIG_SEARCH_PATHS)}"
        )
    
    def _load(self, path: Path):
        """Load configuration from file.
        
        Args:
            path: Path to config file.
        """
        self.config_path = path
        
        # Check file permissions for security
        import stat
        file_stat = path.stat()
        file_mode = stat.S_IMODE(file_stat.st_mode)
        
        # Warn if file has group or other read permissions
        if file_mode & (stat.S_IRGRP | stat.S_IROTH):
            import warnings
            warnings.warn(
                f"Config file {path} has overly permissive permissions ({oct(file_mode)}). "
                f"Recommended: chmod 600 {path}",
                UserWarning
            )
        
        with open(path, "r") as f:
            raw_data = yaml.safe_load(f)
        
        # Substitute environment variables
        self.data = self._substitute_env_vars(raw_data)
        
        # Validate
        self._validate()
    
    def _substitute_env_vars(self, obj: Any) -> Any:
        """Recursively substitute ${VAR} with environment variables.
        
        Args:
            obj: Object to process (dict, list, str, or other).
            
        Returns:
            Processed object with substitutions.
        """
        if isinstance(obj, dict):
            return {k: self._substitute_env_vars(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._substitute_env_vars(item) for item in obj]
        elif isinstance(obj, str):
            # Replace ${VAR} or $VAR with environment variable
            pattern = r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)'
            
            def replace(match):
                var_name = match.group(1) or match.group(2)
                value = os.environ.get(var_name)
                if value is None:
                    raise ConfigError(f"Environment variable not set: {var_name}")
                return value
            
            return re.sub(pattern, replace, obj)
        else:
            return obj
    
    def _validate(self):
        """Validate configuration structure."""
        required_keys = ["telegram", "llm", "monitors"]
        
        for key in required_keys:
            if key not in self.data:
                raise ConfigError(f"Missing required section: {key}")
        
        # Validate telegram section
        telegram = self.data["telegram"]
        if "bot_token" not in telegram or "chat_id" not in telegram:
            raise ConfigError("Telegram section must include 'bot_token' and 'chat_id'")
        
        # Validate llm section
        llm = self.data["llm"]
        if "provider" not in llm:
            raise ConfigError("LLM section must include 'provider'")
        
        # Validate monitors
        monitors = self.data["monitors"]
        if not isinstance(monitors, list) or len(monitors) == 0:
            raise ConfigError("Must configure at least one monitor")
        
        for i, monitor in enumerate(monitors):
            if "type" not in monitor:
                raise ConfigError(f"Monitor {i} missing 'type' field")
            if monitor["type"] not in ["file", "pid", "journal"]:
                raise ConfigError(f"Monitor {i} has invalid type: {monitor['type']}")
    
    def get_telegram_config(self) -> Dict[str, str]:
        """Get Telegram configuration."""
        return self.data["telegram"]
    
    def get_llm_config(self) -> Dict[str, Any]:
        """Get LLM configuration."""
        return self.data["llm"]
    
    def get_notification_config(self) -> Dict[str, Any]:
        """Get notification configuration with defaults."""
        defaults = {
            "debounce_seconds": 300,
            "rate_limit_per_hour": 10,
            "severity_levels": ["critical", "warning", "info"]
        }
        return {**defaults, **self.data.get("notification", {})}
    
    def get_monitors(self) -> List[Dict[str, Any]]:
        """Get list of monitor configurations."""
        return self.data["monitors"]
    
    def get_process_config(self) -> Dict[str, Any]:
        """Get process tracking configuration with defaults."""
        defaults = {
            "name": "Process",
            "description": "",
            "keywords": [],
            "expected_duration_minutes": None,
            "completion_indicators": []
        }
        return {**defaults, **self.data.get("process", {})}
    
    def get_progress_tracking_config(self) -> Dict[str, Any]:
        """Get progress tracking settings with defaults."""
        defaults = {
            "enabled": True,
            "update_interval_percent": 10,
            "min_update_interval_seconds": 300,
            "stall_threshold_minutes": 30,
            "estimation_mode": "auto"
        }
        return {**defaults, **self.data.get("progress_tracking", {})}
    
    def get_interactive_config(self) -> Dict[str, Any]:
        """Get interactive features configuration.
        
        Returns:
            Interactive config dict.
        """
        return self.data.get('interactive', {
            'listen_for_messages': False,
            'status_on_any_message': True
        })
    
    def get_llm_optimization_config(self) -> Dict[str, Any]:
        """Get LLM optimization configuration.
        
        Returns:
            Optimization config dict with defaults.
        """
        llm_config = self.data.get('llm', {})
        opt_config = llm_config.get('optimization', {})
        
        return {
            'enable_cache': opt_config.get('enable_cache', True),
            'cache_max_entries': opt_config.get('cache_max_entries', 100),
            'cache_ttl_seconds': opt_config.get('cache_ttl_seconds', 3600),
            'max_context_lines': opt_config.get('max_context_lines', 15),
            'include_timestamps': opt_config.get('include_timestamps', False),
            'use_local_patterns': opt_config.get('use_local_patterns', True),
            'skip_llm_for_info': opt_config.get('skip_llm_for_info', True),
            'profiler_limit': opt_config.get('profiler_limit', 50),
            'severity_patterns': self.get_severity_patterns()
        }
    
    def get_severity_patterns(self) -> Dict[str, List[str]]:
        """Get severity pattern library.
        
        Returns:
            Pattern dictionary or defaults.
        """
        patterns = self.data.get('severity_patterns', {})
        
        # If empty, return None to use defaults from pattern_matcher
        if not patterns:
            return None
        
        return patterns
    
    def get_anomaly_detection_config(self) -> Dict[str, Any]:
        """Get anomaly detection configuration with defaults.
        
        Returns:
            Anomaly detection config dict.
        """
        defaults = {
            'spike_threshold': 3.0,
            'stall_seconds': 300,
            'novelty_threshold': 0.8
        }
        return {**defaults, **self.data.get('anomaly_detection', {})}


def create_example_config(output_path: Path):
    """Create an example configuration file.
    
    Args:
        output_path: Path where to save example config.
    """
    example = """# TeleWatch Configuration

telegram:
  # Get bot token from @BotFather on Telegram
  bot_token: "${TELEGRAM_BOT_TOKEN}"
  # Your Telegram chat ID (use telewatch setup to find it)
  chat_id: "${TELEGRAM_CHAT_ID}"

llm:
  # Provider: openai, anthropic, groq, ollama
  provider: "openai"
  api_key: "${LLM_API_KEY}"
  model: "gpt-4o-mini"
  # Optional: for Ollama or custom endpoints
  base_url: null

notification:
  # Debounce period in seconds (group similar events)
  debounce_seconds: 300
  # Maximum notifications per hour
  rate_limit_per_hour: 10
  # Which severity levels to send
  severity_levels: [critical, warning, info]

anomaly_detection:
  # Multiplier for spike detection (3.0 = 300% increase)
  spike_threshold: 3.0
  # Time without logs to trigger stall alert (seconds)
  stall_seconds: 300
  # Threshold for novelty detection (0.8 = 80% confidence)
  novelty_threshold: 0.8

monitors:
  # Monitor a log file
  - type: file
    name: "Application Logs"
    path: "/var/log/myapp.log"
    keywords: ["ERROR", "FATAL", "Exception", "Traceback"]
  
  # Monitor a process by PID
  - type: pid
    name: "Training Process"
    pid: 12345
    check_interval: 30
  
  # Monitor systemd journal
  - type: journal
    name: "Web Service"
    unit: "nginx.service"
    since: "5 minutes ago"
"""
    
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(output_path, flags, 0o600), 'w') as f:
        f.write(example)
