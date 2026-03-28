"""
The Discord chat client implementation using Gateway WebSocket.
"""
import asyncio
import json
import os
from pathlib import Path
from threading import Thread
import structlog

from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    import httpx
    import websockets
    DISCORD_AVAILABLE = True
except ImportError:
    DISCORD_AVAILABLE = False
    httpx = None
    websockets = None

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20MB


class Discord_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.discord'
        self.org_manager_user_id = self.agent.config.chat_discord_org_manager
        self._serving_thread = None
        self._stop_serving = False
        self._loop = None
        self._ws = None
        self._seq = None
        self._heartbeat_task = None
        self._http = None
        self._bot_user_id = None
        # Maintain mapping of sender_id to channel_id for replies
        self._channel_ids = {}  # {sender_id: channel_id}
        self.log_receiver = None
        self.report_receiver = None

    def _open(self):
        if not DISCORD_AVAILABLE:
            logger.error('Discord dependencies not installed. Run: pip install httpx websockets')
            return

        if not self.agent.config.chat_discord_token:
            logger.error('Discord bot token not configured')
            return

        try:
            logger.debug('Discord_Client starting')
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'Discord_Client not started: {e}')

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
            logger.exception(f'Error in Discord serving loop: {e}')
        finally:
            loop.close()

    async def _run_async_bot(self):
        """Run the bot with Gateway WebSocket."""
        self._http = httpx.AsyncClient(timeout=30.0)
        try:
            while not self._stop_serving:
                try:
                    logger.info("Connecting to Discord gateway...")
                    async with websockets.connect(DISCORD_GATEWAY_URL) as ws:
                        self._ws = ws
                        await self._gateway_loop()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Discord gateway error: {e}")
                    if not self._stop_serving:
                        logger.info("Reconnecting to Discord gateway in 5 seconds...")
                        await asyncio.sleep(5)
        finally:
            if self._http:
                await self._http.aclose()
                self._http = None

    async def _gateway_loop(self):
        """Main gateway loop: identify, heartbeat, dispatch events."""
        if not self._ws:
            return

        async for raw in self._ws:
            if self._stop_serving:
                break
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            op = data.get("op")
            event_type = data.get("t")
            seq = data.get("s")
            payload = data.get("d")

            if seq is not None:
                self._seq = seq

            if op == 10:
                # HELLO: start heartbeat and identify
                interval_ms = payload.get("heartbeat_interval", 45000)
                await self._start_heartbeat(interval_ms / 1000)
                await self._identify()
            elif op == 0 and event_type == "READY":
                user = payload.get("user", {})
                self._bot_user_id = user.get("id")
                logger.debug(f'Discord bot connected as {user.get("username")}#{user.get("discriminator")}')
            elif op == 0 and event_type == "MESSAGE_CREATE":
                await self._handle_message_create(payload)
            elif op in (7, 9):
                # RECONNECT or INVALID_SESSION
                logger.info("Discord gateway requested reconnect")
                break

    async def _identify(self):
        """Send IDENTIFY payload."""
        if not self._ws:
            return
        identify = {
            "op": 2,
            "d": {
                "token": self.agent.config.chat_discord_token,
                "intents": 33281,  # GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT | DIRECT_MESSAGES
                "properties": {
                    "os": "mobileclaw",
                    "browser": "mobileclaw",
                    "device": "mobileclaw",
                },
            },
        }
        await self._ws.send(json.dumps(identify))

    async def _start_heartbeat(self, interval_s):
        """Start or restart the heartbeat loop."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        async def heartbeat_loop():
            while not self._stop_serving and self._ws:
                payload = {"op": 1, "d": self._seq}
                try:
                    await self._ws.send(json.dumps(payload))
                except Exception:
                    break
                await asyncio.sleep(interval_s)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def _handle_message_create(self, payload):
        """Handle incoming Discord messages."""
        author = payload.get("author") or {}
        if author.get("bot"):
            return

        sender_id = str(author.get("id", ""))
        channel_id = str(payload.get("channel_id", ""))
        content = payload.get("content") or ""

        if not sender_id or not channel_id:
            return

        # Store channel_id for replies
        self._channel_ids[sender_id] = channel_id

        org_manager_set = self._set_org_manager_if_missing(
            'org_manager_user_id',
            'chat_discord_org_manager',
            sender_id,
        )
        if org_manager_set:
            await self._send_to_channel(self._org_manager_status_text(), channel_id)

        # Build content from text and attachments
        content_parts = [content] if content else []
        media_paths = []

        for attachment in payload.get("attachments") or []:
            url = attachment.get("url")
            filename = attachment.get("filename") or "attachment"
            size = attachment.get("size") or 0
            if not url or not self._http:
                continue
            if size and size > MAX_ATTACHMENT_BYTES:
                content_parts.append(f"[attachment: {filename} - too large]")
                continue
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
                file_path = self._save_incoming_media_bytes(
                    'discord',
                    f"{attachment.get('id', 'file')}_{filename.replace('/', '_')}",
                    resp.content,
                )
                media_paths.append(str(file_path))
                content_parts.append(self._format_incoming_attachment_ref('attachment', file_path))
            except Exception as e:
                logger.warning(f"Failed to download Discord attachment: {e}")
                content_parts.append(f"[attachment: {filename} - download failed]")

        final_content = "\n".join(p for p in content_parts if p) or "[empty message]"

        # Handle commands
        if final_content.startswith('/') and sender_id == self.org_manager_user_id:
            await self._handle_command(final_content.strip(), channel_id)
            return
        if not self._should_handle_incoming(sender_id, self.org_manager_user_id, logger=logger, channel='discord'):
            return
        if not self._is_command_message(final_content) and self._ensure_report_receiver_global('discord', channel_id):
            await self._send_to_channel(self._receiver_status_text('report', True), channel_id)

        # Call agent's message handler
        if hasattr(self.agent, 'handle_message'):
            self.agent.handle_message(
                message=final_content,
                history=[],
                sender=sender_id,
                channel='discord'
            )

    async def _handle_command(self, command, channel_id):
        """Handle bot commands from org_manager."""
        if command == '/log_here':
            self.log_receiver = channel_id
            await self._send_to_channel(self._set_log_receiver_global('discord', channel_id), channel_id)
        elif command == '/stop_log_here':
            self.log_receiver = None
            await self._send_to_channel(self._clear_log_receiver_global(), channel_id)
        elif command == '/report_here':
            self.report_receiver = channel_id
            await self._send_to_channel(self._set_report_receiver_global('discord', channel_id), channel_id)
        elif command == '/stop_report_here':
            self.report_receiver = None
            await self._send_to_channel(self._clear_report_receiver_global(), channel_id)

    async def _send_to_channel(self, text, channel_id):
        """Send a message to a Discord channel via REST API."""
        if not self._http:
            return
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.agent.config.chat_discord_token}"}
        # Discord has a 2000 char limit per message
        for i in range(0, len(text), 2000):
            chunk = text[i:i+2000]
            try:
                response = await self._http.post(url, headers=headers, json={"content": chunk})
                if response.status_code == 429:
                    retry_after = response.json().get("retry_after", 1.0)
                    await asyncio.sleep(float(retry_after))
                    await self._http.post(url, headers=headers, json={"content": chunk})
            except Exception as e:
                logger.error(f"Error sending Discord message: {e}")

    async def _send_attachment_to_channel(self, item, channel_id):
        """Send an attachment to a Discord channel via REST API."""
        if not self._http:
            return
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.agent.config.chat_discord_token}"}
        with open(item['abs_path'], 'rb') as file_obj:
            files = {"files[0]": (item['name'], file_obj, item['mime_type'])}
            await self._http.post(url, headers=headers, files=files)

    def send_message(self, message, receiver=None, _type=None):
        """Send a message to a user or channel."""
        if not self._http or not self._loop:
            logger.warning('Discord client not initialized')
            return
        manager_receiver = self._manager_receiver(self.org_manager_user_id)
        if manager_receiver is not None:
            receiver = manager_receiver

        if receiver is None:
            if self.report_receiver:
                channel_id = self.report_receiver
            elif self.org_manager_user_id:
                channel_id = self._channel_ids.get(self.org_manager_user_id)
            else:
                logger.warning('No receiver specified for Discord message')
                return
        else:
            channel_id = self._channel_ids.get(receiver)

        if not channel_id:
            logger.warning(f'send_message failed. channel_id unavailable for {receiver}')
            return

        try:
            normalized_message = self._normalize_outgoing_message(message)
            if self._loop.is_running():
                for item in normalized_message:
                    coro = (
                        self._send_to_channel(item['text'], channel_id)
                        if item['kind'] == 'text'
                        else self._send_attachment_to_channel(item, channel_id)
                    )
                    asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)
            else:
                loop = asyncio.new_event_loop()
                try:
                    for item in normalized_message:
                        coro = (
                            self._send_to_channel(item['text'], channel_id)
                            if item['kind'] == 'text'
                            else self._send_attachment_to_channel(item, channel_id)
                        )
                        loop.run_until_complete(coro)
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending Discord message: {e}')

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)

    def get_history_messages(self, msg, max_previous_messages=10):
        """Get message history. Returns empty list."""
        return []
