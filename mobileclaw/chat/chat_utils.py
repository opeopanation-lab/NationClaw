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
