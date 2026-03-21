"""
The WhatsApp chat client implementation using Node.js bridge (via @whiskeysockets/baileys).
"""
import asyncio
import json
from threading import Thread
import structlog

from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None


class WhatsApp_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.whatsapp'
        self.org_manager_user_id = self.agent.config.chat_whatsapp_org_manager
        self._serving_thread = None
        self._stop_serving = False
        self._loop = None
        self._ws = None
        self._connected = False
        # Maintain mapping of sender_id to full chat_id for replies
        self._chat_ids = {}  # {sender_id: full_jid}
        self.log_receiver = None
        self.report_receiver = None

    def _open(self):
        if not WEBSOCKETS_AVAILABLE:
            logger.error('websockets not installed. Run: pip install websockets')
            return

        try:
            logger.debug('WhatsApp_Client starting')
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'WhatsApp_Client not started: {e}')

    def _close(self):
        self._stop_serving = True
        self._connected = False
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
            logger.exception(f'Error in WhatsApp serving loop: {e}')
        finally:
            loop.close()

    async def _run_async_bot(self):
        """Run the bot by connecting to the Node.js WhatsApp bridge."""
        bridge_url = self.agent.config.chat_whatsapp_bridge_url or 'ws://localhost:18790'

        while not self._stop_serving:
            try:
                logger.info(f"Connecting to WhatsApp bridge at {bridge_url}...")
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.debug("Connected to WhatsApp bridge")

                    async for message in ws:
                        if self._stop_serving:
                            break
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error(f"Error handling bridge message: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning(f"WhatsApp bridge connection error: {e}")
                if not self._stop_serving:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def _handle_bridge_message(self, raw):
        """Handle a message from the Node.js bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON from bridge: {raw[:100]}")
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Phone number style: <phone>@s.whatsapp.net
            pn = data.get("pn", "")
            # New LID style
            sender = data.get("sender", "")
            content = data.get("content", "")

            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id

            # Store full jid for replies
            self._chat_ids[sender_id] = sender

            self._set_org_manager_if_missing(
                'org_manager_user_id',
                'chat_whatsapp_org_manager',
                sender_id,
            )

            # Handle commands
            if content.startswith('/') and sender_id == self.org_manager_user_id:
                await self._handle_command(content.strip(), sender)
                return

            # Call agent's message handler
            if hasattr(self.agent, 'handle_message'):
                self.agent.handle_message(
                    message=content,
                    history=[],
                    sender=sender_id,
                    channel='whatsapp'
                )

        elif msg_type == "status":
            status = data.get("status")
            logger.info(f"WhatsApp status: {status}")
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "qr":
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            logger.error(f"WhatsApp bridge error: {data.get('error')}")

    async def _handle_command(self, command, chat_id):
        """Handle bot commands from org_manager."""
        if command == '/log_here':
            self.log_receiver = chat_id
            self.agent.chat.log_channel = 'whatsapp'
            await self._async_send("Log receiver set.", chat_id)
        elif command == '/stop_log_here':
            self.log_receiver = None
            if self.agent.chat.log_channel == 'whatsapp':
                self.agent.chat.log_channel = None
            await self._async_send("Log receiver cleared.", chat_id)
        elif command == '/report_here':
            self.report_receiver = chat_id
            self.agent.chat.report_channel = 'whatsapp'
            await self._async_send("Report receiver set.", chat_id)
        elif command == '/stop_report_here':
            self.report_receiver = None
            if self.agent.chat.report_channel == 'whatsapp':
                self.agent.chat.report_channel = None
            await self._async_send("Report receiver cleared.", chat_id)

    async def _async_send(self, text, chat_id):
        """Send a message through the WhatsApp bridge."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        try:
            payload = {
                "type": "send",
                "to": chat_id,
                "text": text
            }
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp message: {e}")

    def send_message(self, message, receiver=None, subject=None):
        """Send a message to a user."""
        if not self._ws or not self._connected:
            logger.warning('WhatsApp client not connected')
            return

        if receiver is None:
            if self.report_receiver:
                chat_id = self.report_receiver
            elif self.org_manager_user_id:
                chat_id = self._chat_ids.get(self.org_manager_user_id)
            else:
                logger.warning('No receiver specified for WhatsApp message')
                return
        else:
            chat_id = self._chat_ids.get(receiver, receiver)

        if not chat_id:
            logger.warning(f'send_message failed. chat_id unavailable for {receiver}')
            return

        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._async_send(str(message), chat_id),
                    self._loop
                ).result(timeout=10)
            else:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._async_send(str(message), chat_id))
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending WhatsApp message: {e}')

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)

    def send_to_org(self, message, subject="General"):
        """Send a message to the organization manager."""
        if self.org_manager_user_id:
            self.send_message(message, receiver=self.org_manager_user_id, subject=subject)

    def send_to_log(self, message, subject="Log"):
        """Send a message to the log receiver."""
        if self.log_receiver is None:
            return
        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._async_send(str(message), self.log_receiver),
                    self._loop
                ).result(timeout=10)
        except Exception as e:
            logger.exception(f'Error sending to log receiver: {e}')

    def get_history_messages(self, msg, max_previous_messages=10):
        """Get message history. Returns empty list."""
        return []
