"""Event analysis logic."""

import json
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from collections import deque

from ..monitors.base import MonitorEvent, Severity
from .llm_client import BaseLLMClient, LLMError


@dataclass
class Analysis:
    """Analysis result."""
    severity: Severity
    summary: str
    root_cause: str
    suggested_action: str
    original_event: MonitorEvent


class EventAnalyzer:
    """Analyzes events using LLM with optimizations."""
    
    def __init__(self, llm_client: BaseLLMClient, context_size: int = 10, 
                 optimization_config: Dict[str, Any] = None,
                 token_tracker = None,
                 turbo: bool = False):
        """Initialize analyzer.
        
        Args:
            llm_client: LLM client.
            context_size: Number of previous events to include for context.
            optimization_config: Optimization settings dictionary.
            optimization_config: Optimization settings dictionary.
            token_tracker: Shared token usage tracker.
            turbo: Enable turbo mode (disable profiling & novelty detection).
        """
        self.turbo = turbo
        self.llm_client = llm_client
        self.context_size = context_size
        # Limit history to 50 events to prevent memory growth
        self.event_history: deque = deque(maxlen=50)
        
        # Load optimization config
        from .analysis_cache import AnalysisCache
        from .pattern_matcher import SeverityPatternMatcher, get_default_patterns
        from .token_tracker import TokenUsageTracker
        from .context_optimizer import trim_context, estimate_tokens, strip_timestamp
        from .log_profiler import LogProfiler
        from .anomaly_detector import AnomalyDetector
        
        opt_config = optimization_config or {}
        
        # Initialize cache if enabled
        self.cache = None
        if opt_config.get('enable_cache', True):
            self.cache = AnalysisCache(
                max_entries=opt_config.get('cache_max_entries', 100),
                ttl_seconds=opt_config.get('cache_ttl_seconds', 3600)
            )
        
        # Initialize pattern matcher if enabled
        self.pattern_matcher = None
        if opt_config.get('use_local_patterns', True):
            patterns = opt_config.get('severity_patterns') or get_default_patterns()
            self.pattern_matcher = SeverityPatternMatcher(patterns)
        
        # Initialize log profiler (disabled in turbo mode)
        self.profiler = None
        if not self.turbo:
            self.profiler = LogProfiler(sample_limit=opt_config.get('profiler_limit', 50))
        
        # Initialize anomaly detector (disabled in turbo mode)
        # Note: 'config' object is not available here, so we use default empty config
        # The calling code (MonitorManager) should pass specific config if needed, 
        # but for now we follow existing pattern but fix the NameError
        self.anomaly_detector = None
        if not self.turbo:
             # Use empty config or pass it down if we change signature. 
             # For now, safely handle the missing config object
            self.anomaly_detector = AnomalyDetector(config={})
        
        # Token usage tracking
        self.token_tracker = token_tracker or TokenUsageTracker()
        
        # Context optimization settings
        self.max_context_lines = opt_config.get('max_context_lines', 15)
        self.include_timestamps = opt_config.get('include_timestamps', False)
        self.skip_llm_for_info = opt_config.get('skip_llm_for_info', True)
        
        # Store optimizer functions
        self.trim_context = trim_context
        self.estimate_tokens = estimate_tokens
        self.strip_timestamp = strip_timestamp
    
        self.pending_anomalies: List[Analysis] = []
        
    def analyze_event(self, event: MonitorEvent) -> Analysis:
        """Analyze an event with optimized LLM usage.
        
        Args:
            event: Event to analyze.
            
        Returns:
            Analysis result.
        """
        # 0. Background Profiling & Anomaly Detection (Skip in Turbo Mode)
        anomaly_results = {}
        if not self.turbo:
            if self.profiler:
                self.profiler.add_sample(event.content)
            if self.anomaly_detector:
                anomaly_results = self.anomaly_detector.add_event(event.content)
        
        # Collect spike anomalies
        for anomaly in anomaly_results.get("anomalies", []):
            if anomaly["type"] == "spike":
                spike_analysis = Analysis(
                    severity=Severity.WARNING,
                    summary="📈 LOG FREQUENCY SPIKE",
                    root_cause=anomaly["message"],
                    suggested_action="Check for crash loops or debug logging being enabled.",
                    original_event=MonitorEvent(
                        source="AnomalyDetector",
                        severity=Severity.WARNING,
                        content=anomaly["message"],
                        timestamp=datetime.now(),
                        metadata={"type": "spike"}
                    )
                )
                self.pending_anomalies.append(spike_analysis)
                with open("/home/DevCrewX/.telewatch/telewatch.log", "a") as f:
                    f.write(f"ANOMALY: {spike_analysis.summary} - {spike_analysis.root_cause}\n")
        
        # 1. Structural Novelty - Force LLM if novel and not INFO
        force_llm = anomaly_results.get("is_novel", False) and event.severity != Severity.INFO
        
        # 2. Check cache first (skip if novel to ensure fresh analysis)
        if self.cache and not force_llm:
            cached = self.cache.get(event)
            if cached is not None:
                self.token_tracker.record_cache_hit()
                return cached
        
        # 2. Try pattern matching
        if self.pattern_matcher:
            matched_severity = self.pattern_matcher.match(event.content)
            if matched_severity is not None:
                self.token_tracker.record_pattern_match()
                analysis = Analysis(
                    severity=matched_severity,
                    summary=f"Pattern-matched: {event.content[:100]}",
                    root_cause="Known pattern detected",
                    suggested_action="Check logs for details" if matched_severity != Severity.INFO else "Normal operation",
                    original_event=event
                )
                
                # Cache pattern-matched result
                if self.cache:
                    self.cache.put(event, analysis)
                
                return analysis
        
        # 3. Skip LLM for INFO if configured
        if self.skip_llm_for_info and event.severity == Severity.INFO:
            self.token_tracker.record_pattern_match() # Record as pattern match for simplicity
            analysis = Analysis(
                severity=Severity.INFO,
                summary=event.content[:100],
                root_cause="INFO level event",
                suggested_action="No action required",
                original_event=event
            )
            if self.cache:
                self.cache.put(event, analysis)
            return analysis
        
        # 4. Use LLM analysis with optimized context
        analysis = self._llm_analysis(event, is_novel=force_llm)
        
        # Cache LLM result
        if self.cache:
            self.cache.put(event, analysis)
            
        return analysis
    
    def _llm_analysis(self, event: MonitorEvent, is_novel: bool = False) -> Analysis:
        """Perform LLM-based analysis."""
        # Build context from history
        history_list = list(self.event_history)
        context_events = history_list[-self.context_size:] if history_list else []
        
        # Create prompt
        prompt = self._build_prompt(event, context_events, is_novel=is_novel)
        
        try:
            # Get LLM analysis
            response = self.llm_client.analyze(prompt)
            
            # Track token usage
            tokens_sent = self.estimate_tokens(prompt)
            tokens_received = self.estimate_tokens(response)
            self.token_tracker.record_llm_call(tokens_sent, tokens_received)
            
            # Parse response
            analysis = self._parse_response(response, event)
            
            # Add to history
            self.event_history.append(event)
            
            return analysis
        
        except LLMError as e:
            error_str = str(e)
            
            # Check for quota exhaustion
            if "QUOTA_EXHAUSTED" in error_str:
                return Analysis(
                    severity=Severity.CRITICAL,
                    summary="⚠️ LLM API QUOTA EXHAUSTED",
                    root_cause="Your LLM API quota/credits have been exhausted. Analysis is temporarily disabled.",
                    suggested_action="Update your API key in config: ~/.config/telewatch/config.yaml, then restart telewatch",
                    original_event=event
                )
            
            # Check for auth errors
            elif "AUTH_ERROR" in error_str:
                return Analysis(
                    severity=Severity.CRITICAL,
                    summary="⚠️ LLM API AUTHENTICATION FAILED",
                    root_cause="Invalid or expired API key.",
                    suggested_action="Update your API key in config: ~/.config/telewatch/config.yaml, then restart telewatch",
                    original_event=event
                )
            
            # Generic LLM failure - use basic analysis
            else:
                return Analysis(
                    severity=event.severity,
                    summary=event.content[:100],
                    root_cause=f"LLM analysis failed: {e}",
                    suggested_action="Check LLM configuration or use basic severity classification",
                    original_event=event
                )
    
    def _build_prompt(self, event: MonitorEvent, context: List[MonitorEvent], is_novel: bool = False) -> str:
        """Build optimized analysis prompt.
        
        Args:
            event: Current event.
            context: Previous events for context.
            is_novel: Whether this is a novel structural event.
            
        Returns:
            Formatted prompt with trimmed context.
        """
        # Optimize event content
        optimized_content = self.trim_context(
            event.content,
            max_lines=self.max_context_lines,
            include_timestamps=self.include_timestamps
        )
        
        prompt = """You are analyzing logs from a monitoring system. Based on the information below, provide a structured analysis.

**Your task:**
1. Assess the severity (CRITICAL, WARNING, or INFO)
2. Identify the root cause if it's an error
3. Suggest a specific action to take
4. Provide a one-line summary
5. BOOTSTRAP MODE: If this is a new type of error, generate a single regex pattern that would match this error and similar ones in the future (avoiding too specific values like timestamps or unique IDs).

**Recent Context (previous events):**
"""
        
        if context:
            for ctx_event in context[-3:]:  # Only last 3 for brevity
                ctx_content = self.trim_context(ctx_event.content, max_lines=3, include_timestamps=False)
                prompt += f"[{ctx_event.source}]: {ctx_content[:150]}\n"
        else:
            prompt += "(No previous context)\n"
        
        prompt += f"""
**Current Event:**
Source: {event.source}
Content:
{optimized_content}

**Required Response Format (JSON):**
{{
  "severity": "CRITICAL|WARNING|INFO",
  "summary": "One-line description",
  "root_cause": "What caused this (if error)",
  "suggested_action": "What to do next",
  "recommended_regex": "A regex string to match this type of event locally"
}}

Respond ONLY with valid JSON.
"""
        if is_novel:
            prompt = "NOVEL LOG STRUCTURE DETECTED. " + prompt
            
        return prompt
    
    def _parse_response(self, response: str, event: MonitorEvent) -> Analysis:
        """Parse LLM response.
        
        Args:
            response: LLM response text.
            event: Original event.
            
        Returns:
            Parsed analysis.
        """
        try:
            # Try to parse as JSON
            # Extract JSON from markdown code blocks if present
            response = response.strip()
            if "```json" in response:
                start = response.find("```json") + 7
                end = response.find("```", start)
                response = response[start:end].strip()
            elif "```" in response:
                start = response.find("```") + 3
                end = response.find("```", start)
                response = response[start:end].strip()
            
            data = json.loads(response)
            
            # Map severity
            severity_map = {
                "CRITICAL": Severity.CRITICAL,
                "WARNING": Severity.WARNING,
                "INFO": Severity.INFO,
            }
            severity = severity_map.get(data.get("severity", "INFO").upper(), Severity.INFO)
            
            analysis = Analysis(
                severity=severity,
                summary=data.get("summary", "Event detected")[:200],
                root_cause=data.get("root_cause", "Unknown")[:300],
                suggested_action=data.get("suggested_action", "Monitor")[:300],
                original_event=event
            )
            
            # 5. Inject Recommended Regex if provided
            regex = data.get("recommended_regex")
            if regex and self.pattern_matcher:
                self.pattern_matcher.add_dynamic_pattern(regex, severity)
                
            return analysis
        
        except (json.JSONDecodeError, KeyError, ValueError):
            # Fallback parsing if JSON fails
            return Analysis(
                severity=event.severity,
                summary=response[:100] if response else event.content[:100],
                root_cause="Unable to parse LLM response",
                suggested_action="Review original event",
                original_event=event
            )
    
    def get_token_stats(self, period: str = "current") -> Dict[str, Any]:
        """Get token usage statistics.
        
        Args:
            period: 'current', 'hourly', or 'daily'.
            
        Returns:
            Statistics dictionary.
        """
        stats = self.token_tracker.get_stats(period)
        
        # Add cache stats if available
        if self.cache:
            cache_stats = self.cache.get_stats()
            stats['cache_stats'] = cache_stats
        
        # Add profiler progress
        if self.profiler:
            stats['profiler_progress'] = len(self.profiler.samples) / self.profiler.sample_limit
            
        # Add dynamic patterns count
        if self.pattern_matcher:
            dynamic_count = sum(len(p) for p in self.pattern_matcher.dynamic_patterns.values())
            stats['dynamic_patterns'] = dynamic_count
            
        # Add anomaly stats
        if self.anomaly_detector:
            stats['anomaly_stats'] = self.anomaly_detector.get_stats()
            
        return stats
    
    def check_stall(self) -> Optional[Analysis]:
        """Check for log stream stall."""
        if not self.anomaly_detector:
            return None
            
        anomaly = self.anomaly_detector.check_stall()
        if anomaly:
            analysis = Analysis(
                severity=Severity.CRITICAL,
                summary="⚠️ LOG STREAM STALLED",
                root_cause=anomaly["message"],
                suggested_action="Check if the monitored process is still running or if log rotation broke the stream.",
                original_event=MonitorEvent(
                    source="AnomalyDetector",
                    severity=Severity.CRITICAL,
                    content=anomaly["message"],
                    timestamp=datetime.now(),
                    metadata={"type": "stall"}
                )
            )
            with open("/home/DevCrewX/.telewatch/telewatch.log", "a") as f:
                f.write(f"ANOMALY: {analysis.summary} - {analysis.root_cause}\n")
            return analysis
        return None
    
    def get_stats_summary(self, period: str = "current") -> str:
        """Get formatted statistics summary.
        
        Args:
            period: 'current', 'hourly', or 'daily'.
            
        Returns:
            Formatted summary string.
        """
        return self.token_tracker.get_summary(period)
