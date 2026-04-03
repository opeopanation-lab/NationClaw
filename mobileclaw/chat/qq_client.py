"""
The QQ chat client implementation.
"""
import asyncio
import base64
from collections import deque
from threading import Thread
import structlog

from mobileclaw.utils.interface import UniInterface
from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    import botpy
    from botpy.message import C2CMessage
    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None


def _make_bot_class(client: "QQ_Client") -> "type":
    """Create a botpy Client subclass bound to the given client."""
    if not QQ_AVAILABLE:
        return None

    intents = botpy.Intents(c2c_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents)

        async def on_ready(self):
            logger.info(f"QQ bot ready: {self.robot.name}")
            client.qq_name = self.robot.name
            client.qq_id = getattr(self.robot, 'id', None)

        async def on_c2c_message_create(self, message: "C2CMessage"):
            await client._on_message(message)

        async def on_direct_message_create(self, message):
            await client._on_message(message)

    return _Bot


class QQ_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.qq'
        self.org_manager_user_id = self.agent.config.chat_qq_org_manager
        self.qq_name = None
        self.qq_id = None
        self._serving_thread = None
        self._stop_serving = False
        self._client = None
        self._loop = None
        # Message deduplication
        self._processed_ids = deque(maxlen=1000)
        # Maintain mapping of user ids to open_ids
        self._user_mapping = {}  # {user_id: open_id}
        self._chat_id_mapping = {}  # {sender_id: chat_id} for replies
        # Log receiver for send_to_log messages
        self.log_receiver = None  # Set via /log_here command
        # Report receiver for send_message when receiver is None
        self.report_receiver = None  # Set via /report_here command

    def _open(self):
        if not QQ_AVAILABLE:
            logger.error('QQ SDK not installed. Run: pip install qq-botpy', action='start qq client', status='failed')
            return

        if not self.agent.config.chat_qq_app_id or not self.agent.config.chat_qq_secret:
            logger.error('QQ app_id and secret not configured', action='start qq client', status='failed')
            return

        try:
            logger.debug('QQ_Client starting', action='start qq client', status='starting')
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'QQ_Client not started: {e}', action='start qq client', status='failed')
            self._client = None

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
            logger.exception(f'Error in QQ serving loop: {e}')
        finally:
            loop.close()

    async def _run_async_bot(self):
        """Run the bot connection."""
        try:
            BotClass = _make_bot_class(self)
            if not BotClass:
                logger.error('QQ SDK not available', action='run qq bot', status='failed')
                return

            self._client = BotClass()
            logger.debug('QQ_Client started', action='start qq client', status='done')

            await self._client.start(
                appid=self.agent.config.chat_qq_app_id,
                secret=self.agent.config.chat_qq_secret
            )
        except Exception as e:
            logger.error(f'QQ auth failed, check AppID/Secret at q.qq.com: {e}', action='run qq bot', status='failed')
            self._client = None

    async def _on_message(self, data):
        """Handle incoming message from QQ."""
        try:
            # Dedup by message ID
            if data.id in self._processed_ids:
                return
            self._processed_ids.append(data.id)

            author = data.author
            user_id = str(getattr(author, 'id', None) or getattr(author, 'user_openid', 'unknown'))
            content = (data.content or "").strip()

            if not content:
                return

            org_manager_set = self._set_org_manager_if_missing(
                'org_manager_user_id',
                'chat_qq_org_manager',
                user_id,
            )
            if org_manager_set and self._should_send_system_message():
                await self._async_send_message(self._org_manager_status_text(), user_id)

            # Handle commands (only from org_manager)
            if content.startswith('/') and user_id == self.org_manager_user_id:
                await self._handle_command(content.strip(), user_id, data.id)
                return
            if not self._should_handle_incoming(user_id, self.org_manager_user_id, logger=logger, channel='qq'):
                return
            if (
                not self._is_command_message(content)
                and self._ensure_report_receiver_global('qq', user_id)
                and self._should_send_system_message()
            ):
                await self._async_send_message(self._receiver_status_text('report', True), user_id)

            # Store chat_id for replies
            self._chat_id_mapping[user_id] = user_id

            # Create a simple message object for history
            class SimpleMessage:
                def __init__(self, msg_id, sender, content):
                    self.message_id = msg_id
                    self.sender = sender
                    self.content = content

            msg = SimpleMessage(data.id, user_id, content)

            # Get message history (empty for now)
            history = []

            # Call agent's message handler
            if hasattr(self.agent, 'handle_message'):
                self.agent.handle_message(
                    message=content,
                    history=history,
                    sender=user_id,
                    channel='qq'
                )
        except Exception as e:
            logger.exception(f'Error handling QQ message: {e}')

    async def _handle_command(self, command: str, user_id: str, message_id: str):
        """
        Handle bot commands from org_manager.

        Args:
            command: The command string (e.g., "/log_here")
            user_id: The user ID who sent the command
            message_id: The message ID
        """
        try:
            if command.endswith("/log_here"):
                self.log_receiver = user_id
                response_text = self._set_log_receiver_global('qq', user_id)
                logger.info(f"Log receiver set to user_id: {user_id}")

            elif command.endswith("/stop_log_here"):
                self.log_receiver = None
                response_text = self._clear_log_receiver_global()
                logger.info("Log receiver cleared")

            elif command.endswith("/report_here"):
                self.report_receiver = user_id
                response_text = self._set_report_receiver_global('qq', user_id)
                logger.info(f"Report receiver set to user_id: {user_id}")

            elif command.endswith("/stop_report_here"):
                self.report_receiver = None
                response_text = self._clear_report_receiver_global()
                logger.info("Report receiver cleared")

            else:
                # Unknown command
                response_text = (
                    f"❓ Unknown command: {command}\n\n"
                    "Available commands:\n"
                    f"{self._available_system_commands_text()}"
                )

            # Send response
            await self._send_text_message(response_text, user_id)

        except Exception as e:
            logger.exception(f"Error handling command: {e}")

    async def _send_text_message(self, text: str, user_id: str):
        """Send a text message to a user."""
        try:
            await self._client.api.post_c2c_message(
                openid=user_id,
                msg_type=0,  # Text message
                content=text,
            )
        except Exception as e:
            logger.debug(f"Error sending text message: {e}")

    def send_message(self, message, receiver=None, _type=None):
        """Send a message to a user."""
        if not self._client:
            logger.warning('QQ client not initialized')
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
            logger.warning('No receiver specified for QQ message')
            return

        try:
            # Run async operation in thread-safe way
            normalized_message = self._normalize_outgoing_message(message)
            if self._loop and self._loop.is_running():
                for item in normalized_message:
                    coro = (
                        self._async_send_message(item['text'], receiver)
                        if item['kind'] == 'text'
                        else self._async_send_attachment(item, receiver)
                    )
                    asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)
            else:
                # Fallback: create new event loop
                loop = asyncio.new_event_loop()
                try:
                    for item in normalized_message:
                        coro = (
                            self._async_send_message(item['text'], receiver)
                            if item['kind'] == 'text'
                            else self._async_send_attachment(item, receiver)
                        )
                        loop.run_until_complete(coro)
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending QQ message: {e}')

    async def _async_send_attachment(self, item, receiver):
        """Async helper to send attachment."""
        try:
            if hasattr(self._client.api, 'post_c2c_base64file'):
                file_type = 1 if item['message_type'] == 'image' else 4
                with open(item['abs_path'], 'rb') as file_obj:
                    encoded = base64.b64encode(file_obj.read()).decode('utf-8')
                await self._client.api.post_c2c_base64file(
                    openid=receiver,
                    file_type=file_type,
                    file_data=encoded,
                    srv_send_msg=True,
                )
            else:
                await self._async_send_message(f"[{item['message_type']}: {item['rel_path']}]", receiver)
        except Exception as e:
            logger.error(f'Error in async attachment send: {e}')
            raise

    async def _async_send_message(self, message, receiver):
        """Async helper to send message."""
        try:
            await self._client.api.post_c2c_message(
                openid=receiver,
                msg_type=0,  # Text message
                content=message,
            )
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

    def get_history_messages(self, msg, max_previous_messages=100):
        """Get message history. Returns empty list as QQ API doesn't provide easy history access."""
        return []
