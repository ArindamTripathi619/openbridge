"""Systemd journal monitoring."""

import subprocess
import threading
import json
from typing import Dict, Any, Optional

from .base import BaseMonitor, Severity


class JournalMonitor(BaseMonitor):
    """Monitor systemd journal for a unit."""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize journal monitor.
        
        Args:
            name: Monitor name.
            config: Configuration with 'unit' and optional 'since'.
        """
        super().__init__(name, config)
        
        self.unit = config["unit"]
        self.since = config.get("since", "now")
        
        self.process: Optional[subprocess.Popen] = None
        self.monitor_thread: Optional[threading.Thread] = None
    
    def start(self):
        """Start monitoring journal."""
        if self._running:
            return
        
        self._running = True
        
        # Start journalctl process
        cmd = [
            "journalctl",
            "-u", self.unit,
            "-f",  # Follow
            "-o", "json",  # JSON output
            "--since", self.since,
        ]
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to start journalctl: {e}")
        
        # Start reading thread
        self.monitor_thread = threading.Thread(target=self._read_journal, daemon=True)
        self.monitor_thread.start()
        
        self._emit_event(
            f"Started monitoring journal for {self.unit}",
            Severity.INFO,
            {"unit": self.unit}
        )
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
        
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
            self.monitor_thread = None
    
    def _read_journal(self):
        """Read journal entries."""
        if not self.process or not self.process.stdout:
            return
        
        for line in iter(self.process.stdout.readline, ""):
            if not self._running:
                break
            
            line = line.strip()
            if not line:
                continue
            
            try:
                # Parse JSON entry
                entry = json.loads(line)
                
                # Extract relevant fields
                message = entry.get("MESSAGE", "")
                priority = entry.get("PRIORITY", "6")  # Default: info
                
                # Map priority to severity
                # 0-2: critical, 3-4: warning, 5-7: info
                priority_int = int(priority)
                if priority_int <= 2:
                    severity = Severity.CRITICAL
                elif priority_int <= 4:
                    severity = Severity.WARNING
                else:
                    severity = Severity.INFO
                
                # Emit event
                self._emit_event(
                    message,
                    severity,
                    {
                        "unit": self.unit,
                        "priority": priority,
                        "syslog_identifier": entry.get("SYSLOG_IDENTIFIER", ""),
                    }
                )
            
            except json.JSONDecodeError:
                # Skip invalid JSON
                continue
            except Exception as e:
                self._emit_event(
                    f"Error parsing journal entry: {e}",
                    Severity.WARNING,
                    {"error": str(e)}
                )
