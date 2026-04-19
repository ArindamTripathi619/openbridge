"""Base monitor interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from queue import Queue
from typing import Optional, Dict, Any


class Severity(Enum):
    """Event severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class MonitorEvent:
    """Event from a monitor."""
    timestamp: datetime
    source: str
    severity: Severity
    content: str
    metadata: Dict[str, Any]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "severity": self.severity.value,
            "content": self.content,
            "metadata": self.metadata,
        }


class BaseMonitor(ABC):
    """Abstract base class for monitors."""
    
    def __init__(self, name: str, config: Dict[str, Any]):
        """Initialize monitor.
        
        Args:
            name: Monitor name.
            config: Monitor configuration.
        """
        self.name = name
        self.config = config
        self.event_queue: Queue[MonitorEvent] = Queue()
        self._running = False
    
    @abstractmethod
    def start(self):
        """Start monitoring."""
        pass
    
    @abstractmethod
    def stop(self):
        """Stop monitoring."""
        pass
    
    def get_events(self) -> list[MonitorEvent]:
        """Get all pending events.
        
        Returns:
            List of events.
        """
        events = []
        while not self.event_queue.empty():
            events.append(self.event_queue.get())
        return events
    
    def is_running(self) -> bool:
        """Check if monitor is running."""
        return self._running
    
    def _emit_event(self, content: str, severity: Severity = Severity.INFO, 
                    metadata: Optional[Dict[str, Any]] = None):
        """Emit an event.
        
        Args:
            content: Event content.
            severity: Event severity.
            metadata: Additional metadata.
        """
        event = MonitorEvent(
            timestamp=datetime.now(),
            source=self.name,
            severity=severity,
            content=content,
            metadata=metadata or {}
        )
        self.event_queue.put(event)
