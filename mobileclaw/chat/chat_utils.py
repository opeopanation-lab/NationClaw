"""
The interfaces to chat with users or other agents.
"""
import mimetypes
import os
import structlog

from mobileclaw.utils.interface import UniInterface
from mobileclaw.utils import debug

logger = structlog.get_logger(__name__)


class Chat_Message:
    def __init__(self, content=None, timestamp=None, sender=None, recipient=None, **kwargs):
        self.content = content
        self.timestamp = timestamp
        self.sender = sender
        self.recipient = recipient


class Chat_Handler(UniInterface):
    def __init__(self, agent):
        super().__init__(agent)
    
    def _handle_message(self, message_in):
        debug.print_method_name_with_message('not implemented')


class Chat_Client(UniInterface):
    """
    Each chat client should implement:
    - `_send` method, which sends a message
    """
    def __init__(self, agent):
        super().__init__(agent)
        self._tag = 'chat.client'
        self.chat_with_manager_only = bool(getattr(self.config, 'chat_with_manager_only', False))

    def _manager_only_enabled(self):
        return self.chat_with_manager_only

    @staticmethod
    def _normalize_sender_id(sender):
        if sender is None:
            return None
        sender = str(sender)
        return sender.split('|', 1)[0]

    def _is_manager_sender(self, sender, manager_id):
        if sender is None or manager_id in (None, ''):
            return False

        sender = str(sender)
        manager_id = str(manager_id)
        return (
            sender == manager_id
            or self._normalize_sender_id(sender) == self._normalize_sender_id(manager_id)
        )

    def _should_handle_incoming(self, sender, manager_id, logger=None, channel=None):
        if not self._manager_only_enabled():
            return True
        if manager_id in (None, '', '?'):
            return True
        if self._is_manager_sender(sender, manager_id):
            return True
        if logger is not None:
            logger.info(
                'Ignoring non-manager message because chat_with_manager_only is enabled',
                sender=sender,
                manager_id=manager_id,
                channel=channel,
            )
        return False

    def _manager_receiver(self, manager_id):
        if not self._manager_only_enabled():
            return None
        return manager_id

    @staticmethod
    def _is_command_message(content):
        if content is None:
            return False
        return str(content).lstrip().startswith('/')

    def _resolve_local_attachment_path(self, file_path):
        if not file_path:
            return None, None

        file_path = str(file_path)
        if os.path.isabs(file_path):
            abs_path = os.path.realpath(file_path)
        else:
            abs_path = os.path.realpath(os.path.join(self.agent.file.agent_dir, file_path))

        try:
            rel_path = os.path.relpath(abs_path, self.agent.file.agent_dir)
        except Exception:
            rel_path = abs_path

        return abs_path, rel_path

    def _normalize_outgoing_message(self, message):
        items = message if isinstance(message, list) else [message]
        normalized = []

        for item in items:
            if item is None:
                continue

            if isinstance(item, tuple) and len(item) >= 2:
                message_type = str(item[0]).lower()
                abs_path, rel_path = self._resolve_local_attachment_path(item[1])
                if not abs_path or not os.path.exists(abs_path):
                    logger.warning(
                        'Outgoing attachment path not found',
                        channel=self._tag,
                        message_type=message_type,
                        file_path=item[1],
                    )
                    normalized.append({
                        'kind': 'text',
                        'text': f'[{message_type}: {item[1]} - not found]',
                    })
                    continue

                mime_type, _ = mimetypes.guess_type(abs_path)
                normalized.append({
                    'kind': 'attachment',
                    'message_type': message_type,
                    'abs_path': abs_path,
                    'rel_path': rel_path,
                    'name': os.path.basename(abs_path),
                    'mime_type': mime_type or 'application/octet-stream',
                })
                continue

            normalized.append({
                'kind': 'text',
                'text': str(item),
            })

        return normalized

    def _message_to_plain_text(self, message):
        parts = []
        for item in self._normalize_outgoing_message(message):
            if item['kind'] == 'text':
                parts.append(item['text'])
            else:
                parts.append(f"[{item['message_type']}: {item['rel_path']}]")
        return "\n".join([part for part in parts if part])

    def _incoming_media_dir(self, channel):
        media_dir = os.path.join(self.agent.file.agent_temp_dir, 'chat', channel)
        os.makedirs(media_dir, exist_ok=True)
        return media_dir

    def _save_incoming_media_bytes(self, channel, filename, content):
        media_dir = self._incoming_media_dir(channel)
        safe_name = str(filename or 'attachment').replace('/', '_')
        file_path = os.path.join(media_dir, safe_name)
        with open(file_path, 'wb') as f:
            f.write(content)
        return file_path

    def _relative_to_agent_dir(self, file_path):
        return os.path.relpath(file_path, self.agent.file.agent_dir)

    def _format_incoming_attachment_ref(self, attachment_type, file_path):
        return f'[{attachment_type}: {self._relative_to_agent_dir(file_path)}]'

    @staticmethod
    def _receiver_status_text(kind, enabled):
        if kind == 'log':
            return (
                "✅ Log receiver set. Logs will be sent to you. Reply /stop_log_here to cancel."
                if enabled else
                "✅ Log receiver cleared. Logs will no longer be sent here."
            )
        return (
            "✅ Report receiver set. Progress will be sent to you. Reply /stop_report_here to cancel."
            if enabled else
            "✅ Report receiver cleared. Progress will no longer be sent here."
        )

    @staticmethod
    def _org_manager_status_text():
        return "✅ Manager set to you."

    def _set_log_receiver_global(self, channel, receiver):
        self.agent.chat.set_log_receiver(channel, receiver)
        return self._receiver_status_text('log', True)

    def _clear_log_receiver_global(self):
        self.agent.chat.clear_log_receiver()
        return self._receiver_status_text('log', False)

    def _set_report_receiver_global(self, channel, receiver):
        self.agent.chat.set_report_receiver(channel, receiver)
        return self._receiver_status_text('report', True)

    def _clear_report_receiver_global(self):
        self.agent.chat.clear_report_receiver()
        return self._receiver_status_text('report', False)

    def _ensure_report_receiver_global(self, channel, receiver):
        return self.agent.chat.ensure_report_receiver(channel, receiver)

    def _available_system_commands_text(self):
        return (
            "/log_here - Set this chat as log receiver\n"
            "/stop_log_here - Stop sending logs to this chat\n"
            "/report_here - Set this chat as report receiver\n"
            "/stop_report_here - Stop sending reports to this chat"
        )

    def _history_system_messages(self):
        return {
            self._receiver_status_text('log', True),
            self._receiver_status_text('log', False),
            self._receiver_status_text('report', True),
            self._receiver_status_text('report', False),
            self._org_manager_status_text(),
            self._available_system_commands_text(),
        }

    def _should_skip_history(self, content):
        if content is None:
            return True

        text = str(content).strip()
        if not text:
            return True

        if text in self._history_system_messages():
            return True

        command_prefixes = (
            '/log_here',
            '/stop_log_here',
            '/report_here',
            '/stop_report_here',
            '/help',
        )
        return any(text.startswith(prefix) for prefix in command_prefixes)

    def _set_org_manager_if_missing(self, local_attr_name, config_attr_name, sender):
        """Bind the first valid sender as org_manager when it is not configured."""
        if not sender:
            return False

        current_org_manager = getattr(self, local_attr_name, None)
        if current_org_manager:
            return False

        setattr(self, local_attr_name, sender)
        if hasattr(self.agent, "config"):
            setattr(self.agent.config, config_attr_name, sender)

        logger.info(
            "org_manager auto-bound from first valid message",
            channel=self._tag,
            org_manager=sender,
            config_key=config_attr_name,
        )
        return True
