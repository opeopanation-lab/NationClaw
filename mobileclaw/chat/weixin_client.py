"""
The Weixin chat client implementation using the iLink HTTP API.
"""
import base64
import os
import random
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import requests
import structlog

from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)


class Weixin_Client(Chat_Client):
    def __init__(self, agent):
        super().__init__(agent)
        from mobileclaw.agent import AutoAgent
        assert isinstance(agent, AutoAgent)
        self._tag = 'chat.client.weixin'
        self.org_manager_user_id = self.agent.config.chat_weixin_org_manager
        self.base_url = (self.agent.config.chat_weixin_base_url or 'https://ilinkai.weixin.qq.com').rstrip('/')
        self.bot_token = self.agent.config.chat_weixin_bot_token
        self._serving_thread = None
        self._stop_serving = False
        self._auth_session = requests.Session()
        self._poll_session = requests.Session()
        self._send_session = requests.Session()
        self._get_updates_buf = ''
        self._longpolling_timeout_ms = 35000
        self._routes = {}
        self._route_lock = threading.Lock()
        self._history = defaultdict(lambda: deque(maxlen=20))
        self._history_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.log_receiver = None
        self.report_receiver = None

    def _open(self):
        try:
            logger.debug('Weixin_Client starting')
            self._serving_thread = threading.Thread(target=self._start_serving, daemon=True)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'Weixin_Client not started: {e}')

    def _close(self):
        self._stop_serving = True
        if self._serving_thread is not None:
            self._serving_thread.join(timeout=5)
        for session in (self._auth_session, self._poll_session, self._send_session):
            try:
                session.close()
            except Exception:
                pass

    def _start_serving(self):
        while not self._stop_serving:
            try:
                if not self.bot_token:
                    self._login_via_qrcode()
                    if not self.bot_token:
                        time.sleep(5)
                        continue
                self._poll_updates_forever()
            except Exception as e:
                logger.exception(f'Weixin serving loop error: {e}')
                time.sleep(5)

    def _login_via_qrcode(self):
        logger.info('Weixin bot token missing, starting QR login flow')
        try:
            response = self._auth_session.get(
                f'{self.base_url}/ilink/bot/get_bot_qrcode',
                params={'bot_type': 3},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f'Failed to get Weixin QR code: {e}')
            return

        qrcode = data.get('qrcode')
        qrcode_url = data.get('qrcode_img_content')
        msg = f'Weixin QR code ready. Scan with Weixin now to continue login. qrcode={qrcode}, qrcode_url={qrcode_url}'
        self.agent.chat.send_to_log(msg)
        logger.info(f'\n========\n\n{msg}\n\n========\n')

        while not self._stop_serving:
            try:
                response = self._auth_session.get(
                    f'{self.base_url}/ilink/bot/get_qrcode_status',
                    params={'qrcode': qrcode},
                    timeout=40,
                )
                response.raise_for_status()
                status_data = response.json()
            except Exception as e:
                logger.warning(f'Failed to query Weixin QR status: {e}')
                time.sleep(2)
                continue

            status = status_data.get('status')
            if status == 'confirmed':
                self.bot_token = status_data.get('bot_token')
                confirmed_base_url = (status_data.get('baseurl') or self.base_url).rstrip('/')
                self.base_url = confirmed_base_url
                if hasattr(self.agent.config, 'chat_weixin_bot_token'):
                    self.agent.config.chat_weixin_bot_token = self.bot_token
                if hasattr(self.agent.config, 'chat_weixin_base_url'):
                    self.agent.config.chat_weixin_base_url = self.base_url
                msg = f'Weixin QR login confirmed. token={self.bot_token}, base_url={self.base_url}'
                self.agent.chat.send_to_log(msg)
                return

            if status in ('expired', 'canceled', 'cancelled'):
                self.agent.chat.send_to_log(f'Weixin QR login stopped with status: {status}')
                return

            time.sleep(1)

    def _build_headers(self):
        headers = {
            'Content-Type': 'application/json',
            'AuthorizationType': 'ilink_bot_token',
            'X-WECHAT-UIN': base64.b64encode(str(random.getrandbits(32)).encode('utf-8')).decode('utf-8'),
        }
        if self.bot_token:
            headers['Authorization'] = f'Bearer {self.bot_token}'
        return headers

    def _api_post(self, path, payload, timeout=None, session=None):
        session = session or self._send_session
        response = session.post(
            f'{self.base_url}/{path.lstrip("/")}',
            json=payload,
            headers=self._build_headers(),
            timeout=timeout or 45,
        )
        response.raise_for_status()
        data = response.json()
        ret = data.get('ret', 0)
        if ret not in (0, None):
            raise RuntimeError(f'Weixin API failed: path={path}, ret={ret}, body={data}')
        return data

    @staticmethod
    def _extract_response_context_token(data):
        if not isinstance(data, dict):
            return None

        direct_token = data.get('context_token')
        if direct_token:
            return direct_token

        for key in ('msg', 'message', 'data', 'result'):
            nested = data.get(key)
            if isinstance(nested, dict):
                nested_token = nested.get('context_token')
                if nested_token:
                    return nested_token

        return None

    def _poll_updates_forever(self):
        while not self._stop_serving and self.bot_token:
            timeout_sec = max(int(self._longpolling_timeout_ms / 1000), 35)
            data = self._api_post(
                'ilink/bot/getupdates',
                {
                    'get_updates_buf': self._get_updates_buf,
                    'base_info': {'channel_version': '1.0.2'},
                },
                timeout=timeout_sec,
                session=self._poll_session,
            )

            new_buf = data.get('get_updates_buf')
            if new_buf is not None:
                self._get_updates_buf = new_buf
            self._longpolling_timeout_ms = data.get('longpolling_timeout_ms') or self._longpolling_timeout_ms

            for message in data.get('msgs') or []:
                try:
                    self._handle_inbound_message(message)
                except Exception as e:
                    logger.exception(f'Failed to handle Weixin inbound message: {e}')

    def _handle_inbound_message(self, message):
        from_user_id = message.get('from_user_id')
        if not from_user_id:
            return

        self._set_org_manager_if_missing(from_user_id)
        if not self._should_handle_incoming(from_user_id, self.org_manager_user_id, logger=logger, channel='weixin'):
            return

        group_id = message.get('group_id')
        receiver_key = f'group:{group_id}' if group_id else from_user_id
        text = self._extract_text(message)
        if not text:
            logger.debug('Skipping unsupported Weixin message without text content')
            return

        self._remember_route(receiver_key, message)
        history_messages = self.get_history_messages(receiver_key)
        history = "\n".join([f'[{m[2]}] {m[0]}: {m[1]}' for m in history_messages])
        sender_name = receiver_key
        self._append_history(receiver_key, from_user_id, text)

        if text.startswith('/'):
            if from_user_id == self.org_manager_user_id:
                self._handle_command(text.strip(), receiver_key)
            return

        if hasattr(self.agent, 'handle_message'):
            self.agent.handle_message(
                message=text,
                history=history,
                sender=sender_name,
                channel='weixin',
            )

    def _extract_text(self, message):
        for item in message.get('item_list') or []:
            if item.get('type') == 1:
                return ((item.get('text_item') or {}).get('text') or '').strip()
        return ''

    def _set_org_manager_if_missing(self, sender_id):
        if self.org_manager_user_id not in (None, '', '?'):
            return
        self.org_manager_user_id = sender_id
        if hasattr(self.agent.config, 'chat_weixin_org_manager'):
            self.agent.config.chat_weixin_org_manager = sender_id
        logger.info(f'Weixin org manager initialized to {sender_id}')

    def _remember_route(self, receiver_key, message):
        route = {
            'receiver_key': receiver_key,
            'to_user_id': message.get('from_user_id'),
            'context_token': message.get('context_token'),
            'group_id': message.get('group_id'),
        }
        with self._route_lock:
            self._routes[receiver_key] = route
            from_user_id = message.get('from_user_id')
            if from_user_id:
                self._routes[from_user_id] = route

    def _update_route_context_token(self, receiver, context_token):
        if not receiver or not context_token:
            return
        with self._route_lock:
            route = self._routes.get(str(receiver))
            if route is None:
                return
            route['context_token'] = context_token

    def _resolve_route(self, receiver):
        if receiver is None:
            return None
        receiver = str(receiver)
        with self._route_lock:
            return self._routes.get(receiver)

    def _handle_command(self, command, receiver_key):
        response_text = None
        if command == '/log_here':
            self.log_receiver = receiver_key
            self.agent.chat.log_channel = 'weixin'
            response_text = '✅ Log receiver set. Logs will be sent here.'
        elif command == '/stop_log_here':
            self.log_receiver = None
            if self.agent.chat.log_channel == 'weixin':
                self.agent.chat.log_channel = None
            response_text = '✅ Log receiver cleared.'
        elif command == '/report_here':
            self.report_receiver = receiver_key
            self.agent.chat.report_channel = 'weixin'
            response_text = '✅ Report receiver set. Progress reports will be sent here.'
        elif command == '/stop_report_here':
            self.report_receiver = None
            if self.agent.chat.report_channel == 'weixin':
                self.agent.chat.report_channel = None
            response_text = '✅ Report receiver cleared. Reports will be sent to org_manager.'

        if response_text:
            self.send_message(response_text, receiver=receiver_key)

    def send_message(self, message, receiver=None, subject=None):
        if not self.bot_token:
            logger.warning('Send_message failed because weixin client not logged in')
            return
        manager_receiver = self._manager_receiver(self.org_manager_user_id)
        if manager_receiver is not None:
            receiver = manager_receiver

        if receiver is None:
            if self.report_receiver:
                receiver = self.report_receiver
            else:
                receiver = self.org_manager_user_id

        if not receiver:
            logger.warning('No receiver specified for Weixin message')
            return

        route = self._resolve_route(receiver)
        if not route:
            logger.warning(f'send_message failed. route unavailable for {receiver}')
            return

        if not route.get('context_token'):
            logger.warning(f'send_message failed. context_token unavailable for {receiver}')
            return

        payload = {
            'msg': {
                'from_user_id': '',
                'to_user_id': route['receiver_key'],
                'client_id': self._generate_client_id(),
                'message_type': 2,
                'message_state': 2,
                'context_token': route['context_token'],
                'item_list': [
                    {
                        'type': 1,
                        'text_item': {'text': str(message)},
                    }
                ],
            },
            'base_info': {'channel_version': '1.0.2'},
        }

        try:
            with self._send_lock:
                result = self._api_post(
                    'ilink/bot/sendmessage',
                    payload,
                    timeout=30,
                    session=self._send_session,
                )
            logger.info('Weixin sendmessage response', receiver=receiver, response=result)
            new_context_token = self._extract_response_context_token(result)
            if new_context_token:
                self._update_route_context_token(receiver, new_context_token)
            self._append_history(route['receiver_key'], 'assistant', str(message))
        except Exception as e:
            logger.exception(f'Error sending Weixin message: {e}')

    def send_reply(self, content, previous_message):
        sender = getattr(previous_message, 'sender', None)
        if sender:
            self.send_message(content, receiver=sender)
        else:
            logger.warning('Cannot reply: no sender in previous message')

    def send_to_org(self, message, subject='General'):
        if self.org_manager_user_id:
            self.send_message(message, receiver=self.org_manager_user_id, subject=subject)
        else:
            logger.warning('No org manager configured for Weixin')

    def send_to_log(self, message, subject='Log'):
        if self._manager_only_enabled() and self.org_manager_user_id:
            self.send_message(message, receiver=self.org_manager_user_id, subject=subject)
            return
        if self.log_receiver is None:
            logger.debug('No log receiver set, skipping send_to_log')
            return
        try:
            self.send_message(message, receiver=self.log_receiver, subject=subject)
        except Exception as e:
            logger.exception(f'Error sending to log receiver: {e}')

    def get_history_messages(self, receiver_key, max_previous_messages=10):
        with self._history_lock:
            history = list(self._history.get(str(receiver_key), []))
        return history[-max_previous_messages:]

    def _append_history(self, receiver_key, sender, content):
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with self._history_lock:
            self._history[str(receiver_key)].append((str(sender), str(content), timestamp))

    def _generate_client_id(self):
        return f'mobileclaw-weixin:{int(time.time() * 1000)}-{os.urandom(4).hex()}'
