"""Telegram message listener for two-way communication."""

import time
import logging
import threading
from typing import Optional, Callable
import asyncio
from telegram import Bot, Update
from telegram.error import TelegramError, TimedOut, NetworkError

logger = logging.getLogger(__name__)


class TelegramListener:
    """Listens for user messages on Telegram and triggers callbacks."""
    
    def __init__(self, bot_token: str, chat_id: str):
        """Initialize listener.
        
        Args:
            bot_token: Telegram bot token.
            chat_id: Chat ID to listen to.
        """
        self.token = bot_token
        self.chat_id = chat_id
        self.last_update_id: Optional[int] = None
        self.running = False
        self.on_message_callback: Optional[Callable[[str], None]] = None
        self._lock = threading.Lock()
        self.backoff_time = 0.0
        self._initialized = False  # Track if we've drained stale updates
        
        logger.info(f"TelegramListener initialized for chat {chat_id}")

    def _drain_stale_updates(self):
        """Drain any pending updates from Telegram to avoid Conflict on startup."""
        try:
            async def _drain():
                async with Bot(token=self.token) as bot:
                    updates = await bot.get_updates(offset=-1, timeout=1)
                    if updates:
                        return updates[-1].update_id
                    return None
            
            last_id = asyncio.run(_drain())
            if last_id is not None:
                self.last_update_id = last_id
                logger.info(f"Drained stale updates, offset set to {last_id + 1}")
        except Exception as e:
            logger.debug(f"Drain attempt: {e}")

    def set_message_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback function to call when message is received.
        
        Args:
            callback: Function to call with message text.
        """
        self.on_message_callback = callback
    
    def poll_once(self) -> bool:
        """Poll for new messages once.
        
        Returns:
            True if a message was received.
        """
        if not self._lock.acquire(blocking=False):
            # Already polling elsewhere
            return False
            
        try:
            # First call: drain stale updates to prevent Conflict
            if not self._initialized:
                self._initialized = True
                self._drain_stale_updates()
                time.sleep(1)  # Brief pause after drain

            # Respect backoff
            if self.backoff_time > 0:
                time.sleep(min(self.backoff_time, 30))
                self.backoff_time = 0  # Reset after sleeping once

            async def _get_updates():
                async with Bot(token=self.token) as bot:
                    return await bot.get_updates(
                        offset=self.last_update_id + 1 if self.last_update_id else None,
                        timeout=5,  # Short timeout to reduce Conflict window
                        allowed_updates=["message"]
                    )

            # Get updates
            updates = asyncio.run(_get_updates())
            
            message_received = False
            
            for update in updates:
                self.last_update_id = update.update_id
                
                # Check if it's a message from our chat
                if update.message and str(update.message.chat.id) == str(self.chat_id):
                    message_text = update.message.text or ""
                    
                    logger.info(f"Received message: {message_text[:50]}...")
                    
                    # Detect command
                    is_command = message_text.startswith("/")
                    command_name = None
                    command_args = []
                    
                    if is_command:
                        parts = message_text[1:].split()
                        if parts:
                            command_name = parts[0].lower()
                            command_args = parts[1:]
                    
                    # Trigger callback
                    if self.on_message_callback:
                        # Pass text, is_command, name, and args
                        self.on_message_callback(message_text, is_command, command_name, command_args)
                    
                    message_received = True
            
            # Successful poll - reset backoff
            self.backoff_time = 0
            return message_received
        
        except (asyncio.TimeoutError, TimedOut):
            # Normal timeout for long polling, just return False
            return False
        except NetworkError as e:
            logger.warning(f"Telegram network error: {e}")
            return False
        except TelegramError as e:
            if "Conflict" in str(e):
                self.backoff_time = min(30, max(3.0, self.backoff_time * 2 if self.backoff_time > 0 else 3.0))
                logger.warning(f"Telegram Conflict (backoff {self.backoff_time:.0f}s)")
            else:
                logger.error(f"Telegram API error: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error in polling: {e}")
            return False
        finally:
            if self._lock.locked():
                self._lock.release()
    
    def start_polling(self, interval: float = 2.0) -> None:
        """Start continuous polling (blocking).
        
        Args:
            interval: Seconds between polls.
        """
        self.running = True
        logger.info("Started message polling")
        
        while self.running:
            try:
                self.poll_once()
                time.sleep(interval)
            except KeyboardInterrupt:
                logger.info("Polling stopped by user")
                break
            except Exception as e:
                logger.error(f"Polling loop error: {e}")
                time.sleep(interval)
    
    def stop_polling(self) -> None:
        """Stop polling."""
        self.running = False
        logger.info("Polling stopped")
