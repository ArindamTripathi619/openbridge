import json
import os
import time
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

@dataclass
class BotState:
    """Represents the current state of the bot."""
    pid: int
    start_time: float
    status: str  # "running", "paused", "stalled", "error"
    process_name: str
    progress: float
    message: str
    last_update: float
    # Behavioral metrics
    log_frequency: float = 0.0
    known_structures: int = 0
    is_stalled: bool = False
    
    @classmethod
    def empty(cls) -> 'BotState':
        return cls(
            pid=0,
            start_time=0.0,
            status="stopped",
            process_name="",
            progress=0.0,
            message="Not running",
            last_update=0.0,
            log_frequency=0.0,
            known_structures=0,
            is_stalled=False
        )

class StateManager:
    """Manages persistence of bot state and PID file."""
    
    def __init__(self, app_name: str = "telewatch"):
        self.config_dir = Path.home() / f".{app_name}"
        self.state_file = self.config_dir / "state.json"
        self.pid_file = self.config_dir / "daemon.pid"
        self.log_file = self.config_dir / "telewatch.log"
        
        # Ensure directory exists
        self.config_dir.mkdir(parents=True, exist_ok=True)
        
    def save_state(self, state: BotState):
        """Save current state to JSON file."""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(asdict(state), f, indent=2)
        except Exception as e:
            print(f"Warning: Failed to save state: {e}")

    def load_state(self) -> BotState:
        """Load state from JSON file."""
        if not self.state_file.exists():
            return BotState.empty()
            
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
                return BotState(**data)
        except Exception:
            return BotState.empty()

    def update_status(self, progress: float, message: str, status: str = "running", 
                      frequency: float = 0.0, structures: int = 0, stalled: bool = False):
        """Update specific fields in the state."""
        current = self.load_state()
        
        current.progress = progress
        current.message = message
        current.status = status
        current.log_frequency = frequency
        current.known_structures = structures
        current.is_stalled = stalled
        current.last_update = time.time()
        
        # If we just started, PID might not be set in the loaded state 
        # if we are initializing.
        if os.getpid() == current.pid or current.pid == 0:
             # Only update if we own the lock or it's stale
             self.save_state(current)

    def set_running(self, process_name: str):
        """Mark as running with current PID."""
        state = BotState(
            pid=os.getpid(),
            start_time=time.time(),
            status="running",
            process_name=process_name,
            progress=0.0,
            message="Started",
            last_update=time.time()
        )
        self.save_state(state)
        self.write_pid()

    def set_stopped(self):
        """Mark as stopped."""
        state = self.load_state()
        state.status = "stopped"
        state.pid = 0
        state.message = "Stopped"
        self.save_state(state)
        self.remove_pid()

    def write_pid(self):
        """Write current PID to lock file."""
        with open(self.pid_file, 'w') as f:
            f.write(str(os.getpid()))

    def remove_pid(self):
        """Remove PID file."""
        if self.pid_file.exists():
            self.pid_file.unlink()

    def get_daemon_pid(self) -> Optional[int]:
        """Get PID of running daemon if it exists."""
        if not self.pid_file.exists():
            return None
        try:
            with open(self.pid_file, 'r') as f:
                pid = int(f.read().strip())
            
            # Verify process still exists
            try:
                os.kill(pid, 0)
                return pid
            except OSError:
                # Process is dead
                return None
        except ValueError:
            return None

    def is_running(self) -> bool:
        """Check if daemon is currently running."""
        return self.get_daemon_pid() is not None
