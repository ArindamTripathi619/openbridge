"""File and log monitoring."""

import re
import time
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent

from .base import BaseMonitor, Severity


class FileMonitor(BaseMonitor):
    """Monitor log files for specific patterns."""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize file monitor.
        
        Args:
            name: Monitor name.
            config: Configuration with 'path' and optional 'keywords'.
        """
        super().__init__(name, config)
        
        self.file_path = Path(config["path"])
        if not self.file_path.exists():
            raise ValueError(f"File not found: {self.file_path}")
        
        # Compile keyword patterns
        keywords = config.get("keywords", [])
        if keywords:
            pattern = "|".join(f"({re.escape(k)})" for k in keywords)
            self.pattern = re.compile(pattern, re.IGNORECASE)
        else:
            self.pattern = None
            
        # Compile progress patterns (always capture these as INFO)
        progress_regexes = config.get("progress_regexes", [])
        self.progress_patterns = [re.compile(p, re.IGNORECASE) for p in progress_regexes]
        
        self.observer: Optional[Observer] = None
        self.file_position = 0
        self._lock = threading.Lock()
    
    def start(self):
        """Start monitoring the file."""
        if self._running:
            return
        
        self._running = True
        
        # Get initial file position (start from end)
        with open(self.file_path, "r") as f:
            f.seek(0, 2)  # Seek to end
            self.file_position = f.tell()
        
        # Set up file watcher
        event_handler = FileChangeHandler(self)
        self.observer = Observer()
        self.observer.schedule(
            event_handler,
            str(self.file_path.parent),
            recursive=False
        )
        self.observer.start()
        
        # Start polling thread as fallback
        self._polling_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._polling_thread.start()
    
    def stop(self):
        """Stop monitoring."""
        if not self._running:
            return
        
        self._running = False
        
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
    
    # Built-in completion phrases (always scanned, no config needed)
    COMPLETION_PHRASES = [
        'completed successfully', 'finished successfully',
        'process complete', 'job finished', 'all tasks done',
        'exiting normally', 'shutdown complete', 'run finished',
        'execution complete', 'task completed', 'work done',
        'graceful shutdown', 'exiting with code 0',
    ]

    def process_new_lines(self):
        """Process new lines added to file."""
        with self._lock:
            try:
                with open(self.file_path, "r") as f:
                    f.seek(self.file_position)
                    new_lines = f.readlines()
                    self.file_position = f.tell()
                
                # Process each line
                for line in new_lines:
                    line = line.rstrip()
                    if not line:
                        continue
                    
                    matched = False
                    # Check if matches main pattern
                    if self.pattern:
                        if self.pattern.search(line):
                            severity = self._classify_severity(line)
                            self._emit_event(line, severity)
                            matched = True
                    else:
                        # Emit all lines if no pattern
                        severity = self._classify_severity(line)
                        self._emit_event(line, severity)
                        matched = True
                        
                    # Check progress patterns if not already matched
                    if not matched:
                        for p in self.progress_patterns:
                            if p.search(line):
                                # Mark as progress event
                                self._emit_event(line, Severity.INFO, {"is_progress": True})
                                matched = True
                                break
                    
                    # Always check for built-in completion phrases (bypass keyword filter)
                    if not matched:
                        line_lower = line.lower()
                        if any(phrase in line_lower for phrase in self.COMPLETION_PHRASES):
                            self._emit_event(line, Severity.INFO, {"is_completion": True})
            
            except Exception as e:
                self._emit_event(
                    f"Error reading file: {e}",
                    Severity.WARNING,
                    {"error": str(e)}
                )
    
    def _poll_loop(self):
        """Poll file for changes as fallback."""
        while self._running:
            time.sleep(1.0)
            try:
                if self.file_path.exists():
                    current_size = self.file_path.stat().st_size
                    if current_size > self.file_position:
                        self.process_new_lines()
            except Exception:
                pass

    def _classify_severity(self, line: str) -> Severity:
        """Classify line severity based on content.
        
        Args:
            line: Log line.
            
        Returns:
            Severity level.
        """
        line_upper = line.upper()
        
        if any(word in line_upper for word in ["FATAL", "CRITICAL", "SEGFAULT", "PANIC"]):
            return Severity.CRITICAL
        elif any(word in line_upper for word in ["ERROR", "EXCEPTION", "FAILED", "TRACEBACK"]):
            return Severity.WARNING
        else:
            return Severity.INFO


class FileChangeHandler(FileSystemEventHandler):
    """Handle file system events."""
    
    def __init__(self, file_monitor: FileMonitor):
        """Initialize handler.
        
        Args:
            file_monitor: Parent file monitor.
        """
        self.file_monitor = file_monitor
    
    def on_modified(self, event):
        """Handle file modification event.
        
        Args:
            event: File system event.
        """
        if event.is_directory:
            return
        
        # Check if it's our file
        if Path(event.src_path) == self.file_monitor.file_path:
            self.file_monitor.process_new_lines()
