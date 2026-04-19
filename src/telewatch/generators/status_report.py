"""Status report generation."""

from typing import List, Optional
from datetime import datetime

from ..tracker import ProgressTracker
from ..analyzers import BaseLLMClient
from ..analyzers.context_optimizer import estimate_tokens


class StatusReportGenerator:
    """Generates formatted status reports for Telegram."""
    
    def __init__(self, llm_client: Optional[BaseLLMClient] = None, token_tracker=None):
        """Initialize generator.
        
        Args:
            llm_client: Optional LLM client for generating summaries.
            token_tracker: Optional shared token tracker.
        """
        self.llm_client = llm_client
        self.token_tracker = token_tracker
    
    def generate_report(self, tracker: ProgressTracker, include_llm_summary: bool = True) -> str:
        """Generate a status report.
        
        Args:
            tracker: Progress tracker instance.
            include_llm_summary: Whether to include LLM-generated summary.
            
        Returns:
            Formatted status report.
        """
        lines = []
        
        # Header
        lines.append(f"📊 **Status Report: {tracker.process_name}**")
        lines.append("━" * 40)
        
        # Progress bar and percentage
        progress_bar = tracker.get_progress_bar(10)
        lines.append(f"**Progress:** {tracker.current_percentage:.1f}% {progress_bar}")
        
        # Time info
        elapsed = tracker.get_elapsed_time()
        lines.append(f"**Elapsed:** {elapsed}")
        
        remaining = tracker.get_estimated_remaining()
        if remaining:
            lines.append(f"**Estimated Remaining:** {remaining}")
        
        # Check for stall
        if tracker.is_stalled():
            lines.append("")
            lines.append("⚠️ **Warning:** Process appears stalled")
        
        lines.append("")
        
        # Recent activity
        lines.append("🔍 **Recent Activity:**")
        activity = tracker.get_recent_activity(5)
        for item in activity:
            lines.append(item)
        
        # LLM Summary
        if include_llm_summary and self.llm_client and tracker.recent_logs:
            lines.append("")
            lines.append("💡 **Summary:**")
            
            try:
                summary = self._generate_llm_summary(tracker)
                if summary:
                    lines.append(summary)
                else:
                    lines.append("_Analysis unavailable_")
            except Exception as e:
                lines.append(f"_Analysis failed: {e}_")
        
        # Footer
        lines.append("")
        lines.append(f"_Report generated at {datetime.now().strftime('%H:%M:%S')}_")
        
        return "\n".join(lines)
    
    def generate_report_with_stats(self, tracker: ProgressTracker, analyzer, include_llm_summary: bool = True) -> str:
        """Generate status report with token usage stats.
        
        Args:
            tracker: Progress tracker instance.
            analyzer: EventAnalyzer with token stats.
            include_llm_summary: Whether to include LLM summary.
            
        Returns:
            Formatted report with stats.
        """
        report = self.generate_report(tracker, include_llm_summary)
        
        # Add token usage stats
        if hasattr(analyzer, 'get_stats_summary'):
            stats_summary = analyzer.get_stats_summary('current')
            report += f"\n\n{stats_summary}"
        
        return report
    
    def _generate_llm_summary(self, tracker: ProgressTracker) -> Optional[str]:
        """Generate LLM summary of current status.
        
        Args:
            tracker: Progress tracker.
            
        Returns:
            Summary text or None.
        """
        if not self.llm_client:
            return None
        
        # Build context from recent logs
        recent_context = "\n".join(tracker.recent_logs[-30:])
        
        prompt = f"""Based on these recent logs from "{tracker.process_name}", provide a brief 2-3 sentence summary of what the process is currently doing and its status.

Process Description: {tracker.description}
Current Progress: {tracker.current_percentage:.1f}%

Recent Logs:
{recent_context}

Provide a concise, informative summary for a status report."""
        
        try:
            summary = self.llm_client.analyze(prompt)
            
            # Track tokens if tracker available
            if self.token_tracker:
                tokens_sent = estimate_tokens(prompt)
                tokens_received = estimate_tokens(summary)
                self.token_tracker.record_llm_call(tokens_sent, tokens_received)
            
            # Clean up the summary
            summary = summary.strip()
            if len(summary) > 1000:
                summary = summary[:997] + "..."
            return summary
        except Exception as e:
            return f"Error generating summary: {e}"
    
    def generate_milestone_report(self, tracker: ProgressTracker, milestone: int, include_llm_summary: bool = True) -> str:
        """Generate a milestone update report.
        
        Args:
            tracker: Progress tracker.
            milestone: Milestone percentage reached.
            include_llm_summary: Whether to include LLM summary.
            
        Returns:
            Formatted milestone report.
        """
        lines = []
        
        # Milestone header
        emoji = "🎯" if milestone < 100 else "🎉"
        lines.append(f"{emoji} **Milestone: {milestone}% Complete**")
        lines.append("")
        lines.append(f"**Process:** {tracker.process_name}")
        
        # Progress visualization
        progress_bar = tracker.get_progress_bar(10)
        lines.append(f"{progress_bar}")
        lines.append("")
        
        # Time stats
        elapsed = tracker.get_elapsed_time()
        lines.append(f"⏱️ **Elapsed:** {elapsed}")
        
        remaining = tracker.get_estimated_remaining()
        if remaining:
            lines.append(f"⏳ **Remaining:** ~{remaining}")
        
        # Recent activity snippet
        if tracker.recent_logs:
            lines.append("")
            lines.append("📝 **Latest:**")
            latest = tracker.recent_logs[-1].strip()
            if len(latest) > 150:
                latest = latest[:147] + "..."
            lines.append(f"`{latest}`")
            
        # LLM Summary (Optional)
        if include_llm_summary and self.llm_client and tracker.recent_logs:
            lines.append("")
            lines.append("💡 **Analysis:**")
            try:
                summary = self._generate_llm_summary(tracker)
                if summary:
                    lines.append(summary)
            except Exception as e:
                pass
        
        return "\n".join(lines)
    
    def generate_completion_report(self, tracker: ProgressTracker) -> str:
        """Generate process completion report.
        
        Args:
            tracker: Progress tracker.
            
        Returns:
            Formatted completion report.
        """
        lines = []
        
        lines.append("🎉 **Process Complete!**")
        lines.append("━" * 40)
        lines.append(f"**Process:** {tracker.process_name}")
        lines.append("")
        lines.append("✅ **Status:** COMPLETED")
        
        total_time = tracker.get_elapsed_time()
        lines.append(f"⏱️ **Total Time:** {total_time}")
        
        # Summary if LLM available
        if self.llm_client and tracker.recent_logs:
            lines.append("")
            try:
                summary = self._generate_llm_summary(tracker)
                if summary:
                    lines.append("📋 **Summary:**")
                    lines.append(summary)
            except:
                pass
        
        lines.append("")
        lines.append(f"_Completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
        
        return "\n".join(lines)
