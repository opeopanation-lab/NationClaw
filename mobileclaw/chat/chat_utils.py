"""
The interfaces to chat with users or other agents.
"""
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

    def _set_org_manager_if_missing(self, local_attr_name, config_attr_name, sender):
        """Bind the first valid sender as org_manager when it is not configured."""
        if not sender:
            return

        current_org_manager = getattr(self, local_attr_name, None)
        if current_org_manager:
            return

        setattr(self, local_attr_name, sender)
        if hasattr(self.agent, "config"):
            setattr(self.agent.config, config_attr_name, sender)

        logger.info(
            "org_manager auto-bound from first valid message",
            channel=self._tag,
            org_manager=sender,
            config_key=config_attr_name,
        )

