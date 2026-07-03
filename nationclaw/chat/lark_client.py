"""
The Lark/Feishu chat client implementation.
"""
import re
import json
import asyncio
from collections import OrderedDict
import structlog
import time
import requests

from nationclaw.utils.interface import UniInterface
from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
    from lark_oapi.api.contact.v3 import *
    from PIL import Image
    LARK_AVAILABLE = True
except ImportError:
    LARK_AVAILABLE = False
    lark = None
    Image = None

# Message type display mapping
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


class Lark_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from nationclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.lark'
        self.org_manager_open_id = self.agent.config.chat_lark_org_manager
        self.lark_name = None
        self.lark_open_id = None
        self._serving_thread = None
        self._ws_thread = None
        self._stop_serving = False
        self._ws_client = None
        self._loop = None
        self._tenant_access_token = None
        self._tenant_access_token_expire_at = 0
        self._resource_path_cache = {}
        # Message deduplication with OrderedDict (maintains insertion order)
        self._processed_message_ids = OrderedDict()
        # Maintain mapping of user names/ids to open_ids
        self._user_mapping = {}  # {name: open_id, id: open_id}
        self._group_mapping = {}  # {group_name: chat_id}
        # Log receiver for send_to_log messages
        self.log_receiver = None  # Set via /log_here command
        # Report receiver for send_message when receiver is None
        self.report_receiver = None  # Set via /report_here command

    def _open(self):
        if not LARK_AVAILABLE:
            logger.error('Lark SDK not installed. Run: pip install lark_oapi', action='start lark client', status='failed')
            return

        if not self.agent.config.chat_lark_app_id or not self.agent.config.chat_lark_app_secret:
            logger.error('Lark app_id and app_secret not configured', action='start lark client', status='failed')
            return

        try:
            # Initialize Lark client with app credentials
            self.client = lark.Client.builder() \
                .app_id(self.agent.config.chat_lark_app_id) \
                .app_secret(self.agent.config.chat_lark_app_secret) \
                .log_level(lark.LogLevel.INFO) \
                .build()

            logger.debug('Lark_Client initialized, verifying credentials',
                       action='start lark client', status='starting')

            # Verify credentials and convert org_manager identifier to open_id
            self._verify_and_get_open_ids()

            logger.debug('Lark_Client verified, starting WebSocket connection',
                       action='start lark client', status='starting')

            # Start WebSocket connection in separate thread
            from threading import Thread
            self._serving_thread = Thread(target=self._start_serving)
            self._serving_thread.start()

        except Exception as e:
            logger.exception(f'Lark_Client not started: {e}', action='start lark client', status='failed')
            self.client = None

    def _close(self):
        self._stop_serving = True
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception as e:
                logger.warning(f'Error stopping WebSocket client: {e}')
        if self._serving_thread is not None:
            self._serving_thread.join(timeout=5)

    def _verify_and_get_open_ids(self):
        """
        Verify credentials, get bot info, and convert org_manager identifier (email/phone) to open_id.
        """
        try:
            # Set bot name (bots don't have traditional open_ids in Lark)
            self.lark_name = f"Bot_{self.agent.config.chat_lark_app_id}"
            self.lark_open_id = None  # Bots are auto-added to chats they create
            logger.debug(f'Bot name set: {self.lark_name}')

            # Convert org_manager identifier to open_id if configured
            if not self.org_manager_open_id:
                logger.debug('No org_manager configured, skipping org_manager verification')
                return

            org_manager = self.org_manager_open_id
            emails = []
            mobiles = []

            # Simple heuristic: if contains @, it's email; if all digits (with optional +), it's phone
            if '@' in org_manager:
                emails = [org_manager]
            elif org_manager.replace('+', '').replace('-', '').replace(' ', '').isdigit():
                # Clean phone number (remove spaces, dashes)
                mobiles = [org_manager.replace('-', '').replace(' ', '')]
            else:
                # Assume it's already an open_id
                logger.debug(f'manager appears to be an open_id: {org_manager}')
                return

            # Build request to convert email/phone to open_id
            request = BatchGetIdUserRequest.builder() \
                .user_id_type("open_id") \
                .request_body(BatchGetIdUserRequestBody.builder()
                    .emails(emails if emails else None)
                    .mobiles(mobiles if mobiles else None)
                    .include_resigned(False)
                    .build()) \
                .build()

            # Send request
            response = self.client.contact.v3.user.batch_get_id(request)

            if not response.success():
                logger.error(
                    f"Failed to get open_id for org_manager: code={response.code}, "
                    f"msg={response.msg}, log_id={response.get_log_id()}"
                )
                raise Exception(f'Failed to verify org_manager: {response.msg}')

            # Extract open_id from response
            if response.data and response.data.user_list:
                for user in response.data.user_list:
                    if user.user_id:
                        self.org_manager_open_id = user.user_id
                        logger.info(f'Converted org_manager to open_id: {self.org_manager_open_id}')
                        return

            logger.warning(f'No user found for org_manager: {org_manager}')

        except Exception as e:
            logger.exception(f'Error in verification: {e}')
            raise

    def _start_serving(self):
        """Start WebSocket long connection for receiving messages."""
        try:
            # Create async event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            # Create event handler
            event_handler = lark.EventDispatcherHandler.builder(
                "",  # encrypt_key (optional)
                "",  # verification_token (optional)
            ).register_p2_im_message_receive_v1(
                self._on_message_sync
            ).build()

            # Create WebSocket client
            self._ws_client = lark.ws.Client(
                self.agent.config.chat_lark_app_id,
                self.agent.config.chat_lark_app_secret,
                event_handler=event_handler,
                log_level=lark.LogLevel.INFO
            )

            logger.info('Lark WebSocket connection starting (no public IP required)')

            # Start WebSocket client (blocking call)
            self._ws_client.start()

        except Exception as e:
            logger.exception(f'Error in WebSocket serving loop: {e}')
        finally:
            if self._loop:
                self._loop.close()

    def _on_message_sync(self, data):
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the event loop.
        """
        try:
            # Create task in the current event loop (WebSocket's loop)
            asyncio.create_task(self._on_message(data))
        except Exception as e:
            logger.exception(f'Error scheduling message handler: {e}')

    async def _on_message(self, data):
        """Handle incoming message from Lark/Feishu."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Deduplication check
            message_id = message.message_id
            if message_id in self._processed_message_ids:
                return
            self._processed_message_ids[message_id] = None

            # Trim cache: keep most recent 500 when exceeds 1000
            while len(self._processed_message_ids) > 1000:
                self._processed_message_ids.popitem(last=False)

            # Skip bot messages
            sender_type = sender.sender_type
            if sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type  # "p2p" or "group"
            msg_type = message.message_type

            # Add reaction to indicate "seen"
            await self._add_reaction(message_id, "THUMBSUP")

            # Parse message content
            if msg_type == "text":
                try:
                    parsed = json.loads(message.content)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    rich_content = self._normalize_rich_content(parsed, message_id=message_id)
                    if rich_content is not None:
                        content = rich_content
                    else:
                        content = parsed.get("text", "")
                else:
                    content = message.content or ""
            elif msg_type == "image":
                try:
                    image_key = json.loads(message.content).get("image_key")
                except json.JSONDecodeError:
                    image_key = None
                if image_key:
                    file_path = self._download_resource_via_rest(message_id, image_key, "image", f"{image_key}.png")
                    content = self._format_incoming_attachment_ref('image', file_path)
                else:
                    content = "[image]"
            elif msg_type == "file":
                try:
                    payload = json.loads(message.content)
                    file_key = payload.get("file_key")
                    file_name = payload.get("file_name") or f"{file_key}.bin"
                except json.JSONDecodeError:
                    file_key = None
                    file_name = "file.bin"
                if file_key:
                    file_path = self._download_resource_via_rest(message_id, file_key, "file", file_name)
                    content = self._format_incoming_attachment_ref('file', file_path)
                else:
                    content = "[file]"
            else:
                try:
                    parsed = json.loads(message.content)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
                rich_content = self._normalize_rich_content(parsed, message_id=message_id)
                if rich_content is not None:
                    content = rich_content
                else:
                    content = MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]")

            if not content:
                return

            org_manager_set = self._set_org_manager_if_missing(
                'org_manager_open_id',
                'chat_lark_org_manager',
                sender_id,
            )
            if org_manager_set and self._should_send_system_message():
                await self._send_text_reply(self._org_manager_status_text(), message_id)

            # Handle commands (only from org_manager)
            if content.startswith('/') and sender_id == self.org_manager_open_id:
                await self._handle_command(content.strip(), chat_id, message_id)
                return

            # Determine sender name for routing
            if chat_type == "p2p":
                sender_name = sender_id
            else:
                # Group message
                sender_name = f"group:{chat_id}"
                self._group_mapping[chat_id] = chat_id

            # Maintain user mapping
            self._user_mapping[sender_id] = sender_id

            # Create simple message object
            class SimpleMessage:
                def __init__(self, msg_id, sender, content, chat_id):
                    self.message_id = msg_id
                    self.sender = sender
                    self.content = content
                    self.chat_id = chat_id

            msg = SimpleMessage(message_id, sender_id, content, chat_id)

            # Get history messages
            history_messages = self.get_history_messages(msg)
            history_content = "\n".join([f'[{m[2]}] {m[0]}: {m[1]}' for m in history_messages])

            # Handle the message
            if not self._should_handle_incoming(sender_id, self.org_manager_open_id, logger=logger, channel='lark'):
                return
            if (
                not self._is_command_message(content)
                and self._ensure_report_receiver_global('lark', sender_name)
                and self._should_send_system_message()
            ):
                await self._send_text_reply(self._receiver_status_text('report', True), message_id)
            if hasattr(self.agent, 'handle_message'):
                self.agent.handle_message(
                    message=content,
                    history=history_content,
                    sender=sender_name,
                    channel='lark'
                )

        except Exception as e:
            logger.exception(f'Error processing Lark message: {e}')

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP"):
        """
        Add a reaction emoji to a message (non-blocking).

        Common emoji types: THUMBSUP, OK, EYES, DONE, OnIt, HEART
        """
        try:
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                ).build()

            response = self.client.im.v1.message_reaction.create(request)

            if not response.success():
                logger.debug(f"Failed to add reaction: code={response.code}, msg={response.msg}")
            else:
                logger.debug(f"Added {emoji_type} reaction to message {message_id}")
        except Exception as e:
            logger.debug(f"Error adding reaction: {e}")

    async def _handle_command(self, command: str, chat_id: str, message_id: str):
        """
        Handle bot commands from org_manager.

        Args:
            command: The command string (e.g., "/log_here")
            chat_id: The chat ID where the command was sent
            message_id: The message ID to reply to
        """
        try:
            if command.endswith("/log_here"):
                self.log_receiver = chat_id
                response_text = self._set_log_receiver_global('lark', f"group:{chat_id}")
                logger.info(f"Log receiver set to chat_id: {chat_id}")

            elif command.endswith("/stop_log_here"):
                self.log_receiver = None
                response_text = self._clear_log_receiver_global()
                logger.info("Log receiver cleared")

            elif command.endswith("/report_here"):
                self.report_receiver = chat_id
                response_text = self._set_report_receiver_global('lark', f"group:{chat_id}")
                logger.info(f"Report receiver set to chat_id: {chat_id}")

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

            # Send response as reply
            await self._send_text_reply(response_text, message_id)

        except Exception as e:
            logger.exception(f"Error handling command: {e}")

    async def _send_text_reply(self, text: str, message_id: str):
        """Send a text reply to a message."""
        try:
            card = self._build_card(text)
            card_content = json.dumps(card, ensure_ascii=False)

            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(card_content)
                    .msg_type("interactive")
                    .build()) \
                .build()

            response = self.client.im.v1.message.reply(request)

            if not response.success():
                logger.debug(f"Failed to send reply: {response.msg}")
        except Exception as e:
            logger.debug(f"Error sending text reply: {e}")

    def send_reply(self, content, previous_message):
        """Send a reply to a previous message."""
        try:
            # Build card with markdown support
            card = self._build_card(content)
            card_content = json.dumps(card, ensure_ascii=False)

            request = ReplyMessageRequest.builder() \
                .message_id(previous_message.message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(card_content)
                    .msg_type("interactive")
                    .build()) \
                .build()

            response = self.client.im.v1.message.reply(request)

            if not response.success():
                raise Exception(f'send_reply failed: {response.msg}')
        except Exception as e:
            logger.exception(f'Error sending reply: {e}', action='send_reply', status='failed')
            raise

    # Regex to match markdown tables (header + separator + data rows)
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    @staticmethod
    def _parse_md_table(table_text: str):
        """Parse a markdown table into a Feishu table element."""
        lines = [l.strip() for l in table_text.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            return None
        split = lambda l: [c.strip() for c in l.strip("|").split("|")]
        headers = split(lines[0])
        rows = [split(l) for l in lines[2:]]
        columns = [{"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
                   for i, h in enumerate(headers)]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [{f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows],
        }

    def _build_card_elements(self, content: str):
        """Split content into markdown + table elements for Feishu card."""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end:m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            table = self._parse_md_table(m.group(1))
            elements.append(table or {"tag": "markdown", "content": m.group(1)})
            last_end = m.end()
        remaining = content[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})
        return elements or [{"tag": "markdown", "content": content}]

    def _build_card(self, content: str):
        """Build a Feishu card with markdown + table support."""
        elements = self._build_card_elements(content)
        return {
            "config": {"wide_screen_mode": True},
            "elements": elements,
        }

    def _get_tenant_access_token(self):
        if self._tenant_access_token and time.time() < self._tenant_access_token_expire_at - 60:
            return self._tenant_access_token

        response = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self.agent.config.chat_lark_app_id,
                "app_secret": self.agent.config.chat_lark_app_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f'Failed to get Lark tenant access token: {data}')
        self._tenant_access_token = data["tenant_access_token"]
        self._tenant_access_token_expire_at = time.time() + int(data.get("expire", 7200))
        return self._tenant_access_token

    def _upload_image_via_rest(self, item):
        token = self._get_tenant_access_token()
        with open(item['abs_path'], 'rb') as file_obj:
            response = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (item['name'], file_obj, item['mime_type'])},
                timeout=60,
            )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f'Lark image upload failed: {data}')
        image_key = data["data"]["image_key"]
        self._resource_path_cache[image_key] = self._relative_to_agent_dir(item['abs_path'])
        return image_key

    def _upload_file_via_rest(self, item):
        token = self._get_tenant_access_token()
        with open(item['abs_path'], 'rb') as file_obj:
            response = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/files",
                headers={"Authorization": f"Bearer {token}"},
                data={"file_type": "stream", "file_name": item['name']},
                files={"file": (item['name'], file_obj, item['mime_type'])},
                timeout=60,
            )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f'Lark file upload failed: {data}')
        file_key = data["data"]["file_key"]
        self._resource_path_cache[file_key] = self._relative_to_agent_dir(item['abs_path'])
        return file_key

    def _send_media_via_rest(self, receive_id_type, receive_id, item):
        token = self._get_tenant_access_token()
        if item['message_type'] == 'image':
            msg_type = 'image'
            content = {"image_key": self._upload_image_via_rest(item)}
        else:
            msg_type = 'file'
            content = {"file_key": self._upload_file_via_rest(item)}

        response = requests.post(
            f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise RuntimeError(f'Lark send media failed: {data}')
        return data

    def _download_resource_via_rest(self, message_id, file_key, resource_type, filename):
        token = self._get_tenant_access_token()
        response = requests.get(
            f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        response.raise_for_status()
        local_path = self._save_incoming_media_bytes('lark', filename, response.content)
        self._resource_path_cache[file_key] = self._relative_to_agent_dir(local_path)
        return local_path

    def _normalize_rich_content(self, parsed, message_id=None):
        if not isinstance(parsed, dict):
            return None

        rows = parsed.get("content")
        if not isinstance(rows, list):
            return None

        normalized = dict(parsed)
        normalized_rows = []

        for row in rows:
            if not isinstance(row, list):
                normalized_rows.append(row)
                continue

            normalized_row = []
            for element in row:
                if not isinstance(element, dict):
                    normalized_row.append(element)
                    continue

                tag = element.get("tag")
                normalized_element = dict(element)

                if tag == "img":
                    image_key = element.get("image_key")
                    image_path = self._resource_path_cache.get(image_key) if image_key else None
                    if image_key and image_path is None and message_id:
                        local_path = self._download_resource_via_rest(
                            message_id,
                            image_key,
                            "image",
                            f"{image_key}.png",
                        )
                        image_path = self._relative_to_agent_dir(local_path)
                    if image_path:
                        normalized_element["image_path"] = image_path

                elif tag == "file":
                    file_key = element.get("file_key")
                    file_name = element.get("file_name") or f"{file_key}.bin"
                    file_path = self._resource_path_cache.get(file_key) if file_key else None
                    if file_key and file_path is None and message_id:
                        local_path = self._download_resource_via_rest(
                            message_id,
                            file_key,
                            "file",
                            file_name,
                        )
                        file_path = self._relative_to_agent_dir(local_path)
                    if file_path:
                        normalized_element["file_path"] = file_path

                normalized_row.append(normalized_element)

            normalized_rows.append(normalized_row)

        normalized["content"] = normalized_rows
        return json.dumps(normalized, ensure_ascii=False)

    def _normalize_history_content(self, message_type, raw_content, message_id=None):
        try:
            parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        except json.JSONDecodeError:
            return raw_content

        rich_content = self._normalize_rich_content(parsed, message_id=message_id)
        if rich_content is not None:
            return rich_content

        if message_type == "image" and isinstance(parsed, dict):
            image_key = parsed.get("image_key")
            if image_key and image_key in self._resource_path_cache:
                return json.dumps({
                    "image_key": image_key,
                    "image_path": self._resource_path_cache[image_key],
                }, ensure_ascii=False)

        if message_type == "file" and isinstance(parsed, dict):
            file_key = parsed.get("file_key")
            if file_key and file_key in self._resource_path_cache:
                normalized = {"file_key": file_key, "file_path": self._resource_path_cache[file_key]}
                if parsed.get("file_name"):
                    normalized["file_name"] = parsed["file_name"]
                return json.dumps(normalized, ensure_ascii=False)

        if message_type == "interactive" and isinstance(parsed, dict):
            if parsed.get("title") is None:
                elements = parsed.get("elements")
                if (
                    isinstance(elements, list)
                    and len(elements) == 1
                    and isinstance(elements[0], list)
                    and len(elements[0]) == 1
                    and isinstance(elements[0][0], dict)
                    and elements[0][0].get("tag") == "text"
                ):
                    return json.dumps({"text": elements[0][0].get("text", "")}, ensure_ascii=False)

        return json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, (dict, list)) else raw_content
    
    def send_message(self, message, receiver=None, _type=None):
        """
        Send a message to receiver.

        Args:
            message: Can be a string, an image/file (represented as a path) or a list of them
            receiver: Name/id of the message receiver (can be a user or a group/chat)
                     Use "group:" prefix for group messages (e.g., "group:chat_id")
            subject: Subject/topic for group messages (optional)
        """
        try:
            manager_receiver = self._manager_receiver(self.org_manager_open_id)
            if manager_receiver is not None:
                receiver = manager_receiver
            if receiver is None:
                # Use report_receiver if set, otherwise default to org_manager
                if self.report_receiver:
                    report_receiver = str(self.report_receiver)
                    if report_receiver.startswith("group:"):
                        receiver = report_receiver
                    else:
                        receiver = f"group:{report_receiver}"
                else:
                    receiver = self.org_manager_open_id

            if not receiver:
                logger.warning(
                    'send_message failed. receiver unavailable',
                    action='send_message',
                    status='failed',
                )
                return None

            # Check if receiver has "group:" prefix
            if receiver.startswith("group:"):
                # Group message - remove the prefix
                chat_id = receiver[6:]  # Remove "group:" prefix
                receive_id_type = "chat_id"
                receive_id = chat_id
            else:
                # Private message to user
                # Get open_id if receiver is a name/id, otherwise use receiver as open_id
                receive_id = self._user_mapping.get(receiver, receiver)
                receive_id_type = "open_id"

            responses = []
            for item in self._normalize_outgoing_message(message):
                if item['kind'] == 'text':
                    card = self._build_card(item['text'])
                    msg_content = json.dumps(card, ensure_ascii=False)
                    request = CreateMessageRequest.builder() \
                        .receive_id_type(receive_id_type) \
                        .request_body(CreateMessageRequestBody.builder()
                            .receive_id(receive_id)
                            .msg_type("interactive")
                            .content(msg_content)
                            .build()) \
                        .build()

                    response = self.client.im.v1.message.create(request)

                    if not response.success():
                        err_msg = ''
                        if 'Invalid' in response.msg:
                            err_msg = 'The `receiver` param should either be a user open_id or a chat_id with "group:" prefix'
                        logger.error(
                            f"Failed to send Lark message: code={response.code}, "
                            f"msg={response.msg}, log_id={response.get_log_id()}. {err_msg}"
                        )
                        raise Exception(f'send_message failed: {response.msg}. {err_msg}')
                    responses.append(response)
                else:
                    responses.append(self._send_media_via_rest(receive_id_type, receive_id, item))

            return responses[-1] if responses else None

        except Exception as e:
            logger.exception(f'Error sending message: {e}', action='send_message', status='failed')
            raise

    def get_history_messages(self, msg, max_previous_messages=10):
        """
        Get history messages from a conversation.

        Args:
            msg: The current message object
            max_previous_messages: Maximum number of previous messages to retrieve

        Returns:
            List of tuples (sender_name, content, timestamp)
        """
        try:
            chat_id = msg.chat_id

            # Get message history
            request = ListMessageRequest.builder() \
                .container_id_type("chat") \
                .sort_type("ByCreateTimeDesc") \
                .container_id(chat_id) \
                .page_size(max_previous_messages) \
                .build()

            response = self.client.im.v1.message.list(request)

            messages_to_return = []
            if response.success():
                for message in response.data.items:
                    # Convert timestamp to human-readable format
                    from datetime import datetime
                    timestamp = datetime.fromtimestamp(int(message.create_time) / 1000).strftime('%Y-%m-%d %H:%M:%S')
                    sender_name = "unknown"
                    if getattr(message, "sender", None) and getattr(message.sender, "id", None):
                        sender_name = message.sender.id

                    message_type = getattr(message, "msg_type", None) or getattr(message, "message_type", None)
                    raw_content = None
                    if getattr(message, "body", None) is not None:
                        raw_content = getattr(message.body, "content", None)
                    normalized_content = self._normalize_history_content(message_type, raw_content, message.message_id)

                    if normalized_content is None:
                        normalized_content = raw_content
                    if normalized_content is None:
                        normalized_content = ""

                    content = normalized_content
                    messages_to_return.append((sender_name, content, timestamp))
            else:
                raise Exception(f'{response.code}: {response.msg}')

            return messages_to_return

        except Exception as e:
            logger.exception(f'get_history_messages failed: {e}')
            return []

    def _check_whether_chat_exists(self, chat_id):
        """
        Check whether a Lark chat with the given ID exists.

        Args:
            chat_id: ID of the chat to check

        Returns:
            True if the chat exists, False otherwise
        """
        try:
            request = GetChatRequest.builder() \
                .chat_id(chat_id) \
                .build()

            response = self.client.im.v1.chat.get(request)

            if response.success():
                return True
            return False

        except Exception as e:
            logger.debug(f'Chat {chat_id} does not exist: {e}',
                        action='_check_whether_chat_exists', status='not_found')
            return False

    def _create_chat(self, chat_name, description=""):
        """
        Creates a Lark group chat with the given name.
        Adds the agent and the bound user as members.

        Args:
            chat_name: Name of the chat to create
            description: Optional description for the chat

        Returns:
            Chat ID of the created chat
        """
        try:
            # Get the user's open_id from org_manager
            org_manager_open_id = self.org_manager_open_id

            # Create user list (only real users, not bots)
            user_id_list = []
            if org_manager_open_id:
                user_id_list.append(org_manager_open_id)

            # Create bot list (bots use app_id with "cli_" prefix)
            bot_id_list = [f"{self.agent.config.chat_lark_app_id}"]

            # Create the chat
            request = CreateChatRequest.builder() \
                .user_id_type("open_id") \
                .set_bot_manager(False) \
                .request_body(CreateChatRequestBody.builder()
                    .name(chat_name)
                    .description(description)
                    .owner_id(org_manager_open_id)
                    .user_id_list(user_id_list)
                    .bot_id_list(bot_id_list)
                    .build()) \
                .build()

            response = self.client.im.v1.chat.create(request)

            if response.success():
                chat_id = response.data.chat_id
                logger.debug(f'Chat created: {chat_name}', action='create_chat', status='success')
                return chat_id
            else:
                raise Exception(f'Failed to create chat: {response.msg}')

        except Exception as e:
            logger.exception(f'Failed to create chat {chat_name}: {e}', action='create_chat', status='failed')
            raise

    def _send_to_chat(self, chat_name, message, _type=None, description=None):
        """
        Helper method to send a message to a chat, creating it if it doesn't exist.

        Args:
            chat_name: Name of the chat
            message: Message content to send
            subject: Subject/topic for the message (optional)
            description: Description for the chat if it needs to be created
        """
        # Check if we have a cached chat_id for this chat_name
        chat_id = self._group_mapping.get(chat_name)

        # If not cached or doesn't exist, create the chat
        if not chat_id or not self._check_whether_chat_exists(chat_id):
            logger.debug(f'Chat does not exist, creating: {chat_name}')
            try:
                chat_description = description or f"Chat for {chat_name}"
                chat_id = self._create_chat(chat_name, description=chat_description)
                self._group_mapping[chat_name] = chat_id
            except Exception as e:
                logger.exception(f'Failed to create chat: {e}')
                raise

        # Send the message
        try:
            self.send_message(message, receiver=f'group:{chat_id}', _type=_type)
            logger.debug(f'Message sent to chat: {chat_name}', action='_send_to_chat', status='success')
        except Exception as e:
            logger.exception(f'Failed to send message to chat {chat_name}: {e}')
            raise
