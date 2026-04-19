"""Process monitoring by PID."""

import os
import psutil
import threading
import time
from typing import Dict, Any, Optional

from .base import BaseMonitor, Severity


class PIDMonitor(BaseMonitor):
    """Monitor a process by its PID."""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize PID monitor.
        
        Args:
            name: Monitor name.
            config: Configuration with 'pid' and optional 'check_interval'.
        """
        super().__init__(name, config)
        
        self.pid = int(config["pid"])
        self.check_interval = config.get("check_interval", 30)  # seconds
        
        # Verify process exists
        if not psutil.pid_exists(self.pid):
            raise ValueError(f"Process with PID {self.pid} does not exist")
        
        try:
            self.process = psutil.Process(self.pid)
            self.initial_status = self.process.status()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            raise ValueError(f"Cannot access process {self.pid}: {e}")
        
        self.monitor_thread: Optional[threading.Thread] = None
        self.last_cpu_percent = 0.0
        self.last_memory_mb = 0.0
    
    def start(self):
        """Start monitoring the process."""
        if self._running:
            return
        
        self._running = True
        
        # Emit initial status
        self._emit_event(
            f"Started monitoring PID {self.pid} ({self.process.name()})",
            Severity.INFO,
            {
                "pid": self.pid,
                "name": self.process.name(),
                "status": self.initial_status,
            }
        )
        
        # Start monitoring thread
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
    
    def stop(self):
        """Stop monitoring."""
        self._running = False
        
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
            self.monitor_thread = None
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                if not psutil.pid_exists(self.pid):
                    self._emit_event(
                        f"Process {self.pid} has terminated (no longer exists)",
                        Severity.CRITICAL,
                        {"pid": self.pid, "reason": "process_not_found"}
                    )
                    self._running = False
                    break
                
                # Check process status
                try:
                    status = self.process.status()
                    
                    # Detect status change
                    if status != self.initial_status:
                        if status == psutil.STATUS_ZOMBIE:
                            self._emit_event(
                                f"Process {self.pid} became a zombie",
                                Severity.CRITICAL,
                                {"pid": self.pid, "status": status}
                            )
                        elif status == psutil.STATUS_STOPPED:
                            self._emit_event(
                                f"Process {self.pid} was stopped",
                                Severity.WARNING,
                                {"pid": self.pid, "status": status}
                            )
                        else:
                            self._emit_event(
                                f"Process {self.pid} status changed: {self.initial_status} -> {status}",
                                Severity.INFO,
                                {"pid": self.pid, "status": status}
                            )
                        self.initial_status = status
                    
                    # Get resource usage
                    cpu_percent = self.process.cpu_percent(interval=1)
                    memory_mb = self.process.memory_info().rss / 1024 / 1024
                    
                    # Check for high CPU/memory (optional alerts)
                    if cpu_percent > 90 and abs(cpu_percent - self.last_cpu_percent) > 10:
                        self._emit_event(
                            f"High CPU usage: {cpu_percent:.1f}%",
                            Severity.WARNING,
                            {"pid": self.pid, "cpu_percent": cpu_percent}
                        )
                    
                    self.last_cpu_percent = cpu_percent
                    self.last_memory_mb = memory_mb
                
                except psutil.NoSuchProcess:
                    # Process terminated
                    self._emit_event(
                        f"Process {self.pid} has terminated",
                        Severity.CRITICAL,
                        {"pid": self.pid}
                    )
                    self._running = False
                    break
                
                except psutil.AccessDenied:
                    self._emit_event(
                        f"Access denied while monitoring PID {self.pid}",
                        Severity.WARNING,
                        {"pid": self.pid}
                    )
            
            except Exception as e:
                self._emit_event(
                    f"Error monitoring PID {self.pid}: {e}",
                    Severity.WARNING,
                    {"pid": self.pid, "error": str(e)}
                )
            
            # Sleep until next check
            time.sleep(self.check_interval)
