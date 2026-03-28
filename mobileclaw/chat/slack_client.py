"""
The Slack chat client implementation using Socket Mode.
"""
import asyncio
from pathlib import Path
import re
from threading import Thread
import structlog
import requests

from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient
    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    SocketModeClient = None
    SocketModeRequest = None
    SocketModeResponse = None
    AsyncWebClient = None


class Slack_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.slack'
        self.org_manager_user_id = self.agent.config.chat_slack_org_manager
        self._serving_thread = None
        self._stop_serving = False
        self._loop = None
        self._web_client = None
        self._socket_client = None
        self._bot_user_id = None
        # Maintain mapping of sender_id to channel_id for replies
        self._channel_ids = {}  # {sender_id: channel_id}
        self.log_receiver = None
        self.report_receiver = None

    def _open(self):
        if not SLACK_AVAILABLE:
            logger.error('Slack SDK not installed. Run: pip install slack_sdk aiohttp')
            return

        if not self.agent.config.chat_slack_bot_token or not self.agent.config.chat_slack_app_token:
            logger.error('Slack bot_token and app_token not configured')
            return

        try:
            logger.debug('Slack_Client starting')
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'Slack_Client not started: {e}')

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
            logger.exception(f'Error in Slack serving loop: {e}')
        finally:
            loop.close()

    async def _run_async_bot(self):
        """Run the Slack bot with Socket Mode."""
        self._web_client = AsyncWebClient(token=self.agent.config.chat_slack_bot_token)
        self._socket_client = SocketModeClient(
            app_token=self.agent.config.chat_slack_app_token,
            web_client=self._web_client,
        )

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)

        # Get bot user ID
        try:
            auth = await self._web_client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.debug(f'Slack bot connected as {self._bot_user_id}')
        except Exception as e:
            logger.warning(f"Slack auth_test failed: {e}")

        logger.info("Starting Slack Socket Mode client...")
        await self._socket_client.connect()

        while not self._stop_serving:
            await asyncio.sleep(1)

        # Cleanup
        try:
            await self._socket_client.close()
        except Exception:
            pass

    async def _on_socket_request(self, client, req):
        """Handle incoming Socket Mode requests."""
        if req.type != "events_api":
            return

        # Acknowledge immediately
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        payload = req.payload or {}
        event = payload.get("event") or {}
        event_type = event.get("type")

        if event_type not in ("message", "app_mention"):
            return

        sender_id = event.get("user")
        chat_id = event.get("channel")

        # Ignore bot/system messages
        if event.get("subtype"):
            return
        if self._bot_user_id and sender_id == self._bot_user_id:
            return

        text = event.get("text") or ""
        content_parts = [text] if text else []

        for file_info in event.get("files") or []:
            file_url = file_info.get("url_private_download") or file_info.get("url_private")
            if not file_url:
                continue
            filename = file_info.get("name") or "attachment"
            try:
                response = requests.get(
                    file_url,
                    headers={"Authorization": f"Bearer {self.agent.config.chat_slack_bot_token}"},
                    timeout=30,
                )
                response.raise_for_status()
                file_path = self._save_incoming_media_bytes(
                    'slack',
                    filename.replace("/", "_"),
                    response.content,
                )
                if (file_info.get("mimetype") or "").startswith("image/"):
                    content_parts.append(self._format_incoming_attachment_ref('image', file_path))
                else:
                    content_parts.append(self._format_incoming_attachment_ref('file', file_path))
            except Exception as e:
                logger.warning(f"Failed to download Slack attachment: {e}")
                content_parts.append(f"[attachment: {filename} - download failed]")

        text = "\n".join([part for part in content_parts if part]).strip()

        # Avoid double-processing: prefer app_mention over message with mention
        if event_type == "message" and self._bot_user_id and f"<@{self._bot_user_id}>" in text:
            return

        if not sender_id or not chat_id:
            return

        # Store channel_id for replies
        self._channel_ids[sender_id] = chat_id

        org_manager_set = self._set_org_manager_if_missing(
            'org_manager_user_id',
            'chat_slack_org_manager',
            sender_id,
        )
        if org_manager_set and self._web_client:
            try:
                await self._web_client.chat_postMessage(channel=chat_id, text=self._org_manager_status_text(), thread_ts=thread_ts)
            except Exception as e:
                logger.error(f"Error sending Slack manager response: {e}")

        # Strip bot mention from text
        if self._bot_user_id:
            text = re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

        channel_type = event.get("channel_type") or ""
        thread_ts = event.get("thread_ts") or event.get("ts")

        # Add reaction to indicate "seen"
        try:
            if self._web_client and event.get("ts"):
                await self._web_client.reactions_add(
                    channel=chat_id,
                    name="eyes",
                    timestamp=event.get("ts"),
                )
        except Exception as e:
            logger.debug(f"Slack reactions_add failed: {e}")

        # Handle commands
        if text.startswith('/') and sender_id == self.org_manager_user_id:
            await self._handle_command(text.strip(), chat_id, thread_ts)
            return
        if not self._should_handle_incoming(sender_id, self.org_manager_user_id, logger=logger, channel='slack'):
            return
        if not self._is_command_message(text) and self._ensure_report_receiver_global('slack', chat_id) and self._web_client:
            try:
                await self._web_client.chat_postMessage(channel=chat_id, text=self._receiver_status_text('report', True), thread_ts=thread_ts)
            except Exception as e:
                logger.error(f"Error sending Slack report receiver response: {e}")

        # Call agent's message handler
        if hasattr(self.agent, 'handle_message'):
            self.agent.handle_message(
                message=text,
                history=[],
                sender=sender_id,
                channel='slack'
            )

    async def _handle_command(self, command, channel_id, thread_ts=None):
        """Handle bot commands from org_manager."""
        response_text = None
        if command == '/log_here':
            self.log_receiver = channel_id
            response_text = self._set_log_receiver_global('slack', channel_id)
        elif command == '/stop_log_here':
            self.log_receiver = None
            response_text = self._clear_log_receiver_global()
        elif command == '/report_here':
            self.report_receiver = channel_id
            response_text = self._set_report_receiver_global('slack', channel_id)
        elif command == '/stop_report_here':
            self.report_receiver = None
            response_text = self._clear_report_receiver_global()

        if response_text and self._web_client:
            try:
                await self._web_client.chat_postMessage(
                    channel=channel_id,
                    text=response_text,
                    thread_ts=thread_ts,
                )
            except Exception as e:
                logger.error(f"Error sending Slack command response: {e}")

    async def _async_send(self, text, channel_id, thread_ts=None):
        """Send a message to a Slack channel."""
        if not self._web_client:
            return
        try:
            await self._web_client.chat_postMessage(
                channel=channel_id,
                text=text,
                thread_ts=thread_ts,
            )
        except Exception as e:
            logger.error(f"Error sending Slack message: {e}")

    async def _async_send_attachment(self, item, channel_id):
        """Send an attachment to Slack."""
        if not self._web_client:
            return
        with open(item['abs_path'], 'rb') as file_obj:
            await self._web_client.files_upload_v2(
                channel=channel_id,
                file=file_obj,
                filename=item['name'],
                title=item['name'],
            )

    def send_message(self, message, receiver=None, _type=None):
        """Send a message to a user or channel."""
        if not self._web_client or not self._loop:
            logger.warning('Slack client not initialized')
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
                logger.warning('No receiver specified for Slack message')
                return
        else:
            channel_id = self._channel_ids.get(receiver, receiver)

        if not channel_id:
            logger.warning(f'send_message failed. channel_id unavailable for {receiver}')
            return

        try:
            normalized_message = self._normalize_outgoing_message(message)
            if self._loop.is_running():
                for item in normalized_message:
                    coro = (
                        self._async_send(item['text'], channel_id)
                        if item['kind'] == 'text'
                        else self._async_send_attachment(item, channel_id)
                    )
                    asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=10)
            else:
                loop = asyncio.new_event_loop()
                try:
                    for item in normalized_message:
                        coro = (
                            self._async_send(item['text'], channel_id)
                            if item['kind'] == 'text'
                            else self._async_send_attachment(item, channel_id)
                        )
                        loop.run_until_complete(coro)
                finally:
                    loop.close()
        except Exception as e:
            logger.exception(f'Error sending Slack message: {e}')

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)

    def get_history_messages(self, msg, max_previous_messages=10):
        """Get message history. Returns empty list."""
        return []
