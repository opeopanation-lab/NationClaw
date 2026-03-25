"""
The Telegram chat client implementation.
"""
import asyncio
import re
import os
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from threading import Lock, Thread
import structlog

from mobileclaw.utils.interface import UniInterface
from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    from telegram import Update, BotCommand
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = None
    BotCommand = None
    Application = None
    CommandHandler = None
    MessageHandler = None
    filters = None
    ContextTypes = None


def _markdown_to_telegram_html(text: str) -> str:
    """
    Convert markdown to Telegram-safe HTML.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks
    code_blocks = []
    def save_code_block(m):
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r'```[\w]*\n?([\s\S]*?)```', save_code_block, text)

    # 2. Extract and protect inline code
    inline_codes = []
    def save_inline_code(m):
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r'`([^`]+)`', save_inline_code, text)

    # 3. Headers # Title -> just the title text
    text = re.sub(r'^#{1,6}\s+(.+)$', r'\1', text, flags=re.MULTILINE)

    # 4. Blockquotes > text -> just the text
    text = re.sub(r'^>\s*(.*)$', r'\1', text, flags=re.MULTILINE)

    # 5. Escape HTML special characters
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6. Links [text](url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)

    # 7. Bold **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 8. Italic _text_
    text = re.sub(r'(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])', r'<i>\1</i>', text)

    # 9. Strikethrough ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 10. Bullet lists - item -> • item
    text = re.sub(r'^[-*]\s+', '• ', text, flags=re.MULTILINE)

    # 11. Restore inline code with HTML tags
    for i, code in enumerate(inline_codes):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 12. Restore code blocks with HTML tags
    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


class Telegram_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.telegram'
        self.org_manager_user_id = self.agent.config.chat_telegram_org_manager
        self.telegram_username = None
        self.telegram_id = None
        self._serving_thread = None
        self._stop_serving = False
        self._app = None
        self._loop = None
        # Maintain mapping of sender_id to chat_id for replies
        self._chat_ids = {}  # {sender_id: chat_id}
        self._chat_history = defaultdict(lambda: deque(maxlen=20))
        self._history_lock = Lock()
        # Log receiver for send_to_log messages
        self.log_receiver = None  # Set via /log_here command
        # Report receiver for send_message when receiver is None
        self.report_receiver = None  # Set via /report_here command

    def _set_org_manager_if_missing(self, attr_name, config_name, sender_id):
        """Persist the first Telegram sender as org manager when not configured."""
        current_value = getattr(self, attr_name, None)
        if current_value not in (None, "", "?"):
            return

        setattr(self, attr_name, sender_id)
        if hasattr(self.agent.config, config_name):
            setattr(self.agent.config, config_name, sender_id)

        logger.info(f"Telegram org manager initialized to {sender_id}")

    def _remember_chat_id(self, sender_id, chat_id):
        """Cache chat_id for both raw numeric ID and username-qualified ID."""
        sender_id = str(sender_id)
        chat_id = str(chat_id)
        self._chat_ids[sender_id] = chat_id

        numeric_sender_id = sender_id.split('|', 1)[0]
        self._chat_ids[numeric_sender_id] = chat_id

    def _resolve_chat_id(self, receiver):
        """Resolve a receiver identifier to a known Telegram chat_id."""
        if receiver is None:
            return None

        receiver = str(receiver)
        chat_id = self._chat_ids.get(receiver)
        if chat_id:
            return chat_id

        numeric_receiver = receiver.split('|', 1)[0]
        return self._chat_ids.get(numeric_receiver)

    def _open(self):
        if not TELEGRAM_AVAILABLE:
            logger.error('Telegram SDK not installed. Run: pip install python-telegram-bot')
            return

        if not self.agent.config.chat_telegram_token:
            logger.error('Telegram bot token not configured')
            return

        try:
            logger.debug('Telegram_Client starting')
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'Telegram_Client not started: {e}')
            self._app = None

    def _close(self):
        self._stop_serving = True
        if self._serving_thread is not None:
            self._serving_thread.join(timeout=5)

    def _start_serving(self):
        """Thread target that runs async event loop."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run_async_bot())
        except Exception as e:
            logger.exception(f'Error in Telegram serving loop: {e}')
        finally:
            loop.close()

    async def _run_async_bot(self):
        """Run the bot with long polling."""
        try:
            # Build the application
            builder = Application.builder().token(self.agent.config.chat_telegram_token)
            if self.agent.config.chat_telegram_proxy:
                builder = builder.proxy(self.agent.config.chat_telegram_proxy)

            self._app = builder.build()

            # Add command handlers
            self._app.add_handler(CommandHandler("start", self._on_start))
            self._app.add_handler(CommandHandler("help", self._on_help))
            self._app.add_handler(CommandHandler("log_here", self._on_log_here))
            self._app.add_handler(CommandHandler("stop_log_here", self._on_stop_log_here))
            self._app.add_handler(CommandHandler("report_here", self._on_report_here))
            self._app.add_handler(CommandHandler("stop_report_here", self._on_stop_report_here))

            # Add message handler for text and media
            self._app.add_handler(
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VOICE |
                     filters.AUDIO | filters.Document.ALL) & ~filters.COMMAND,
                    self._on_message
                )
            )

            # Initialize and start
            await self._app.initialize()
            await self._app.start()

            # Get bot info
            bot_info = await self._app.bot.get_me()
            self.telegram_username = bot_info.username
            self.telegram_id = bot_info.id
            logger.debug(f'Telegram bot @{self.telegram_username} connected')

            # Start polling
            await self._app.updater.start_polling(
                allowed_updates=["message"],
                drop_pending_updates=True
            )

            # Keep running until stopped
            while not self._stop_serving:
                await asyncio.sleep(1)

            # Cleanup
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

        except Exception as e:
            logger.error(f'Telegram bot failed: {e}')
            self._app = None

    async def _on_start(self, update, context):
        """Handle /start command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        await update.message.reply_text(
            f"👋 Hi {user.first_name}! I'm MobileClaw bot.\n\n"
            "Send me a message and I'll respond!\n"
            "Type /help to see available commands."
        )

    async def _on_help(self, update, context):
        """Handle /help command."""
        if not update.message:
            return

        help_text = (
            "🤖 <b>MobileClaw bot commands</b>\n\n"
            "/start — Start the bot\n"
            "/help — Show this help message\n"
            "/log_here — Set this chat as log receiver (org_manager only)\n"
            "/stop_log_here — Stop receiving logs (org_manager only)\n"
            "/report_here — Set this chat as report receiver (org_manager only)\n"
            "/stop_report_here — Stop receiving reports (org_manager only)\n\n"
            "Just send me a text message to chat!"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def _on_log_here(self, update, context):
        """Handle /log_here command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        user_id = str(user.id)
        if user.username:
            user_id = f"{user_id}|{user.username}"

        # Check if user is org_manager
        if user_id != self.org_manager_user_id and str(user.id) != self.org_manager_user_id:
            await update.message.reply_text("❌ Only the organization manager can use this command.")
            return

        self.log_receiver = user_id
        # Set global log channel
        self.agent.chat.log_channel = 'telegram'
        logger.info(f"Log receiver set to user_id: {user_id}, global log channel set to telegram")
        await update.message.reply_text("✅ Log receiver set. Logs will be sent to you.")

    async def _on_stop_log_here(self, update, context):
        """Handle /stop_log_here command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        user_id = str(user.id)
        if user.username:
            user_id = f"{user_id}|{user.username}"

        # Check if user is org_manager
        if user_id != self.org_manager_user_id and str(user.id) != self.org_manager_user_id:
            await update.message.reply_text("❌ Only the organization manager can use this command.")
            return

        self.log_receiver = None
        # Clear global log channel if it was telegram
        if self.agent.chat.log_channel == 'telegram':
            self.agent.chat.log_channel = None
        logger.info("Log receiver cleared")
        await update.message.reply_text("✅ Log receiver cleared. Logs will no longer be sent.")

    async def _on_report_here(self, update, context):
        """Handle /report_here command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        user_id = str(user.id)
        if user.username:
            user_id = f"{user_id}|{user.username}"

        # Check if user is org_manager
        if user_id != self.org_manager_user_id and str(user.id) != self.org_manager_user_id:
            await update.message.reply_text("❌ Only the organization manager can use this command.")
            return

        self.report_receiver = user_id
        # Set global report channel
        self.agent.chat.report_channel = 'telegram'
        logger.info(f"Report receiver set to user_id: {user_id}, global report channel set to telegram")
        await update.message.reply_text("✅ Report receiver set. Progress reports will be sent to you.")

    async def _on_stop_report_here(self, update, context):
        """Handle /stop_report_here command."""
        if not update.message or not update.effective_user:
            return

        user = update.effective_user
        user_id = str(user.id)
        if user.username:
            user_id = f"{user_id}|{user.username}"

        # Check if user is org_manager
        if user_id != self.org_manager_user_id and str(user.id) != self.org_manager_user_id:
            await update.message.reply_text("❌ Only the organization manager can use this command.")
            return

        self.report_receiver = None
        # Clear global report channel if it was telegram
        if self.agent.chat.report_channel == 'telegram':
            self.agent.chat.report_channel = None
        logger.info("Report receiver cleared")
        await update.message.reply_text("✅ Report receiver cleared. Reports will be sent to org_manager.")

    async def _on_message(self, update, context):
        """Handle incoming messages (text, photos, voice, documents)."""
        if not update.message or not update.effective_user:
            return

        message = update.message
        user = update.effective_user
        chat_id = message.chat_id

        # Use stable numeric ID
        sender_id = str(user.id)
        if user.username:
            sender_id = f"{sender_id}|{user.username}"

        # Store chat_id for replies
        self._remember_chat_id(sender_id, chat_id)

        self._set_org_manager_if_missing(
            'org_manager_user_id',
            'chat_telegram_org_manager',
            sender_id,
        )
        if not self._should_handle_incoming(sender_id, self.org_manager_user_id, logger=logger, channel='telegram'):
            return

        # Build content from text and/or media
        content_parts = []
        media_paths = []

        # Text content
        if message.text:
            content_parts.append(message.text)
        if message.caption:
            content_parts.append(message.caption)

        # Handle media files
        media_file = None
        media_type = None

        if message.photo:
            media_file = message.photo[-1]  # Largest photo
            media_type = "image"
        elif message.voice:
            media_file = message.voice
            media_type = "voice"
        elif message.audio:
            media_file = message.audio
            media_type = "audio"
        elif message.document:
            media_file = message.document
            media_type = "file"

        # Download media if present
        if media_file and self._app:
            try:
                file = await self._app.bot.get_file(media_file.file_id)
                ext = self._get_extension(media_type, getattr(media_file, 'mime_type', None))

                # Save to ~/.mobileclaw/media/
                media_dir = Path.home() / ".mobileclaw" / "media"
                media_dir.mkdir(parents=True, exist_ok=True)

                file_path = media_dir / f"{media_file.file_id[:16]}{ext}"
                await file.download_to_drive(str(file_path))

                media_paths.append(str(file_path))
                content_parts.append(f"[{media_type}: {file_path}]")

                logger.debug(f"Downloaded {media_type} to {file_path}")
            except Exception as e:
                logger.error(f"Failed to download media: {e}")
                content_parts.append(f"[{media_type}: download failed]")

        content = "\n".join(content_parts) if content_parts else "[empty message]"

        # Create a simple message object
        class SimpleMessage:
            def __init__(self, msg_id, sender, content, chat_id):
                self.message_id = msg_id
                self.sender = sender
                self.content = content
                self.chat_id = chat_id

        msg = SimpleMessage(message.message_id, sender_id, content, chat_id)
        history_messages = self.get_history_messages(msg)
        history = "\n".join([f'[{m[2]}] {m[0]}: {m[1]}' for m in history_messages])
        self._append_history(chat_id, sender_id, content)

        # Call agent's message handler
        if hasattr(self.agent, 'handle_message'):
            await asyncio.to_thread(
                self.agent.handle_message,
                message=content,
                history=history,
                sender=sender_id,
                channel='telegram'
            )

    def _get_extension(self, media_type, mime_type):
        """Get file extension based on media type and mime type."""
        if media_type == "image":
            return ".jpg"
        elif media_type == "voice":
            return ".ogg"
        elif media_type == "audio":
            if mime_type and "mp3" in mime_type:
                return ".mp3"
            return ".ogg"
        elif media_type == "file":
            if mime_type:
                ext_map = {
                    "application/pdf": ".pdf",
                    "text/plain": ".txt",
                    "application/zip": ".zip",
                }
                return ext_map.get(mime_type, ".bin")
            return ".bin"
        return ""

    def send_message(self, message, receiver=None, subject=None):
        """Send a message to a user."""
        if not self._app:
            logger.warning('Telegram client not initialized')
            return
        manager_receiver = self._manager_receiver(self.org_manager_user_id)
        if manager_receiver is not None:
            receiver = manager_receiver

        if receiver is None:
            # Use report_receiver if set, otherwise default to org_manager
            if self.report_receiver:
                receiver = self.report_receiver
            else:
                receiver = self.org_manager_user_id

        if not receiver:
            logger.warning('No receiver specified for Telegram message')
            return

        # Get chat_id from mapping or use receiver directly
        chat_id = self._resolve_chat_id(receiver)
        if not chat_id:
            logger.warning(f'send_message failed. chat_id unavailable {receiver}')
            return

        try:
            # Run async operation in thread-safe way
            if self._loop and self._loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    self._async_send_message(message, chat_id),
                    self._loop
                )

                def _log_send_result(done_future):
                    try:
                        done_future.result()
                    except Exception as e:
                        logger.exception(f'Error sending Telegram message: {e}')

                future.add_done_callback(_log_send_result)
            else:
                # Fallback: create new event loop
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._async_send_message(message, chat_id))
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending Telegram message: {e}')

    async def _async_send_message(self, message, chat_id):
        """Async helper to send message."""
        try:
            plain_message = str(message)
            # Convert markdown to Telegram HTML
            html_content = _markdown_to_telegram_html(plain_message)

            try:
                await self._app.bot.send_message(
                    chat_id=int(chat_id),
                    text=html_content,
                    parse_mode="HTML"
                )
            except Exception as e:
                # Fallback to plain text if HTML parsing fails
                logger.warning(f'HTML parse failed, falling back to plain text: {e}')
                await self._app.bot.send_message(
                    chat_id=int(chat_id),
                    text=plain_message
                )
            self._append_history(str(chat_id), 'assistant', plain_message)
        except Exception as e:
            logger.error(f'Error in async send: {e}')
            raise

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        # Extract sender from previous message
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)
        else:
            logger.warning('Cannot reply: no sender in previous message')

    def send_to_org(self, message, subject="General"):
        """Send a message to the organization manager."""
        if self.org_manager_user_id:
            self.send_message(message, receiver=self.org_manager_user_id, subject=subject)
        else:
            logger.warning('No org manager configured for Telegram')

    def send_to_log(self, message, subject="Log"):
        """
        Send a message to the log receiver.
        If log_receiver is not set, returns without sending.
        """
        if self._manager_only_enabled() and self.org_manager_user_id:
            self.send_message(message, receiver=self.org_manager_user_id, subject=subject)
            return
        if self.log_receiver is None:
            logger.debug('No log receiver set, skipping send_to_log')
            return

        try:
            self.send_message(message, receiver=self.log_receiver, subject=subject)
        except Exception as e:
            logger.exception(f'Error sending to log receiver: {e}')

    def get_history_messages(self, msg, max_previous_messages=10):
        """Get recent cached message history for the current chat."""
        chat_id = getattr(msg, 'chat_id', None)
        if chat_id is None:
            sender = getattr(msg, 'sender', None)
            chat_id = self._chat_ids.get(sender)
        if chat_id is None:
            return []

        with self._history_lock:
            history = list(self._chat_history.get(str(chat_id), []))
        return history[-max_previous_messages:]

    def _append_history(self, chat_id, sender, content):
        """Append one message to in-memory chat history."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with self._history_lock:
            self._chat_history[str(chat_id)].append((str(sender), str(content), timestamp))
