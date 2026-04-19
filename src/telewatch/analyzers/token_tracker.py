"""Token usage tracking and statistics."""

import time
from typing import Dict, Any
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TokenStats:
    """Token usage statistics for a time period."""
    requests: int = 0
    tokens_sent: int = 0
    tokens_received: int = 0
    llm_calls: int = 0
    cached_calls: int = 0
    pattern_matched_calls: int = 0
    start_time: float = field(default_factory=time.time)
    
    def get_cache_hit_rate(self) -> float:
        """Calculate cache hit rate percentage."""
        total = self.cached_calls + self.llm_calls
        if total == 0:
            return 0.0
        return (self.cached_calls / total) * 100
    
    def get_pattern_match_rate(self) -> float:
        """Calculate pattern match rate percentage."""
        total = self.pattern_matched_calls + self.llm_calls
        if total == 0:
            return 0.0
        return (self.pattern_matched_calls / total) * 100


class TokenUsageTracker:
    """Track LLM token usage and optimization statistics."""
    
    def __init__(self):
        """Initialize tracker."""
        self.current = TokenStats()
        self.hourly = TokenStats()
        self.daily = TokenStats()
        self.last_hourly_reset = time.time()
        self.last_daily_reset = time.time()
    
    def record_llm_call(self, tokens_sent: int, tokens_received: int) -> None:
        """Record an LLM API call.
        
        Args:
            tokens_sent: Number of tokens in prompt.
            tokens_received: Number of tokens in response.
        """
        for stats in [self.current, self.hourly, self.daily]:
            stats.requests += 1
            stats.llm_calls += 1
            stats.tokens_sent += tokens_sent
            stats.tokens_received += tokens_received
        
        self._check_resets()
    
    def record_cache_hit(self) -> None:
        """Record a cache hit (LLM call avoided)."""
        for stats in [self.current, self.hourly, self.daily]:
            stats.requests += 1
            stats.cached_calls += 1
        
        self._check_resets()
    
    def record_pattern_match(self) -> None:
        """Record a pattern match (LLM call avoided)."""
        for stats in [self.current, self.hourly, self.daily]:
            stats.requests += 1
            stats.pattern_matched_calls += 1
        
        self._check_resets()
    
    def _check_resets(self) -> None:
        """Reset hourly/daily stats if time period elapsed."""
        current_time = time.time()
        
        # Reset hourly stats
        if current_time - self.last_hourly_reset > 3600:
            self.hourly = TokenStats()
            self.last_hourly_reset = current_time
        
        # Reset daily stats
        if current_time - self.last_daily_reset > 86400:
            self.daily = TokenStats()
            self.last_daily_reset = current_time
    
    def get_stats(self, period: str = "current") -> Dict[str, Any]:
        """Get statistics for a time period.
        
        Args:
            period: 'current', 'hourly', or 'daily'.
            
        Returns:
            Statistics dictionary.
        """
        if period == "hourly":
            stats = self.hourly
        elif period == "daily":
            stats = self.daily
        else:
            stats = self.current
        
        total_tokens = stats.tokens_sent + stats.tokens_received
        
        return {
            "period": period,
            "total_requests": stats.requests,
            "llm_calls": stats.llm_calls,
            "cached_calls": stats.cached_calls,
            "pattern_matched": stats.pattern_matched_calls,
            "tokens_sent": stats.tokens_sent,
            "tokens_received": stats.tokens_received,
            "total_tokens": total_tokens,
            "cache_hit_rate": round(stats.get_cache_hit_rate(), 1),
            "pattern_match_rate": round(stats.get_pattern_match_rate(), 1),
            "uptime_seconds": int(time.time() - stats.start_time)
        }
    
    def get_summary(self, period: str = "current") -> str:
        """Get formatted summary string.
        
        Args:
            period: 'current', 'hourly', or 'daily'.
            
        Returns:
            Formatted summary.
        """
        stats = self.get_stats(period)
        
        lines = [
            f"📊 **Token Usage ({period.capitalize()})**",
            f"• Total Requests: {stats['total_requests']}",
            f"• LLM Calls: {stats['llm_calls']}",
            f"• Cache Hits: {stats['cached_calls']} ({stats['cache_hit_rate']}%)",
            f"• Pattern Matches: {stats['pattern_matched']} ({stats['pattern_match_rate']}%)",
            f"• Tokens: {stats['tokens_sent']:,} sent / {stats['tokens_received']:,} received",
        ]
        
        return "\n".join(lines)
    
    def reset_current(self) -> None:
        """Reset current session stats."""
        self.current = TokenStats()
