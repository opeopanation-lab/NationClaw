"""
The Weixin chat client implementation using the iLink HTTP API.
"""
import base64
import hashlib
import json
import os
import random
import struct
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

import requests
import structlog
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from .chat_utils import Chat_Client

logger = structlog.get_logger(__name__)

WEIXIN_CDN_BASE_URL = 'https://novac2c.cdn.weixin.qq.com/c2c'
WEIXIN_CHANNEL_VERSION = '2.0.0'
WEIXIN_MSG_ITEM_TEXT = 1
WEIXIN_MSG_ITEM_IMAGE = 2
WEIXIN_MSG_ITEM_VOICE = 3
WEIXIN_MSG_ITEM_FILE = 4
WEIXIN_MEDIA_TYPE_IMAGE = 1
WEIXIN_MEDIA_TYPE_VIDEO = 2
WEIXIN_MEDIA_TYPE_FILE = 3
WEIXIN_MEDIA_TYPE_VOICE = 4


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
        self._route_cache_path = os.path.join(self.agent.file.agent_temp_dir, 'chat', 'weixin', 'routes.json')
        self._history = defaultdict(lambda: deque(maxlen=20))
        self._history_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.log_receiver = None
        self.report_receiver = None
        self._load_route_cache()

    def _open(self):
        try:
            logger.debug('Weixin_Client starting')
            self._serving_thread = threading.Thread(target=self._start_serving, daemon=True)
            self._serving_thread.start()
        except Exception as e:
            logger.exception(f'Weixin_Client not started: {e}')

    def _close(self):
        self._stop_serving = True
        self._save_route_cache()
        if self._serving_thread is not None:
            self._serving_thread.join(timeout=5)
        for session in (self._auth_session, self._poll_session, self._send_session):
            try:
                session.close()
            except Exception:
                pass

    def _load_route_cache(self):
        try:
            if not os.path.exists(self._route_cache_path):
                return
            with open(self._route_cache_path, 'r', encoding='utf-8') as file_obj:
                payload = json.load(file_obj)
            if not isinstance(payload, dict):
                return
            with self._route_lock:
                for key, route in payload.items():
                    if not isinstance(route, dict):
                        continue
                    if not route.get('context_token'):
                        continue
                    self._routes[str(key)] = {
                        'receiver_key': route.get('receiver_key') or key,
                        'to_user_id': route.get('to_user_id') or route.get('receiver_key') or key,
                        'context_token': route.get('context_token'),
                        'group_id': route.get('group_id'),
                    }
        except Exception as e:
            logger.warning(f'Failed to load Weixin route cache: {e}')

    def _save_route_cache(self):
        try:
            os.makedirs(os.path.dirname(self._route_cache_path), exist_ok=True)
            with self._route_lock:
                payload = {
                    str(key): value
                    for key, value in self._routes.items()
                    if isinstance(value, dict) and value.get('context_token')
                }
            with open(self._route_cache_path, 'w', encoding='utf-8') as file_obj:
                json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f'Failed to save Weixin route cache: {e}')

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
                logger.info(f'\n========\n\n{msg}\n\n========\n')
                return

            if status in ('expired', 'canceled', 'cancelled'):
                self.agent.chat.send_to_log(f'Weixin QR login stopped with status: {status}')
                return

            time.sleep(1)

    def _build_headers(self, body_text=None):
        headers = {
            'Content-Type': 'application/json',
            'AuthorizationType': 'ilink_bot_token',
            'X-WECHAT-UIN': base64.b64encode(str(struct.unpack(">I", os.urandom(4))[0]).encode('utf-8')).decode('ascii'),
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
                    'base_info': {'channel_version': WEIXIN_CHANNEL_VERSION},
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

        org_manager_set = self._set_org_manager_if_missing(from_user_id)
        if org_manager_set:
            self.send_message(self._org_manager_status_text(), receiver=from_user_id)
        if not self._should_handle_incoming(from_user_id, self.org_manager_user_id, logger=logger, channel='weixin'):
            return
        if self._ensure_report_receiver_global('weixin', receiver_key):
            self.send_message(self._receiver_status_text('report', True), receiver=receiver_key)

        group_id = message.get('group_id')
        receiver_key = f'group:{group_id}' if group_id else from_user_id
        content = self._extract_message_content(message)
        if not content:
            logger.debug('Skipping unsupported Weixin message without text content')
            return

        self._remember_route(receiver_key, message)
        history_messages = self.get_history_messages(receiver_key)
        history = "\n".join([f'[{m[2]}] {m[0]}: {m[1]}' for m in history_messages])
        sender_name = receiver_key
        self._append_history(receiver_key, from_user_id, content)

        if content.startswith('/'):
            if from_user_id == self.org_manager_user_id:
                self._handle_command(content.strip(), receiver_key)
            return

        if hasattr(self.agent, 'handle_message'):
            self.agent.handle_message(
                message=content,
                history=history,
                sender=sender_name,
                channel='weixin',
            )

    def _extract_message_content(self, message):
        content_parts = []
        for item in message.get('item_list') or []:
            item_type = item.get('type')
            if item_type == WEIXIN_MSG_ITEM_TEXT:
                text = ((item.get('text_item') or {}).get('text') or '').strip()
                if text:
                    content_parts.append(text)
            elif item_type == WEIXIN_MSG_ITEM_IMAGE:
                image_item = item.get('image_item') or {}
                file_path = self._download_media_item(
                    self._build_media_download_url(image_item),
                    self._extract_media_aes_key(image_item),
                    image_item.get('media_id') or f'image_{int(time.time())}',
                    default_ext='.jpg',
                )
                if file_path:
                    content_parts.append(self._format_incoming_attachment_ref('image', file_path))
            elif item_type == WEIXIN_MSG_ITEM_VOICE:
                voice_item = item.get('voice_item') or {}
                file_path = self._download_media_item(
                    self._build_media_download_url(voice_item),
                    self._extract_media_aes_key(voice_item),
                    voice_item.get('media_id') or f'voice_{int(time.time())}',
                    default_ext='.silk',
                )
                transcript = (voice_item.get('text') or voice_item.get('transcript') or '').strip()
                if file_path:
                    content_parts.append(self._format_incoming_attachment_ref('voice', file_path))
                if transcript:
                    content_parts.append(f'[voice transcript] {transcript}')
            elif item_type == WEIXIN_MSG_ITEM_FILE:
                file_item = item.get('file_item') or {}
                filename = file_item.get('file_name') or file_item.get('media_id') or f'file_{int(time.time())}'
                _, ext = os.path.splitext(filename)
                file_path = self._download_media_item(
                    self._build_media_download_url(file_item),
                    self._extract_media_aes_key(file_item),
                    filename,
                    default_ext=ext or '.bin',
                )
                if file_path:
                    content_parts.append(self._format_incoming_attachment_ref('file', file_path))
        return "\n".join([part for part in content_parts if part]).strip()

    def _download_media_item(self, cdn_url, aes_key_base64, filename_stem, default_ext='.bin'):
        if not cdn_url or not aes_key_base64:
            return None
        try:
            response = requests.get(cdn_url, timeout=30)
            response.raise_for_status()
            decrypted = self._decrypt_aes_ecb(response.content, aes_key_base64)
            safe_name = str(filename_stem).replace('/', '_')
            if '.' not in safe_name and default_ext:
                safe_name = f'{safe_name}{default_ext}'
            return self._save_incoming_media_bytes('weixin', safe_name, decrypted)
        except Exception as e:
            logger.warning(f'Failed to download Weixin media item: {e}')
            return None

    def _extract_media_aes_key(self, media_item):
        if not isinstance(media_item, dict):
            return None
        media_ref = media_item.get('media') or media_item.get('cdn_media') or {}
        return (
            media_item.get('aes_key')
            or media_item.get('aeskey')
            or media_ref.get('aes_key')
            or media_ref.get('aeskey')
        )

    def _build_media_download_url(self, media_item):
        if not isinstance(media_item, dict):
            return None

        cdn_url = (
            media_item.get('cdn_url')
            or media_item.get('full_url')
            or media_item.get('url')
        )
        if cdn_url:
            cdn_url = str(cdn_url)
            if cdn_url.startswith('http://') or cdn_url.startswith('https://'):
                return cdn_url
            if 'encrypted_query_param=' in cdn_url:
                return f'{WEIXIN_CDN_BASE_URL}/download?{cdn_url.lstrip("?")}'

        media_ref = media_item.get('media') or media_item.get('cdn_media') or {}
        full_url = media_ref.get('full_url') or media_ref.get('cdn_url')
        if full_url:
            return full_url
        encrypted_query_param = (
            media_ref.get('encrypted_query_param')
            or media_ref.get('encrypt_query_param')
            or media_ref.get('param')
            or media_item.get('encrypted_query_param')
            or media_item.get('encrypt_query_param')
            or media_item.get('param')
        )
        if encrypted_query_param:
            return f'{WEIXIN_CDN_BASE_URL}/download?encrypted_query_param={quote(str(encrypted_query_param))}'

        return None

    @staticmethod
    def _decode_weixin_aes_key(key_value):
        if key_value is None:
            raise ValueError('empty aes key')

        if isinstance(key_value, bytes):
            if len(key_value) != 16:
                raise ValueError(f'invalid aes key bytes length: {len(key_value)}')
            return key_value

        key_str = str(key_value).strip()
        if len(key_str) == 32:
            try:
                return bytes.fromhex(key_str)
            except ValueError:
                pass

        decoded = base64.b64decode(key_str)
        if len(decoded) == 16:
            return decoded

        try:
            decoded_str = decoded.decode('utf-8').strip()
            if len(decoded_str) == 32:
                return bytes.fromhex(decoded_str)
        except Exception:
            pass

        raise ValueError(f'invalid aes key format: {key_value}')

    def _decrypt_aes_ecb(self, encrypted_data, key_base64):
        key = self._decode_weixin_aes_key(key_base64)
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        decryptor = cipher.decryptor()
        padded = decryptor.update(encrypted_data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    def _encrypt_aes_ecb(self, raw_data, key):
        padder = padding.PKCS7(128).padder()
        padded = padder.update(raw_data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def _set_org_manager_if_missing(self, sender_id):
        if self.org_manager_user_id not in (None, '', '?'):
            return False
        self.org_manager_user_id = sender_id
        if hasattr(self.agent.config, 'chat_weixin_org_manager'):
            self.agent.config.chat_weixin_org_manager = sender_id
        logger.info(f'Weixin org manager initialized to {sender_id}')
        return True

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
        self._save_route_cache()

    def _update_route_context_token(self, receiver, context_token):
        if not receiver or not context_token:
            return
        with self._route_lock:
            route = self._routes.get(str(receiver))
            if route is None:
                route = {
                    'receiver_key': str(receiver),
                    'to_user_id': str(receiver),
                    'context_token': context_token,
                    'group_id': None,
                }
                self._routes[str(receiver)] = route
            route['context_token'] = context_token
        self._save_route_cache()

    def _resolve_route(self, receiver):
        if receiver is None:
            return None
        receiver = str(receiver)
        with self._route_lock:
            route = self._routes.get(receiver)
            if route is not None:
                return route

        if receiver.startswith('group:'):
            return None

        try:
            if os.path.exists(self._route_cache_path):
                with open(self._route_cache_path, 'r', encoding='utf-8') as file_obj:
                    payload = json.load(file_obj)
                route = payload.get(receiver)
                if isinstance(route, dict) and route.get('context_token'):
                    normalized = {
                        'receiver_key': route.get('receiver_key') or receiver,
                        'to_user_id': route.get('to_user_id') or receiver,
                        'context_token': route.get('context_token'),
                        'group_id': route.get('group_id'),
                    }
                    with self._route_lock:
                        self._routes[receiver] = normalized
                    return normalized
        except Exception as e:
            logger.warning(f'Failed to resolve cached Weixin route for {receiver}: {e}')

        return None

    def _handle_command(self, command, receiver_key):
        response_text = None
        if command == '/log_here':
            self.log_receiver = receiver_key
            response_text = self._set_log_receiver_global('weixin', receiver_key)
        elif command == '/stop_log_here':
            self.log_receiver = None
            response_text = self._clear_log_receiver_global()
        elif command == '/report_here':
            self.report_receiver = receiver_key
            response_text = self._set_report_receiver_global('weixin', receiver_key)
        elif command == '/stop_report_here':
            self.report_receiver = None
            response_text = self._clear_report_receiver_global()

        if response_text:
            self.send_message(response_text, receiver=receiver_key)

    def send_message(self, message, receiver=None, subject=None):
        if not self.bot_token:
            err = 'send_message failed because weixin client not logged in'
            logger.error(err)
            raise RuntimeError(err)
        manager_receiver = self._manager_receiver(self.org_manager_user_id)
        if manager_receiver is not None:
            receiver = manager_receiver

        if receiver is None:
            if self.report_receiver:
                receiver = self.report_receiver
            else:
                return None

        if not receiver:
            err = 'No receiver specified for Weixin message'
            logger.error(err)
            raise RuntimeError(err)

        route = self._resolve_route(receiver)
        if not route:
            err = f'send_message failed. route unavailable for {receiver}'
            logger.error(err)
            raise RuntimeError(err)

        if not route.get('context_token'):
            err = f'send_message failed. context_token unavailable for {receiver}'
            logger.error(err)
            raise RuntimeError(err)

        item_list = self._build_outgoing_item_list(message, route)
        payload = {
            'msg': {
                'from_user_id': '',
                'to_user_id': route['receiver_key'],
                'client_id': self._generate_client_id(),
                'message_type': 2,
                'message_state': 2,
                'context_token': route['context_token'],
                'item_list': item_list,
            },
            'base_info': {'channel_version': WEIXIN_CHANNEL_VERSION},
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
            self._append_history(route['receiver_key'], 'assistant', self._message_to_plain_text(message))
        except Exception as e:
            logger.exception(f'Error sending Weixin message: {e}')
            raise

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

    def _build_outgoing_item_list(self, message, route):
        item_list = []
        for item in self._normalize_outgoing_message(message):
            if item['kind'] == 'text':
                item_list.append({
                    'type': WEIXIN_MSG_ITEM_TEXT,
                    'text_item': {'text': item['text']},
                })
                continue

            if item['message_type'] not in ('image', 'file'):
                item_list.append({
                    'type': WEIXIN_MSG_ITEM_TEXT,
                    'text_item': {'text': f'[{item["message_type"]}: {item["rel_path"]}]'},
                })
                continue

            upload_media_type = WEIXIN_MEDIA_TYPE_IMAGE if item['message_type'] == 'image' else WEIXIN_MEDIA_TYPE_FILE
            upload_data = self._prepare_weixin_upload(item, route, upload_media_type)
            if item['message_type'] == 'image':
                image_item = {
                    'media': upload_data['media'],
                    'mid_size': upload_data['encrypted_file_size'],
                }
                if upload_data.get('thumb_media'):
                    image_item.update({
                        'thumb_media': upload_data['thumb_media'],
                        'thumb_size': upload_data.get('thumb_encrypted_file_size'),
                        'thumb_width': upload_data.get('thumb_width'),
                        'thumb_height': upload_data.get('thumb_height'),
                    })
                item_list.append({
                    'type': WEIXIN_MSG_ITEM_IMAGE,
                    'image_item': image_item,
                })
            else:
                item_list.append({
                    'type': WEIXIN_MSG_ITEM_FILE,
                    'file_item': {
                        'media': upload_data['media'],
                        'file_name': item['name'],
                        'len': str(os.path.getsize(item['abs_path'])),
                    }
                })
        return item_list

    def _prepare_weixin_upload(self, item, route, media_type):
        raw_bytes = Path(item['abs_path']).read_bytes()
        aes_key = os.urandom(16)
        aes_key_hex = aes_key.hex()
        encrypted = self._encrypt_aes_ecb(raw_bytes, aes_key)
        filekey = os.urandom(16).hex()
        raw_md5 = hashlib.md5(raw_bytes).hexdigest()
        thumb_info = None
        if media_type == WEIXIN_MSG_ITEM_IMAGE:
            thumb_info = self._build_image_thumbnail(item['abs_path'])
        upload_resp = self._request_weixin_upload_url(
            to_user_id=route.get('to_user_id') or route.get('receiver_key'),
            filekey=filekey,
            media_type=media_type,
            encrypted_length=len(encrypted),
            raw_length=len(raw_bytes),
            raw_md5=raw_md5,
            aes_key_hex=aes_key_hex,
            thumb_info=thumb_info,
        )
        upload_param = upload_resp.get('upload_param')
        if not upload_param:
            raise RuntimeError(f'Weixin getuploadurl failed: {upload_resp}')
        aes_key_b64 = base64.b64encode(aes_key_hex.encode('utf-8')).decode('utf-8')
        upload_result = self._upload_weixin_cdn_bytes(upload_param, filekey, encrypted, aes_key_b64)
        result = {
            'media': upload_result['media'],
            'encrypted_file_size': len(encrypted),
        }
        if thumb_info:
            thumb_upload_param = upload_resp.get('thumb_upload_param')
            if thumb_upload_param:
                thumb_filekey = f'{filekey}_thumb'
                thumb_upload = self._upload_weixin_cdn_bytes(
                    thumb_upload_param,
                    thumb_filekey,
                    thumb_info['encrypted'],
                    thumb_info['aes_key_b64'],
                )
                result.update({
                    'thumb_media': thumb_upload['media'],
                    'thumb_encrypted_file_size': len(thumb_info['encrypted']),
                    'thumb_width': thumb_info['width'],
                    'thumb_height': thumb_info['height'],
                })
        return result

    def _request_weixin_upload_url(self, to_user_id, filekey, media_type, encrypted_length, raw_length, raw_md5, aes_key_hex, thumb_info=None):
        payload_variants = [
            {
                'filekey': filekey,
                'media_type': media_type,
                'to_user_id': to_user_id,
                'rawsize': raw_length,
                'rawfilemd5': raw_md5,
                'filesize': encrypted_length,
                'aeskey': aes_key_hex,
                'base_info': {'channel_version': WEIXIN_CHANNEL_VERSION},
            },
        ]
        if thumb_info:
            payload_variants[0].update({
                'thumb_rawsize': thumb_info['raw_size'],
                'thumb_rawfilemd5': thumb_info['raw_md5'],
                'thumb_filesize': len(thumb_info['encrypted']),
                'no_need_thumb': False,
            })
        else:
            payload_variants[0]['no_need_thumb'] = True

        last_error = None
        for payload in payload_variants:
            try:
                return self._api_post(
                    'ilink/bot/getuploadurl',
                    payload,
                    timeout=10,
                    session=self._send_session,
                )
            except RuntimeError as e:
                last_error = e
                if 'ret=-2' not in str(e) and "ret': -2" not in str(e):
                    raise

        raise last_error

    def _build_image_thumbnail(self, abs_path):
        try:
            with Image.open(abs_path) as image:
                image = image.convert('RGB')
                image.thumbnail((240, 240))
                width, height = image.size
                from io import BytesIO
                buffer = BytesIO()
                image.save(buffer, format='JPEG', quality=85)
                thumb_raw = buffer.getvalue()
        except Exception as e:
            logger.warning(f'Failed to build Weixin image thumbnail: {e}')
            return None

        aes_key = os.urandom(16)
        aes_key_hex = aes_key.hex()
        encrypted = self._encrypt_aes_ecb(thumb_raw, aes_key)
        return {
            'raw': thumb_raw,
            'raw_size': len(thumb_raw),
            'raw_md5': hashlib.md5(thumb_raw).hexdigest(),
            'encrypted': encrypted,
            'aes_key_hex': aes_key_hex,
            'aes_key_b64': base64.b64encode(aes_key_hex.encode('utf-8')).decode('utf-8'),
            'width': width,
            'height': height,
        }

    def _upload_weixin_cdn_bytes(self, upload_param, filekey, encrypted, aes_key_b64):
        upload_url = (
            f'{WEIXIN_CDN_BASE_URL}/upload'
            f'?encrypted_query_param={quote(str(upload_param))}'
            f'&filekey={quote(str(filekey))}'
        )
        response = requests.post(
            upload_url,
            data=encrypted,
            headers={'Content-Type': 'application/octet-stream'},
            timeout=60,
        )
        if response.status_code >= 400:
            error_msg = response.headers.get('x-error-message') or f'HTTP {response.status_code}'
            raise RuntimeError(f'Weixin CDN upload failed: {error_msg}')
        encrypted_param = response.headers.get('x-encrypted-param') or response.headers.get('X-Encrypted-Param')
        if not encrypted_param:
            raise RuntimeError(f'Weixin CDN upload missing x-encrypted-param header: status={response.status_code}')
        return {
            'media': {
                'encrypt_query_param': encrypted_param,
                'aes_key': aes_key_b64,
                'encrypt_type': 1,
            },
        }
