"""
The interfaces to chat with users or other agents.
"""
from mobileclaw.utils.interface import UniInterface

class Chat_Interface(UniInterface):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat'
        self.chat_channels = agent.config.chat_channels.split(',')
        self.chat_clients = {}
        self.default_chat_channel = agent.config.default_chat_channel
        self.log_receiver = None
        self.report_receiver = None

    def __str__(self) -> str:
        return "Chat interface"

    def _open(self):
        if 'zulip' in self.chat_channels:
            from .zulip_client import Zulip_Client
            self.zulip_client = Zulip_Client(self.agent)
            self.zulip_client._open()
            self.chat_clients['zulip'] = self.zulip_client

        if 'lark' in self.chat_channels:
            from .lark_client import Lark_Client
            self.lark_client = Lark_Client(self.agent)
            self.lark_client._open()
            self.chat_clients['lark'] = self.lark_client

        if 'qq' in self.chat_channels:
            from .qq_client import QQ_Client
            self.qq_client = QQ_Client(self.agent)
            self.qq_client._open()
            self.chat_clients['qq'] = self.qq_client

        if 'telegram' in self.chat_channels:
            from .telegram_client import Telegram_Client
            self.telegram_client = Telegram_Client(self.agent)
            self.telegram_client._open()
            self.chat_clients['telegram'] = self.telegram_client

        if 'discord' in self.chat_channels:
            from .discord_client import Discord_Client
            self.discord_client = Discord_Client(self.agent)
            self.discord_client._open()
            self.chat_clients['discord'] = self.discord_client

        if 'whatsapp' in self.chat_channels:
            from .whatsapp_client import WhatsApp_Client
            self.whatsapp_client = WhatsApp_Client(self.agent)
            self.whatsapp_client._open()
            self.chat_clients['whatsapp'] = self.whatsapp_client

        if 'slack' in self.chat_channels:
            from .slack_client import Slack_Client
            self.slack_client = Slack_Client(self.agent)
            self.slack_client._open()
            self.chat_clients['slack'] = self.slack_client

        if 'weixin' in self.chat_channels:
            from .weixin_client import Weixin_Client
            self.weixin_client = Weixin_Client(self.agent)
            self.weixin_client._open()
            self.chat_clients['weixin'] = self.weixin_client

    def _close(self):
        for client in self.chat_clients.values():
            if client is not None:
                client._close()

    def _get_client(self, channel=None):
        """
        Get the chat client for the specified channel.
        If no channel is specified, use the default channel.

        Args:
            channel: Channel name (e.g., 'zulip'). If None, uses default_chat_channel.

        Returns:
            The chat client for the specified channel, or None if not available.
        """
        if channel is None:
            channel = self.default_chat_channel
        channel = channel.lower()
        if channel not in self.chat_channels:
            raise Exception(f'Unknown channel: {channel}; Should be one of {self.chat_channels}')
        return self.chat_clients.get(channel)

    def set_log_receiver(self, channel, receiver):
        self.log_receiver = {
            'channel': str(channel),
            'receiver': receiver,
        }

    def clear_log_receiver(self):
        self.log_receiver = None

    def set_report_receiver(self, channel, receiver):
        self.report_receiver = {
            'channel': str(channel),
            'receiver': receiver,
        }

    def clear_report_receiver(self):
        self.report_receiver = None

    def ensure_report_receiver(self, channel, receiver):
        if self.report_receiver is not None:
            return False
        self.set_report_receiver(channel, receiver)
        return True

    def send_reply(self, message, previous_message, channel=None):
        """
        Send a reply to a previous message.

        Args:
            message: Reply message content
            previous_message: The previous message to reply to
            channel: Channel to use (optional, defaults to default_chat_channel)
        """
        client = self._get_client(channel)
        if client is not None and hasattr(client, 'send_reply'):
            client.send_reply(message, previous_message)

    def send_to_org(self, message, subject="General", channel=None):
        """
        Send a message to the organization.

        Args:
            message: Message content to send
            subject: Subject/topic for the message
            channel: Channel to use (optional, defaults to default_chat_channel)
        """
        client = self._get_client(channel)
        if client is not None and hasattr(client, 'send_to_org'):
            client.send_to_org(message, subject)

    def send_to_log(self, message, subject="Log", channel=None):
        """
        Send a message to the agent's self-reporting stream.

        Args:
            message: Message content to send
            subject: Subject/topic for the message
            channel: Channel to use (optional, defaults to log_channel or default_chat_channel)
        """
        if channel is None:
            if self.log_receiver is None:
                return
            channel = self.log_receiver['channel']
            receiver = self.log_receiver['receiver']
        else:
            receiver = None
        client = self._get_client(channel)
        if client is not None and hasattr(client, 'send_message'):
            client.send_message(message, receiver=receiver)

    def send_message(self, message, receiver=None, channel=None):
        """
        Send a message to the receiver.

        Args:
            message: Can be a string, an image/file (represented as a path) or a list of them
            receiver: Name/id of the message receiver (can be a user or a group)
            channel: Channel to use (optional, defaults to report_channel or default_chat_channel)
        """
        if channel is None and receiver is None:
            if self.report_receiver is None:
                return
            channel = self.report_receiver['channel']
            receiver = self.report_receiver['receiver']
        client = self._get_client(channel)
        if client is not None and hasattr(client, 'send_message'):
            client.send_message(message, receiver)
