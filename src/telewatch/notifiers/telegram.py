"""Telegram notification system."""

import time
import html
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from telegram import Bot
from telegram.error import TelegramError

from ..monitors.base import Severity
from ..analyzers.event_analyzer import Analysis


class TelegramNotifier:
    """Send notifications via Telegram."""
    
    SEVERITY_EMOJI = {
        Severity.CRITICAL: "🔴",
        Severity.WARNING: "🟡",
        Severity.INFO: "🟢",
    }
    
    def __init__(self, bot_token: str, chat_id: str, 
                 rate_limit_per_hour: int = 50):
        """Initialize notifier.
        
        Args:
            bot_token: Telegram bot token.
            chat_id: Chat ID to send messages to.
            rate_limit_per_hour: Maximum messages per hour.
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.rate_limit = rate_limit_per_hour
        
        # Track message timestamps for rate limiting
        self.message_times: deque = deque()
    
    def send_analysis(self, analysis: Analysis) -> bool:
        """Send analysis as notification.
        
        Args:
            analysis: Analysis to send.
            
        Returns:
            True if sent successfully.
        """
        # Check rate limit
        if not self._check_rate_limit():
            print(f"Rate limit exceeded, skipping notification")
            return False
        
        # Format message
        message = self._format_message(analysis)
        
        # Send
        import asyncio
        try:
            async def _send():
                async with Bot(self.bot_token) as bot:
                    await bot.send_message(
                        chat_id=self.chat_id,
                        text=message,
                        parse_mode="HTML",
                        read_timeout=30,
                    )
            
            asyncio.run(_send())
            
            # Record send time
            self.message_times.append(datetime.now())
            return True
        
        except Exception as e:
            print(f"Failed to send Telegram message: {e}")
            return False
    
    def send_test_message(self) -> bool:
        """Send a test message.
        
        Returns:
            True if sent successfully.
        """
        import asyncio
        try:
            async def _send_test():
                async with Bot(self.bot_token) as bot:
                    await bot.send_message(
                        chat_id=self.chat_id,
                        text="🤖 <b>TeleWatch Test</b>\n\nYour monitoring system is configured correctly!",
                        parse_mode="HTML",
                    )

            asyncio.run(_send_test())
            return True
        except Exception as e:
            print(f"Test message failed: {e}")
            return False
    
    def send_message(self, text: str) -> bool:
        """Send a generic text message.
        
        Args:
            text: Message text.
            
        Returns:
            True if sent successfully.
        """
        import asyncio
        try:
            async def _send():
                async with Bot(self.bot_token) as bot:
                    await bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode="HTML",
                    )

            asyncio.run(_send())
            return True
        except Exception as e:
            print(f"Failed to send message: {e}")
            return False

    def _check_rate_limit(self) -> bool:
        """Check if we can send another message.
        
        Returns:
            True if under rate limit.
        """
        now = datetime.now()
        cutoff = now - timedelta(hours=1)
        
        # Remove old timestamps
        while self.message_times and self.message_times[0] < cutoff:
            self.message_times.popleft()
        
        # Check if under limit
        return len(self.message_times) < self.rate_limit
    
    def _format_message(self, analysis: Analysis) -> str:
        """Format analysis as Telegram message.
        
        Args:
            analysis: Analysis to format.
            
        Returns:
            Formatted message (HTML mode).
        """
        emoji = self.SEVERITY_EMOJI.get(analysis.severity, "⚪")
        severity_text = analysis.severity.value.upper()
        
        event = analysis.original_event
        
        # Build message
        summary = html.escape(analysis.summary)
        lines = [
            f"{emoji} <b>{severity_text}: {summary}</b>",
            "",
            f"<b>Source:</b> {html.escape(event.source)}",
            f"<b>Time:</b> {event.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
        ]
        
        # Add analysis
        if analysis.root_cause and analysis.root_cause != "Unknown":
            root_cause = html.escape(analysis.root_cause)
            lines.append(f"<b>Root Cause:</b>\n{root_cause}")
            lines.append("")
        
        # Add original content (truncated)
        content = event.content[:300]
        if len(event.content) > 300:
            content += "..."
        
        escaped_content = html.escape(content)
        lines.append(f"<b>Event:</b>\n<pre>{escaped_content}</pre>")
        lines.append("")
        
        # Add suggested action
        if analysis.suggested_action:
            action = html.escape(analysis.suggested_action)
            lines.append(f"<b>Action:</b> {action}")
        
        return "\n".join(lines)
