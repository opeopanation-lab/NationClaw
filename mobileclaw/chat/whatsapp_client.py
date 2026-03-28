"""
The WhatsApp chat client implementation using Node.js bridge (via @whiskeysockets/baileys).
"""
import asyncio
import base64
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
            content = data.get("content", "") or ""
            media_type = data.get("media_type")
            file_name = data.get("file_name") or "attachment"
            file_data = data.get("file_data")
            file_path = data.get("file_path")

            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id

            # Store full jid for replies
            self._chat_ids[sender_id] = sender

            org_manager_set = self._set_org_manager_if_missing(
                'org_manager_user_id',
                'chat_whatsapp_org_manager',
                sender_id,
            )
            if org_manager_set:
                await self._async_send(self._org_manager_status_text(), sender)

            content_parts = [content] if content else []
            if media_type:
                saved_path = None
                try:
                    if file_data:
                        saved_path = self._save_incoming_media_bytes(
                            'whatsapp',
                            file_name,
                            base64.b64decode(file_data),
                        )
                    elif file_path:
                        with open(file_path, 'rb') as file_obj:
                            saved_path = self._save_incoming_media_bytes(
                                'whatsapp',
                                file_name,
                                file_obj.read(),
                            )
                except Exception as e:
                    logger.warning(f"Failed to persist WhatsApp media: {e}")
                if saved_path:
                    content_parts.append(self._format_incoming_attachment_ref(media_type, saved_path))
            content = "\n".join([part for part in content_parts if part]).strip()

            # Handle commands
            if content.startswith('/') and sender_id == self.org_manager_user_id:
                await self._handle_command(content.strip(), sender)
                return
            if not self._should_handle_incoming(sender_id, self.org_manager_user_id, logger=logger, channel='whatsapp'):
                return
            if not self._is_command_message(content) and self._ensure_report_receiver_global('whatsapp', sender):
                await self._async_send(self._receiver_status_text('report', True), sender)

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
            await self._async_send(self._set_log_receiver_global('whatsapp', chat_id), chat_id)
        elif command == '/stop_log_here':
            self.log_receiver = None
            await self._async_send(self._clear_log_receiver_global(), chat_id)
        elif command == '/report_here':
            self.report_receiver = chat_id
            await self._async_send(self._set_report_receiver_global('whatsapp', chat_id), chat_id)
        elif command == '/stop_report_here':
            self.report_receiver = None
            await self._async_send(self._clear_report_receiver_global(), chat_id)

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

    async def _async_send_media(self, item, chat_id):
        """Send media through the WhatsApp bridge."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return
        try:
            with open(item['abs_path'], 'rb') as file_obj:
                file_data = base64.b64encode(file_obj.read()).decode('utf-8')
            payload = {
                "type": "send_media",
                "to": chat_id,
                "media_type": item['message_type'],
                "file_name": item['name'],
                "mime_type": item['mime_type'],
                "file_path": item['abs_path'],
                "file_data": file_data,
            }
            await self._ws.send(json.dumps(payload))
        except Exception as e:
            logger.error(f"Error sending WhatsApp media: {e}")

    def send_message(self, message, receiver=None, _type=None):
        """Send a message to a user."""
        if not self._ws or not self._connected:
            logger.warning('WhatsApp client not connected')
            return
        manager_receiver = self._manager_receiver(self.org_manager_user_id)
        if manager_receiver is not None:
            receiver = manager_receiver

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
            normalized_message = self._normalize_outgoing_message(message)
            if self._loop and self._loop.is_running():
                for item in normalized_message:
                    coro = (
                        self._async_send(item['text'], chat_id)
                        if item['kind'] == 'text'
                        else self._async_send_media(item, chat_id)
                    )
                    asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)
            else:
                loop = asyncio.new_event_loop()
                try:
                    for item in normalized_message:
                        coro = (
                            self._async_send(item['text'], chat_id)
                            if item['kind'] == 'text'
                            else self._async_send_media(item, chat_id)
                        )
                        loop.run_until_complete(coro)
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending WhatsApp message: {e}')

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)

    def get_history_messages(self, msg, max_previous_messages=10):
        """Get message history. Returns empty list."""
        return []
