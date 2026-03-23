import os
import io
import json
import base64
import PIL.Image
import time
from datetime import datetime
import structlog
import requests
import tempfile
import subprocess
import re

from mobileclaw.device.device_base import DeviceControllerBase
from mobileclaw.device.phone.websocket_client import WebSocketClient
from mobileclaw.utils.scrcpy_recorder import ScrcpyRecorder

logger = structlog.get_logger(__name__)

class WebsocketController(DeviceControllerBase):
    """
    this class describes a connected device
    """

    def __init__(self, agent, device_name: str, device_id: str):
        super().__init__(agent, device_name, device_id)
        device_port = self._get_device_port(self.device_name)
        self.server_address = f"ws://localhost:{device_port}"
        self.websocket_client = WebSocketClient(server_address=self.server_address)
        self.device_serial_id = device_id

        # Initialize scrcpy recorder for native recording
        # self.scrcpy_recorder = ScrcpyRecorder()

    def __str__(self) -> str:
        return f"手机设备: {self.device_name}"

    def _resolve_device_serial_id(self) -> str:
        """
        解析设备序列号
        - 优先从配置的 device_mappings 中读取
        - 对于Android应用内场景，直接返回配置的设备ID（如"local"）
        - ADB方式仅作为PC控制场景的回退方案
        
        Returns:
            str: 设备序列号
        """
        try:
            # 优先从配置的 device_mappings 中读取
            if self.device_name in self.config.device_mappings:
                device_id = self.config.device_mappings[self.device_name]
                logger.debug(f"从配置中获取设备ID: {device_id}")
                return device_id
        except Exception as e:
            logger.debug(f"读取配置的 device_mappings 失败: {str(e)}")

        # 对于Android应用内场景，配置应该已经包含设备映射
        # 如果没有配置，说明可能是PC控制场景，尝试通过 adb devices 获取
        def _list_connected_devices() -> list:
            try:
                result = subprocess.run(
                    "adb devices",
                    shell=True,
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    return []
                lines = result.stdout.strip().splitlines()
                device_ids = []
                for line in lines[1:]:  # 跳过标题行
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == 'device':
                        device_ids.append(parts[0])
                return device_ids
            except Exception:
                return []

        device_ids = _list_connected_devices()
        
        if device_ids:
            logger.info("⚠️ 当前未在配置中找到设备映射，已自动使用ADB连接的第一个设备（仅适用于PC控制场景）")
            return device_ids[0]

        # 未检测到已连接设备：提醒并轮询，直到发现设备
        logger.info("❌ 当前未在配置中找到设备映射，且未检测到ADB连接的设备。请检查配置或设备连接状态，将在 3 秒后重试")
        last_notice_ts = time.time()
        while self.agent._enabled:
            time.sleep(0.2)
            device_ids = _list_connected_devices()
            if device_ids:
                logger.info("📱 已自动使用ADB连接的第一个设备（仅适用于PC控制场景）")
                return device_ids[0]
            # 每 3 秒提醒一次
            if time.time() - last_notice_ts >= 3:
                logger.info("❌ 仍未检测到设备，请检查配置或设备连接状态，将在 3 秒后重试")
                last_notice_ts = time.time()

    def _open(self):
        self._open_device()
        width, height = self.get_width_height()
        self.device_bound = (0, 0, width, height)
        self.width = width
        self.height = height

    def _open_device(self):
        self.websocket_client.start()

    def _close_device(self):
        """
        disconnect current device
        :return:
        """
        if self.websocket_client:
            self.websocket_client.close()

    def _get_all_installed_packages(self) -> list[str]:
        """
        通过 adb 获取设备上所有已安装的应用包名
        Returns:
            list[str]: 所有已安装的应用包名列表
        """
        try:
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell pm list packages",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                # 解析输出，格式为 "package:com.example.app"
                packages = []
                for line in result.stdout.strip().split('\n'):
                    if line.startswith('package:'):
                        package_name = line.replace('package:', '').strip()
                        packages.append(package_name)
                logger.debug(f"获取到 {len(packages)} 个已安装的应用包")
                return packages
            else:
                logger.error(f"获取应用包列表失败: {result.stderr}")
                return []
        except Exception as e:
            logger.error(f"获取应用包列表时发生错误: {str(e)}")
            return []

    def _get_package_main_activity(self, package_name: str) -> str:
        """
        获取指定包的主启动Activity
        Args:
            package_name: 应用包名
        Returns:
            str: 主启动Activity的完整名称，如果获取失败返回空字符串
        """
        try:
            # 方法1：使用 cmd package resolve-activity (推荐方法，跨平台兼容)
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell cmd package resolve-activity --brief {package_name}",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if '/' in line and package_name in line:
                        # 格式通常是 package_name/activity_name
                        parts = line.split('/')
                        if len(parts) >= 2:
                            activity = parts[1].strip()
                            if activity.startswith('.'):
                                activity = package_name + activity
                            logger.debug(f"找到包 {package_name} 的主Activity: {activity}")
                            return activity
            
            # 方法2：使用 pm dump 获取包的详细信息 (跨平台兼容版本)
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell pm dump {package_name}",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                # 在 Python 中解析输出，避免使用 grep
                lines = result.stdout.split('\n')
                found_main_intent = False
                
                for i, line in enumerate(lines):
                    # 查找包含 MAIN intent 的行
                    if 'android.intent.action.MAIN' in line:
                        found_main_intent = True
                        # 向前查找包含 Activity 的行
                        for j in range(max(0, i-10), min(len(lines), i+10)):
                            activity_line = lines[j]
                            if 'Activity' in activity_line and package_name in activity_line:
                                # 尝试提取Activity名称
                                activity_match = re.search(rf'{package_name}/([^\s\}}]+)', activity_line)
                                if activity_match:
                                    activity = activity_match.group(1)
                                    # 如果是相对路径，转换为绝对路径
                                    if activity.startswith('.'):
                                        activity = package_name + activity
                                    logger.debug(f"找到包 {package_name} 的主Activity: {activity}")
                                    return activity
                        break
            
            # 方法3：尝试通过 monkey 命令获取启动Activity
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                # monkey命令成功执行表示包可以启动，但我们需要获取具体的Activity
                # 尝试查看最近启动的Activity
                result = subprocess.run(
                    f"adb -s {self.device_serial_id} shell dumpsys activity activities | head -20",
                    shell=True,
                    capture_output=True,
                    text=True
                )
                
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.split('\n'):
                        if package_name in line and 'ActivityRecord' in line:
                            # 尝试提取Activity信息
                            activity_match = re.search(rf'{package_name}/([^\s\}}]+)', line)
                            if activity_match:
                                activity = activity_match.group(1)
                                if activity.startswith('.'):
                                    activity = package_name + activity
                                logger.debug(f"通过 monkey 找到包 {package_name} 的Activity: {activity}")
                                return activity
            
            logger.debug(f"无法获取包 {package_name} 的主Activity")
            return ""
        except Exception as e:
            logger.debug(f"获取包 {package_name} 主Activity时发生错误: {str(e)}")
            return ""

    def _start_app_by_package(self, package_name: str, activity_name: str = None) -> bool:
        """
        通过包名启动应用
        Args:
            package_name: 应用包名
            activity_name: 启动Activity名称，如果为空则自动获取
        Returns:
            bool: 启动是否成功
        """
        try:
            if not activity_name:
                activity_name = self._get_package_main_activity(package_name)
                if not activity_name:
                    logger.error(f"无法获取包 {package_name} 的启动Activity")
                    return False
            
            app_launcher_component_name = f"{package_name}/{activity_name}"

            # 启动应用
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell am start -n {app_launcher_component_name}",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                logger.debug(f"成功启动应用: {package_name}")
                return True, app_launcher_component_name
            else:
                logger.error(f"启动应用失败: {result.stderr}")
                return False, None
        except Exception as e:
            logger.error(f"启动应用时发生错误: {str(e)}")
            return False, None

    def start_app(self, app) -> bool:
        try:
            # 根据配置选择启动方式
            if self.config.prefer_phone_action_type == 'adb':
                # ADB 模式：需要获取 package 和 activity，然后用 ADB 启动
                logger.debug(f"使用 ADB 启动应用: {app}")
                
                # 先尝试直接启动（通过 open_app 获取可用应用列表）
                # 如果失败，会在 except 块中使用 LLM 智能查找
                raise RuntimeError(f"ADB 模式需要通过智能查找启动应用")
                
            else:
                # WebSocket 模式：直接使用 open_app 命令（传入应用名称）
                logger.debug(f"使用 WebSocket 启动应用: {app}")
                launch_res = self._send_command('open_app,' + app)
                
                if launch_res.get('status') != 'success':
                    raise RuntimeError(f"WebSocket 启动应用失败: {launch_res.get('message', 'Unknown error')}")
                
                logger.info(f"✅ 成功启动 app \"{app}\"")
                self.agent.sleep(0.5)
                
                # WebSocket 模式下，component name 从 open_app 返回的信息中获取（如果有）
                # 或者设置为空
                app_launcher_component_name = ''
                self._notify_app_started(app, app_launcher_component_name=app_launcher_component_name)
            
        except Exception as e:
            # 如果 手机端 启动 app 失败，则调用 LLM 智能分析提取所需启动的 app 的 package name
            logger.info(f"🔁 未能直接在手机上找到 “{app}” app，正在智能分析本地 app 信息，并尝试启动")

            # 提取 手机端 返回的 availableApps
            available_apps = []
            if len(e.args) > 0 and isinstance(e.args[0], dict):
                error_dict = e.args[0]
                available_apps = error_dict.get('availableApps', [])

            logger.debug(f"🔁 直接找到的 app，当前设备上的可见app列表为: {available_apps}")

            # 获取 手机端 捕获的 app 之外的 app package（比如：新安装的 app）
            # 仅在 ADB 模式下获取额外的 app packages
            other_app_packages = []
            if self.config.prefer_phone_action_type == 'adb':
                all_packages = self._get_all_installed_packages()
                available_packages = set()
                for app_info in available_apps:
                    if isinstance(app_info, dict) and 'appPkg' in app_info:
                        available_packages.add(app_info['appPkg'])
                other_app_packages = [pkg for pkg in all_packages if pkg not in available_packages]
            
            # 调用 LLM 提取所需启动的 app 的 package name
            # result = self.agent.fm.dynamic_prompt.find_app(app, available_apps, other_app_packages)
            prompt = f"""You are an expert Android application developer. The user wants to launch an application by name, and your task is to find the best matching application's package name.

User requested app name: "{app}"

List of installed applications (name, package, launcher, local_name) on this device provided by the user:
```
{json.dumps(available_apps, indent=2, ensure_ascii=False)}
```

Additional package names detected via adb that are not listed above:
```
{json.dumps(other_app_packages, indent=2, ensure_ascii=False)}
```

Instructions:
1. Analyze the user's requested app name and find the best matching application from the installed apps list or additional package names.
2. Consider common abbreviations, alternative names, and partial matches:
   - "WeChat" should match "com.tencent.mm"
   - "Chrome" should match "com.android.chrome"
   - Partial names like "Alipay" should match "com.eg.android.AlipayGphone"
3. If you find a matching app, return the exact package name.
4. If no reasonable match is found, return an empty string "".

Note: Be conservative - only return a match if you're confident the user meant that application. Consider both the display name and local_name when matching.
"""
            # Call fm.query to get the best matching app path
            result = self.agent.fm.call_func(
                'query_model_formatted',
                params={'query': prompt, 'returns': [("app_package_name", str)]},
            )

            if result is None:
                logger.error(f"❌ 未找到名为 \"{app}\" 的应用，请检查 app 名称是否正确以及 app 是否已安装")
            else:
                package_name = result

            if package_name:
                logger.debug(f"找到 app \"{app}\" 匹配的应用包: {package_name}")
                
                # 检查包名是在 available_apps 中还是在 other_app_packages 中
                found_in_available = False
                launcher_activity = None
                
                # 先检查 available_apps
                for app_info in available_apps:
                    if isinstance(app_info, dict) and app_info.get('appPkg') == package_name:
                        launcher_activity = app_info.get('appLauncher')
                        found_in_available = True
                        break
                
                if found_in_available and launcher_activity:
                    # 从 available_apps 中找到，根据配置启动
                    logger.debug(f"从 available_apps 中找到应用，启动Activity: {launcher_activity}")
                    
                    if self.config.prefer_phone_action_type == 'adb':
                        # ADB 模式：使用 _start_app_by_package
                        success, app_launcher_component_name = self._start_app_by_package(package_name, launcher_activity)
                    else:
                        # WebSocket 模式：直接使用 open_app 命令传应用名
                        try:
                            launch_res = self._send_command(f'open_app,{app}')
                            success = launch_res.get('status') == 'success'
                            app_launcher_component_name = f"{package_name}/{launcher_activity}"
                        except Exception as e:
                            logger.debug(f"WebSocket 启动失败: {e}")
                            success = False
                            app_launcher_component_name = None
                    
                    if success:
                        logger.info(f"✅ 成功启动 app \"{app}\"")
                        self.agent.sleep(0.5)
                        # 通知应用启动成功，传递已获取的 app_launcher_component_name
                        self._notify_app_started(app, app_launcher_component_name=app_launcher_component_name)
                        return True
                elif self.config.prefer_phone_action_type == 'adb':
                    # 仅在 ADB 模式下尝试从 other_app_packages 中启动
                    logger.debug(f"从 other_app_packages 中找到应用: {package_name}")
                    success, app_launcher_component_name = self._start_app_by_package(package_name)
                    if success:
                        logger.info(f"✅ 成功启动 app \"{app}\"")
                        self.agent.sleep(0.5)
                        # 通知应用启动成功，传递已获取的 bundle_id（如果有）
                        self._notify_app_started(app, app_launcher_component_name=app_launcher_component_name)
                        return True
                
                logger.error(f"❌ 在本地找到 “{app}” app，但启动失败")
                raise RuntimeError(f"找到应用 \"{app}\" 但启动失败，包名: {package_name}")
            else:
                logger.error(f"❓ 未找到名为 “{app}” 的应用，请检查 app 名称是否正确以及 app 是否已安装")
                raise RuntimeError(f"未找到名为 \"{app}\" 的应用，请检查 app 名称是否正确以及 app 是否已安装")

        return True

    def _get_app_info(self, app_name: str, **kwargs) -> dict:
        """获取Android应用信息
        
        Args:
            app_name: 应用名称
            **kwargs: 可选参数，可包含 bundle_id
            
        Returns:
            dict: 应用信息字典
        """
        app_launcher_component_name = kwargs.get('app_launcher_component_name', '')
        
        app_info = {
            "bundle_id": app_launcher_component_name,
            "name": app_name,
            "type": 3,  # Android端固定为3
            "version": ""
        }
        
        # 尝试获取应用版本信息 (仅 ADB 模式支持)
        try:
            if app_launcher_component_name and self.config.prefer_phone_action_type == 'adb':
                # 从 component name 中提取 package name (格式通常是: com.package.name/.ActivityName)
                package_name = app_launcher_component_name.split('/')[0] if '/' in app_launcher_component_name else app_launcher_component_name
                result = subprocess.run(
                    f"adb -s {self.device_serial_id} shell dumpsys package {package_name} | grep versionName",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                if result.returncode == 0 and result.stdout:
                    # 解析输出，格式通常类似于: versionName=1.0.0
                    version_line = result.stdout.strip()
                    if '=' in version_line:
                        app_info["version"] = version_line.split('=')[1].strip()
        except Exception as e:
            logger.debug(f"获取应用版本信息失败: {str(e)}")
        
        return app_info
    
    def kill_app(self, app) -> bool:
        """
        强制停止应用
        Args:
            app: 应用名称
        Returns:
            bool: 是否成功
        """
        try:
            # 通过WebSocket发送kill_app命令（WebSocket服务器会处理包名获取和强制停止）
            res = self._send_command(f'kill_app,{app}')
            
            if res.get('status') == 'success':
                logger.debug(f"已通过WebSocket强制停止应用: {app}")
                return True
            else:
                logger.debug(f"强制停止应用失败: {res.get('message', 'Unknown error')}")
                return False
                    
        except Exception as e:
            logger.debug(f"强制停止应用失败: {str(e)}")
            return False
    
    def push_file(self, local_path, remote_path):
        pass

    def pull_file(self, local_path, remote_path):
        pass

    def key_press(self, key):
        pass

    def back(self) -> bool:
        res = self._send_command('back')
        return True

    def home(self) -> bool:
        res = self._send_command('home')
        return True

    def long_touch(self, x, y, duration=None) -> bool:
        if self.config.prefer_phone_action_type == 'adb':
            try:
                if duration is None:
                    logger.debug(f"使用 adb click 命令执行长按操作: {x} {y} {duration}")
                    subprocess.run(
                        f"adb -s {self.device_serial_id} shell click {x} {y}",
                        shell=True,
                        check=True
                    )
                else:
                    logger.debug(f"使用 adb swipe 命令执行长按操作: {x} {y} {x} {y} {duration}")
                    subprocess.run(
                        f"adb -s {self.device_serial_id} shell input swipe {x} {y} {x} {y} {duration}",
                        shell=True,
                        check=True
                    )
                logger.debug("使用 adb 命令成功执行长按操作")
                return (x, y)
            except Exception as e:
                logger.debug(f"使用 adb 命令执行长按操作失败，继续使用 websocket 执行操作: {str(e)}")

        # TODO: support duration
        res = self._send_command('click,' + str(x) + ',' + str(y) + ',' + str(duration))
        self.agent.sleep(duration * 0.001)
        return (x, y)

    def click(self, x, y, duration=200) -> bool:
        # logger.debug(
        #     f'Click action done at ({x}, {y})',
        #     action='click',
        #     status='done',
        #     metadata={'coordinates': (x, y)}
        # )
        return self.long_touch(x, y, duration)

    def snap_click(self, x: int, y: int, duration=200) -> bool:
        """
        坐标吸附点击功能：将输入坐标匹配到最近的可点击元素，然后执行点击
        
        Args:
            x: 目标x坐标
            y: 目标y坐标
            duration: 点击持续时间
            
        Returns:
            bool: 点击是否成功
        """
        try:
            # 获取UI组件树
            ui_tree = self.get_ui_tree()

            if not ui_tree:
                logger.warning("UI树为空，无法进行坐标吸附")
                return self.click(x, y, duration)
            
            # 找到最近的可点击元素
            target_element = self._find_nearest_clickable_element(ui_tree, x, y)
            
            if target_element:
                # 计算目标元素的中心坐标
                bounds = target_element['bounds']
                center_x = (bounds[0][0] + bounds[1][0]) // 2
                center_y = (bounds[0][1] + bounds[1][1]) // 2
                
                logger.debug(
                    f'Snap click: 原坐标({x}, {y}) -> 吸附到元素中心({center_x}, {center_y})',
                    action='snap_click',
                    status='done',
                    metadata={
                        'original_coordinates': (x, y),
                        'snapped_coordinates': (center_x, center_y),
                        'element_class': target_element.get('class'),
                        'element_resource_id': target_element.get('resource_id')
                    }
                )
                
                # 执行点击
                return self.click(center_x, center_y, duration)
            else:
                # 如果没有找到可点击元素，使用原坐标点击
                logger.warning(f"未找到可点击元素，使用原坐标({x}, {y})进行点击")
                return self.click(x, y, duration)
                
        except Exception as e:
            logger.error(f"坐标吸附过程中发生错误: {str(e)}")
            # 发生错误时回退到普通点击
            return self.click(x, y, duration)

    def _find_nearest_clickable_element(self, ui_tree, target_x, target_y):
        """
        在UI树中找到距离目标坐标最近的可点击元素
        
        Args:
            ui_tree: UI组件树
            target_x: 目标x坐标
            target_y: 目标y坐标
            
        Returns:
            dict: 最近的可点击元素，如果没有找到返回None
        """
        if not ui_tree:
            return None
            
        # 过滤出可见且启用的元素
        valid_elements = [elem for elem in ui_tree 
                         if elem.get('visible', False) and elem.get('enabled', True)]
        
        if not valid_elements:
            return None
            
        # 先寻找直接可点击的元素
        clickable_elements = [elem for elem in valid_elements if elem.get('clickable', False)]
        
        if clickable_elements:
            # 找到距离最近的可点击元素
            nearest_element = self._find_nearest_element_by_distance(clickable_elements, target_x, target_y)
            if nearest_element:
                return nearest_element
        
        # 如果没有直接可点击的元素，寻找包含可点击子元素的元素
        return self._find_nearest_element_with_clickable_children(valid_elements, ui_tree, target_x, target_y)
    
    def _find_nearest_element_by_distance(self, elements, target_x, target_y):
        """
        根据距离找到最近的元素
        
        Args:
            elements: 元素列表
            target_x: 目标x坐标
            target_y: 目标y坐标
            
        Returns:
            dict: 最近的元素
        """
        if not elements:
            return None
            
        min_distance = float('inf')
        nearest_element = None
        
        for element in elements:
            bounds = element.get('bounds')
            if not bounds or len(bounds) != 2:
                continue
                
            # 计算元素中心坐标
            center_x = (bounds[0][0] + bounds[1][0]) / 2
            center_y = (bounds[0][1] + bounds[1][1]) / 2
            
            # 计算到目标点的距离
            distance = ((center_x - target_x) ** 2 + (center_y - target_y) ** 2) ** 0.5
            
            # 检查目标点是否在元素边界内（如果在边界内，优先级更高）
            is_inside = (bounds[0][0] <= target_x <= bounds[1][0] and 
                        bounds[0][1] <= target_y <= bounds[1][1])
            
            # 如果在边界内，距离设为很小的值以提高优先级
            if is_inside:
                distance = distance * 0.1
            
            if distance < min_distance:
                min_distance = distance
                nearest_element = element
                
        return nearest_element
    
    def _find_nearest_element_with_clickable_children(self, elements, ui_tree, target_x, target_y):
        """
        在元素的子元素中寻找可点击的元素
        
        Args:
            elements: 父元素列表
            ui_tree: 完整的UI树
            target_x: 目标x坐标  
            target_y: 目标y坐标
            
        Returns:
            dict: 最近的可点击子元素
        """
        # 创建元素索引映射
        element_map = {elem.get('temp_id'): elem for elem in ui_tree}
        
        # 收集所有可点击的子元素
        all_clickable_children = []
        
        for element in elements:
            children_ids = element.get('children', [])
            if children_ids:
                # 递归收集所有可点击的后代元素
                clickable_children = self._collect_clickable_descendants(
                    children_ids, element_map, target_x, target_y
                )
                all_clickable_children.extend(clickable_children)
        
        # 从所有可点击的子元素中找到最近的
        return self._find_nearest_element_by_distance(all_clickable_children, target_x, target_y)
    
    def _collect_clickable_descendants(self, children_ids, element_map, target_x, target_y):
        """
        递归收集所有可点击的后代元素
        
        Args:
            children_ids: 子元素ID列表
            element_map: 元素ID到元素的映射
            target_x: 目标x坐标
            target_y: 目标y坐标
            
        Returns:
            list: 可点击的后代元素列表
        """
        clickable_descendants = []
        
        for child_id in children_ids:
            child_element = element_map.get(child_id)
            if not child_element:
                continue
                
            # 检查元素是否可见且启用
            if not (child_element.get('visible', False) and child_element.get('enabled', True)):
                continue
                
            # 如果子元素可点击，添加到列表
            if child_element.get('clickable', False):
                clickable_descendants.append(child_element)
            
            # 递归检查子元素的子元素
            grandchildren_ids = child_element.get('children', [])
            if grandchildren_ids:
                grandchildren_clickable = self._collect_clickable_descendants(
                    grandchildren_ids, element_map, target_x, target_y
                )
                clickable_descendants.extend(grandchildren_clickable)
        
        return clickable_descendants

    def long_click(self, x, y, duration=1000) -> bool:
        logger.debug(
            f'Long click action done at ({x}, {y})',
            action='long_click',
            status='done',
            metadata={'coordinates': (x, y)}
        )
        return self.long_touch(x, y, duration)

    def input(self, text):
        logger.debug(
            f'Input action done with text: {text}',
            action='input',
            status='done',
            metadata={'text': text}
        )
        self.view_append_text(text)

    def clear(self):
        logger.debug(
            f'Clear text action done',
            action='clear',
            status='done',
        )
        self.view_clear_text()

    def clear_and_input(self, text):
        logger.debug(
            f'Clear and input action done with text: {text}',
            action='clear_and_input',
            status='done',
            metadata={'text': text}
        )
        self.view_set_text(text)

    def scroll(self, *args, **kwargs) -> bool:
        """
        执行滚动操作
        支持两种调用方式：
        1. scroll(direction, start_xy=None, duration=1000) - 方向版本
        2. scroll(start_xy, end_xy, duration=1000) - 坐标版本

        Args:
            direction 或 start_xy: 方向字符串('up', 'down', 'left', 'right') 或起始坐标元组
            start_xy 或 end_xy: 起始坐标元组 或 结束坐标元组
            duration: 滚动持续时间（毫秒）
        Returns:
            bool: 滚动是否成功
        """
        if len(args) == 1 and isinstance(args[0], str):
            # 方向版本调用: scroll(direction, start_xy=None, duration=1000)
            direction = args[0]
            start_xy = kwargs.get('start_xy')
            duration = kwargs.get('duration', 1000)

            if start_xy is None:
                # 如果没有指定起始坐标，使用屏幕中心
                start_xy = (self.width // 2, self.height // 2)

            # 根据方向计算结束坐标（滑动 1/3 屏幕距离）
            if direction == 'up':
                distance = self.height // 3
                end_xy = (start_xy[0], start_xy[1] + distance)
            elif direction == 'down':
                distance = self.height // 3
                end_xy = (start_xy[0], start_xy[1] - distance)
            elif direction == 'left':
                distance = self.width // 2
                end_xy = (start_xy[0] + distance, start_xy[1])
            elif direction == 'right':
                distance = self.width // 2
                end_xy = (start_xy[0] - distance, start_xy[1])
            else:
                logger.error(f"不支持的滚动方向: {direction}")
                return False

        elif len(args) >= 2:
            # 坐标版本调用: scroll(start_xy, end_xy, duration=1000)
            start_xy = args[0]
            end_xy = args[1]
            duration = args[2] if len(args) > 2 else kwargs.get('duration', 1000)
        else:
            logger.error("scroll 方法调用参数错误")
            return False

        # 确保坐标在屏幕范围内
        start_xy = (max(0, min(start_xy[0], self.width - 1)),
                   max(0, min(start_xy[1], self.height - 1)))
        end_xy = (max(0, min(end_xy[0], self.width - 1)),
                 max(0, min(end_xy[1], self.height - 1)))

        # 使用 ADB 命令执行滑动
        if self.config.prefer_phone_action_type == 'adb':
            try:
                logger.debug(f"使用 adb swipe 命令执行滚动: {start_xy} -> {end_xy}, duration={duration}")
                subprocess.run(
                    f"adb -s {self.device_serial_id} shell input swipe {start_xy[0]} {start_xy[1]} {end_xy[0]} {end_xy[1]} {duration}",
                    shell=True,
                    check=True
                )
                self.agent.sleep(duration * 0.001)
                logger.debug("使用 adb 命令成功执行滚动操作")
                return True
            except Exception as e:
                logger.debug(f"使用 adb 命令执行滚动操作失败，继续使用 websocket 执行操作: {str(e)}")

        # 使用 websocket 命令执行拖拽（回退方案）
        try:
            res = self._send_command(f"drag,{start_xy[0]},{start_xy[1]},{end_xy[0]},{end_xy[1]},{duration}")
            self.agent.sleep(duration * 0.001)
            return True
        except Exception as e:
            logger.error(f"执行滚动操作失败: {str(e)}")
            return False

    def _do_drag(self, start_xy, end_xy, duration=None) -> bool:
        if duration is None:
            duration = 500
        logger.debug(f"✋ [websocket_device] 准备执行拖拽动作: ({start_xy[0]}, {start_xy[1]}) -> ({end_xy[0]}, {end_xy[1]})")
        res = self._send_command(f"drag,{start_xy[0]},{start_xy[1]},{end_xy[0]},{end_xy[1]},{duration}")
        logger.debug(f"✋ [websocket_device] 执行拖拽完成: ({start_xy[0]}, {start_xy[1]}) -> ({end_xy[0]}, {end_xy[1]})")
        self.agent.sleep(duration * 0.001)
        return True

    def get_current_state(self):
        res = self._send_command('view_hierarchy')

        if 'message' not in res or 'height' not in res or 'width' not in res:
            raise Exception('Invalid response, missing message or height or width while getting current state, please check the device recording premissions')
        views = res['message']
        height = res['height']
        width = res['width']

        try:
            views = json.loads(views) if isinstance(views, str) else views
        except Exception as e:
            views = []

        # TODO: DeviceState 在遇到超长 GUI Tree 的时候（比如微博首页），会爆迭代错误，需要优化
        device_state = DeviceState(views, width, height)

        return device_state

    def get_width_height(self):
        res = self._send_command('width_height')
        return res['width'], res['height']
    
    def get_current_app_package(self) -> str:
        """
        获取当前前台应用的包名
        
        注意:此功能仅在 ADB 模式下可用,WebSocket 模式下会返回空字符串
        
        Returns:
            str: 当前前台应用的包名，如果获取失败或不在 ADB 模式下返回空字符串
        """
        if self.config.prefer_phone_action_type != 'adb':
            logger.debug("WebSocket 模式不支持获取当前应用包名")
            return ""
        
        try:
            # 使用 adb 命令获取当前前台活动
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell dumpsys activity activities | grep mFocusedActivity",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                # 解析输出，格式通常类似于：mFocusedActivity: ActivityRecord{xxx com.tencent.mm/...}
                match = re.search(r'mFocusedActivity:.*?{.*?\s+([^/\s]+)/', result.stdout)
                if match:
                    package_name = match.group(1)
                    logger.debug(f"当前前台应用包名: {package_name}")
                    return package_name
            
            # 如果上面的方法失败，尝试使用另一种方法
            result = subprocess.run(
                f"adb -s {self.device_serial_id} shell dumpsys window | grep mCurrentFocus",
                shell=True,
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0 and result.stdout:
                # 解析输出，格式通常类似于：mCurrentFocus=Window{xxx com.tencent.mm/...}
                match = re.search(r'mCurrentFocus=Window{.*?\s+([^/\s]+)/', result.stdout)
                if match:
                    package_name = match.group(1)
                    logger.debug(f"当前前台应用包名: {package_name}")
                    return package_name
                    
        except Exception as e:
            logger.debug(f"获取当前应用包名失败: {str(e)}")
        
        return ""

    def get_current_app_info(self) -> dict:
        """
        获取当前前台应用的完整信息，包括包名和显示名称
        
        注意:此功能仅在 ADB 模式下可用,WebSocket 模式下会返回空字典

        Returns:
            dict: 包含以下字段的字典：
                - package_name: 应用包名
                - component_name: 应用的完整组件名 (package/activity)
                - display_name: 应用本地显示名称
                如果获取失败或不在 ADB 模式下，各字段可能为空字符串
        """
        result = {
            'package_name': '',
            'component_name': '',
            'display_name': ''
        }
        
        if self.config.prefer_phone_action_type != 'adb':
            logger.debug("WebSocket 模式不支持获取当前应用信息")
            return result

        try:
            # 第一步：通过 ADB 获取当前前台活动的完整组件名
            # 使用 topResumedActivity 更准确地获取当前前台活动
            adb_result = subprocess.run(
                f"adb -s {self.device_serial_id} shell dumpsys activity activities | grep topResumedActivity",
                shell=True,
                capture_output=True,
                text=True
            )

            component_name = ""
            if adb_result.returncode == 0 and adb_result.stdout:
                # 解析输出，格式通常类似于：topResumedActivity=ActivityRecord{40c5d47 u0 com.wisewk.assistant/com.example.ruyiclient.MainActivity t378}
                match = re.search(r'topResumedActivity=ActivityRecord\{[^}]+\s+u0\s+([^}\s]+)', adb_result.stdout)
                if match:
                    component_name = match.group(1).strip()
                    result['component_name'] = component_name
                    logger.debug(f"当前前台应用组件名: {component_name}")

            # 如果上面的方法失败，尝试使用 mFocusedActivity 作为备用方法
            if not component_name:
                adb_result = subprocess.run(
                    f"adb -s {self.device_serial_id} shell dumpsys activity activities | grep mFocusedActivity",
                    shell=True,
                    capture_output=True,
                    text=True
                )

                if adb_result.returncode == 0 and adb_result.stdout:
                    # 解析输出，格式通常类似于：mFocusedActivity: ActivityRecord{xxx u0 com.tencent.mm/.ui.LauncherUI}
                    match = re.search(r'mFocusedActivity:.*?ActivityRecord\{[^}]+\s+u0\s+([^}\s]+)', adb_result.stdout)
                    if match:
                        component_name = match.group(1).strip()
                        result['component_name'] = component_name
                        logger.debug(f"当前前台应用组件名 (备用方法): {component_name}")

            # 如果仍然失败，使用 dumpsys window 作为最后备选
            if not component_name:
                adb_result = subprocess.run(
                    f"adb -s {self.device_serial_id} shell dumpsys window | grep mCurrentFocus",
                    shell=True,
                    capture_output=True,
                    text=True
                )

                if adb_result.returncode == 0 and adb_result.stdout:
                    # 解析输出，格式通常类似于：mCurrentFocus=Window{xxx u0 com.tencent.mm/.ui.LauncherUI}
                    match = re.search(r'mCurrentFocus=Window\{[^}]+\s+u0\s+([^}\s]+)', adb_result.stdout)
                    if match:
                        component_name = match.group(1).strip()
                        result['component_name'] = component_name
                        logger.debug(f"当前前台应用组件名 (最后备选): {component_name}")

            # 第二步：从组件名中提取包名
            if component_name and '/' in component_name:
                package_name = component_name.split('/')[0]
                result['package_name'] = package_name
                logger.debug(f"从组件名提取的包名: {package_name}")
            elif component_name:
                # 如果没有 '/' 分隔符，整个就是包名
                result['package_name'] = component_name

            # 第三步：通过 websocket 获取应用的显示名称
            if result['package_name']:
                try:
                    websocket_result = self._send_command(f'get_app_display_name,{result["package_name"]}')
                    if websocket_result and 'message' in websocket_result:
                        display_name = websocket_result['message']
                        result['display_name'] = display_name
                        logger.debug(f"应用 {result['package_name']} 的显示名称: {display_name}")
                    else:
                        logger.debug(f"通过 websocket 获取应用 {result['package_name']} 显示名称失败")
                except Exception as e:
                    logger.debug(f"通过 websocket 获取应用显示名称时出错: {str(e)}")

        except Exception as e:
            logger.debug(f"获取当前应用信息失败: {str(e)}")

        return result

    def _adb_input_text(self, text) -> bool:
        """
        使用 adb 命令输入文本
        Args:
            text: 要输入的文本（支持任何类型，会自动转换为字符串）
        Returns:
            bool: 输入是否成功
        """
        try:
            # 确保text是字符串类型
            text_str = str(text)

            # 统一换行符，方便后续处理多行输入
            text_str = text_str.replace('\r\n', '\n').replace('\r', '\n')

            # 如果包含非 ASCII 字符（如中文、表情等），改用 剪贴板+粘贴 键方式，规避 adb input 的系统 Bug
            try:
                text_str.encode('ascii')
                is_ascii = True
            except Exception:
                is_ascii = False

            if not is_ascii:
                try:
                    lines = text_str.split('\n')

                    for idx, line in enumerate(lines):
                        if line:
                            self.set_clipboard(line)
                            try:
                                clipboard_text = self.get_clipboard()
                                if clipboard_text != line:
                                    logger.debug(
                                        f"剪贴板校验不一致，expected={repr(line)}, actual={repr(clipboard_text)}"
                                    )
                            except Exception as verify_err:
                                logger.debug(f"剪贴板校验失败: {verify_err}")

                            subprocess.run(
                                ["adb", "-s", self.device_serial_id, "shell", "input", "keyevent", "279"],
                                check=True
                            )
                            time.sleep(0.15)

                        if idx < len(lines) - 1:
                            subprocess.run(
                                ["adb", "-s", self.device_serial_id, "shell", "input", "keyevent", "66"],
                                check=True
                            )
                            time.sleep(0.1)

                    logger.debug("使用剪贴板+粘贴键方式成功输入非 ASCII 文本")
                    return True
                except Exception as e_clip:
                    logger.debug(f"使用剪贴板方式输入失败: {str(e_clip)}")
                    # 继续尝试退化到原始方式（大概率仍会失败，但保持兼容）

            # 对 ASCII 文本，使用 adb input text。
            # - 空格需要转为 %s，否则 adb input 会把空格后的内容丢弃
            # - 换行为多次 input text + 回车键 66
            lines = text_str.split('\n')

            for idx, line in enumerate(lines):
                if line:
                    # 先对空格做 adb 的 %s 转义
                    line_for_adb = line.replace(' ', '%s')
                    # 转义双引号和单引号，避免被 shell 误解析
                    escaped_text = line_for_adb.replace('"', '\\"').replace("'", "\\'")

                    subprocess.run(
                        ["adb", "-s", self.device_serial_id, "shell", "input", "text", escaped_text],
                        check=True
                    )

                # 如果不是最后一行，说明原始文本中存在换行符，补一个 Enter
                if idx < len(lines) - 1:
                    subprocess.run(
                        ["adb", "-s", self.device_serial_id, "shell", "input", "keyevent", "66"],
                        check=True
                    )

            logger.debug(f"使用 adb 成功输入文本: {repr(text_str)}")
            return True
        except Exception as e:
            logger.debug(f"使用 adb 输入文本失败: {str(e)}")
            return False
    
    def _adb_clear_text(self) -> bool:
        """
        使用 adb 命令清除当前输入框的文本
        Returns:
            bool: 清除是否成功
        """
        try:
            for _ in range(50):  # 删除50个字符，应该足够清除大多数文本
                subprocess.run(
                    f"adb -s {self.device_serial_id} shell input keyevent KEYCODE_DEL",
                    shell=True,
                    check=True
                )
            logger.debug("使用备用方法清除文本")
            return True
        except Exception as e2:
            logger.debug(f"备用清除文本方法也失败: {str(e2)}")
            return False
    
    def view_set_text(self, text) -> bool:
        # 确保text是字符串类型
        text_str = str(text)
        
        # 根据配置选择输入方式
        if self.config.prefer_phone_action_type == 'adb':
            logger.debug(f"使用 adb 命令清除并输入文本: {text_str}")
            # 先清除现有文本，然后输入新文本
            self._adb_clear_text()
            return self._adb_input_text(text_str)
        else:
            logger.debug(f"使用 websocket 命令清除并输入文本: {text_str}")
            # 使用 websocket 方式：先清除，再输入
            self._send_command('clear')
            res = self._send_command('input,' + text_str)
            return True
    
    def view_append_text(self, text) -> bool:
        # 确保text是字符串类型
        text_str = str(text)
        
        # 根据配置选择输入方式
        if self.config.prefer_phone_action_type == 'adb':
            logger.debug(f"使用 adb 命令追加输入文本: {text_str}")
            return self._adb_input_text(text_str)
        else:
            logger.debug(f"使用 websocket 命令追加输入文本: {text_str}")
            res = self._send_command('input,' + text_str)
            return True
    
    def view_clear_text(self) -> bool:
        res = self._send_command('clear')
        return True
    
    def get_input_field_text(self) -> str:
        try:
            res = self._send_command('get_input_field_text')
            message = res['message']
            return message
        except Exception as e:
            logger.debug(f"❌ 获取输入框文本失败: {e}")
            return ""

    def enter(self) -> bool:
        """在当前设备上发送回车键 (Enter)"""
        if self.config.prefer_phone_action_type == 'adb':
            try:
                subprocess.run(
                    f"adb -s {self.device_serial_id} shell input keyevent 66",
                    shell=True,
                    check=True
                )
                logger.debug("使用 adb 命令发送回车键")
                return True
            except Exception as e:
                logger.error(f"使用 adb 发送 Enter 动作失败: {e}")
                return False
        else:
            # WebSocket 模式暂不支持 Enter 键
            logger.warning("WebSocket 模式暂不支持 Enter 键操作")
            return False

    def _send_command(self, command: str):
        res = self.websocket_client.send_message(command)
        # TODO: add a timeout here
        if res is None or res is False:
            raise Exception('command failed')
        res = json.loads(res)
        if res['status'] != 'success':
            if res.get('clipboard_fallback'):
                raise Exception("Input failed. Text saved into clipboard. Try to paste it instead.")
            raise Exception(res.get('message', 'command failed'))
        return res

    def take_screenshot_adb(self):
        """
        使用 ADB 命令进行截图
        Args:
            device_serial_id: 设备序列号
        Returns:
            PIL.Image: 截图图像
        """
        # 生成临时截图文件路径
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
            screenshot_path = temp_file.name
        
        try:
            # 使用 ADB 命令截图
            result = subprocess.run(
                f"adb -s {self.device_serial_id} exec-out screencap -p > {screenshot_path}",
                shell=True,
                check=True
            )
            
            # 读取截图文件并返回 PIL.Image 对象
            screenshot_image = PIL.Image.open(screenshot_path)
            
            # 清理临时文件
            os.unlink(screenshot_path)
            
            return screenshot_image
            
        except subprocess.CalledProcessError as e:
            # 清理临时文件
            if os.path.exists(screenshot_path):
                os.unlink(screenshot_path)
            raise Exception(f"ADB 截图命令执行失败: {str(e)}")
        except Exception as e:
            # 清理临时文件
            if os.path.exists(screenshot_path):
                os.unlink(screenshot_path)
            raise Exception(f"截图过程中发生错误: {str(e)}")

    def take_screenshot_websocket(self):
        try:
            screenshot_base64 = self._send_command('screenshot')

            screenshot_base64 = screenshot_base64['data']

            screenshot_bytes = decode_base64(screenshot_base64)

            # 尝试在保存之前验证数据
            if len(screenshot_bytes) == 0:
                raise ValueError("Received empty screenshot data")

            screenshot_image = PIL.Image.open(io.BytesIO(screenshot_bytes))
            
            return screenshot_image

        except Exception as e:
            logger.info(f"📷 获取界面截图失败，可能是当前设备未连接，或者当前设备处于隐私保护界面，无法截图，请检查设备连接状态并重试")
            logger.debug(f"获取界面截图失败，错误信息: {str(e)}")
            raise

    def take_screenshot_impl(self, save_path=None) -> PIL.Image:
        """
        获取设备截图
        - 根据 prefer_phone_action_type 配置选择截图方式
        - websocket: 仅使用 WebSocket 方式
        - adb: 优先使用 ADB 方式，失败时尝试 WebSocket
        
        Args:
            save_path: 可选的保存路径
        Returns:
            PIL.Image: 截图图像
        """
        while self.agent._enabled:
            if self.config.prefer_phone_action_type == 'websocket':
                # WebSocket 模式：仅使用 WebSocket 截图
                try:
                    screenshot_image = self.take_screenshot_websocket()
                    logger.debug("使用 WebSocket 方式截图成功")
                    return screenshot_image
                except Exception as websocket_error:
                    logger.info(f"📷 获取界面截图失败，可能是当前设备未连接，或者当前设备处于隐私保护界面，无法截图，请检查设备连接状态并重试")
                    logger.debug(f"WebSocket截图失败: {str(websocket_error)}")
                    
                    # 请求用户手动接管
                    try:
                        task_language = getattr(self.agent, 'task_language', 'zh')
                        if task_language == 'en':
                            message = f'Failed to capture screenshot from device "{self.device_name}". The device may be disconnected or in a privacy protection screen. Please manually handle it and click "Takeover Ended" when done.'
                        else:
                            message = f'获取设备 "{self.device_name}" 的界面截图失败，可能是当前设备未连接，或者当前设备处于隐私保护界面，无法截图，请您手动进行处理，处理完成后点击"接管结束"按钮'
                        
                        logger.info(f"⚠️ {message}")
                        self.agent.user.request_manual_takeover(message, timeout=30)
                    except Exception as takeover_error:
                        logger.debug(f"请求手动接管失败: {str(takeover_error)}")
                    
                    # 接管结束后，继续循环重试截图
            else:
                # ADB 模式：优先 ADB，失败时尝试 WebSocket
                try:
                    screenshot_image = self.take_screenshot_adb()
                    logger.debug("使用 ADB 方式截图成功")
                    return screenshot_image
                except Exception as adb_error:
                    logger.debug(f"ADB截图失败: {str(adb_error)}, 尝试WebSocket方式")
                    
                    try:
                        screenshot_image = self.take_screenshot_websocket()
                        logger.debug("使用 WebSocket 方式截图成功（回退方案）")
                        return screenshot_image
                    except Exception as websocket_error:
                        logger.info(f"📷 获取界面截图失败，可能是当前设备未连接，或者当前设备处于隐私保护界面，无法截图，请检查设备连接状态并重试")
                        logger.debug(f"ADB截图失败: {str(adb_error)}")
                        logger.debug(f"WebSocket截图失败: {str(websocket_error)}")
                        
                        # 请求用户手动接管
                        try:
                            task_language = getattr(self.agent, 'task_language', 'zh')
                            if task_language == 'en':
                                message = f'Failed to capture screenshot from device "{self.device_name}". The device may be disconnected or in a privacy protection screen. Please manually handle it and click "Takeover Ended" when done.'
                            else:
                                message = f'获取设备 "{self.device_name}" 的界面截图失败，可能是当前设备未连接，或者当前设备处于隐私保护界面，无法截图，请您手动进行处理，处理完成后点击"接管结束"按钮'
                            
                            logger.info(f"⚠️ {message}")
                            self.agent.user.request_manual_takeover(message, timeout=30)
                        except Exception as takeover_error:
                            logger.debug(f"请求手动接管失败: {str(takeover_error)}")
                        
                        # 接管结束后，继续循环重试截图

    def start_screen_record(self) -> bool:
        res = self._send_command('start_screen_record')
        return True

    def stop_screen_record(self) -> bool:
        res = self._send_command('stop_screen_record')
        return True

    def show_highlight(self, x, y, radius) -> bool:
        res = self._send_command(f'show_highlight,{x},{y},{radius}')
        return True

    def hide_highlight(self) -> bool:
        res = self._send_command('hide_highlight')
        return True

    def set_clipboard(self, text) -> bool:
        # 确保text是字符串类型
        text_str = str(text)
        _ = self._send_command('set_clipboard,' + text_str)
        return True

    def get_clipboard(self) -> str:
        res = self._send_command('get_clipboard')
        return res['message']

    def expand_notification_panel(self):
        _ = self._send_command('expand_notification')
        return True

    def get_ui_tree(self):
        """
        Get the UI tree of the current device.
        """
        try:
            res = self._send_command('view_hierarchy')

            if 'message' not in res:
                raise Exception('Invalid response, missing message while getting current state, please check the device recording premissions')
            views = res['message']
            try:
                views = json.loads(views) if isinstance(views, str) else views
            except Exception as e:
                views = []
        except:
            views = []
        return views

    def _get_device_port(self, device_name: str) -> int:
        """根据设备名称获取对应的端口号（仅 websocket 设备使用）
        
        优先从 phone_port_mappings（设备名称 -> 端口）中查找，未找到则回退到默认 device_port。
        """
        try:
            mappings = getattr(self.config, 'phone_port_mappings', None)
            if mappings:
                matched_name = self._find_device_with_bilingual_match(device_name, mappings)
                if matched_name and matched_name in mappings:
                    port = mappings[matched_name]
                    logger.debug(f"Found port {port} for device {device_name} (matched: {matched_name}) in phone_port_mappings")
                    return int(port)
            default_port = getattr(self.config, 'device_port', 51825)
            logger.debug(f"Using default port {default_port} for device {device_name}")
            return int(default_port)
        except Exception as e:
            logger.debug(f"Failed to get port for device {device_name}: {str(e)}, using default port")
            return int(getattr(self.config, 'device_port', 51825))

    def _do_device_switch(self, device_name: str, device_id: str) -> bool:
        """执行WebSocket设备的切换操作"""
        try:
            # 关闭现有连接
            self._close_device()

            # 更新设备序列号
            self.device_serial_id = device_id
            
            # 获取设备对应的端口号（基于设备名称）
            device_port = self._get_device_port(device_name)
            logger.info(f"🔄 切换到设备 “{device_name}”")
            
            # 转发端口（不在执行，由 Electron 主进程执行）
            # os.system(f"adb -s {device_id} forward tcp:{device_port} tcp:6666")
            
            # 获取新的设备 URL
            device_url = f"ws://localhost:{device_port}"
            self.server_address = device_url

            # 重新建立WebSocket连接
            self.websocket_client = WebSocketClient(self.server_address)
            self.websocket_client.start()
            
            return True
        except Exception as e:
            logger.error(f"❌ 切换到设备 '{device_name}' 失败")
            logger.debug(f"Failed to switch WebSocket device to '{device_name}': {str(e)}")
            return False

    def start_recording(self, output_path=None, use_scrcpy_native=True, quality='high'):
        """开始视频录制，优先使用原生scrcpy

        Args:
            output_path (str, optional): 输出视频文件路径。如果未指定，将自动生成。
            use_scrcpy_native (bool): 是否优先使用原生scrcpy录制，默认为True
            quality (str): 录制质量 ('low', 'medium', 'high')

        Returns:
            str: 录制文件路径

        Raises:
            RuntimeError: 当已经在录制时
            ImportError: 当视频编码服务不可用时
        """
        if self.recording_active:
            raise RuntimeError("录制已经在进行中")

        # Store quality setting
        self.recording_quality = quality

        # Try native scrcpy recording first (highest priority)
        if use_scrcpy_native and self.scrcpy_recorder.is_available():
            try:
                return self._start_scrcpy_native_recording(output_path, quality)
            except Exception as e:
                logger.warning(f"原生scrcpy录制不可用，回退到截图录制: {str(e)}")

        # Use traditional screenshot-based recording as fallback
        return super().start_recording(output_path)

    def _start_scrcpy_native_recording(self, output_path=None, quality='medium'):
        """使用原生scrcpy进行视频录制

        Args:
            output_path (str, optional): 输出视频文件路径
            quality (str): 录制质量 ('low', 'medium', 'high')

        Returns:
            str: 录制文件路径
        """
        # Generate output path if not provided
        if output_path is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            task_name = getattr(self.agent, "current_task_name", "task")
            output_path = os.path.join(
                getattr(self.agent, "workspace_path", os.getcwd()),
                "recordings",
                f"{timestamp}_websocket_scrcpy_recording_{self.device_name}.mp4"
            )

        # Ensure recordings directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        # Reset recording state
        self.recording_output_path = output_path
        self.recording_stop_requested = False
        self.recording_frames = []
        self.recording_start_time = time.time()
        self.recording_stop_time = None
        self.recording_active = True
        self.recording_method = "scrcpy_native"

        # Initialize recording metadata
        task_name = getattr(self.agent, 'current_task_name', 'task')
        self.recording_metadata = {
            'task_name': task_name,
            'device_id': self.device_serial_id,
            'device_name': self.device_name,
            'device_type': 'WebsocketDevice',
            'websocket_address': self.server_address,
            'start_time': self.recording_start_time,
            'start_time_iso': datetime.fromtimestamp(self.recording_start_time).isoformat(),
            'stop_time': None,
            'stop_time_iso': None,
            'duration_seconds': None,
            'file_size_bytes': None,
            'frame_count': 0,
            'output_path': output_path,
            'gui_actions': [],
            'action_timeline_summary': {},
            'video_format': 'mp4',
            'recording_source': 'websocket_scrcpy_native',
            'quality_preset': quality
        }

        logger.info(f"🎬 Starting WebSocket native scrcpy recording: {output_path}")
        logger.info(f"📱 Device: {self.device_serial_id}")
        logger.info(f"🔗 WebSocket: {self.server_address}")
        logger.info(f"⚙️ Quality: {quality}")

        try:
            # Start recording using scrcpy recorder with device serial ID
            video_path = self.scrcpy_recorder.start_recording(
                output_path=output_path,
                device_id=self.device_serial_id,
                quality=quality,
                max_duration=3600,  # 1 hour max
                stay_awake=True,
                show_touches=False,
                record_format='mp4'
            )

            logger.info(f"✅ WebSocket native scrcpy recording started successfully: {video_path}")
            return video_path

        except Exception as e:
            self.recording_active = False
            self.recording_method = None
            raise Exception(f"Failed to start WebSocket native scrcpy recording: {str(e)}")

    def stop_recording(self):
        """停止视频录制

        Returns:
            str: 录制视频文件路径，如果录制失败返回None
        """
        if not self.recording_active:
            logger.warning("没有进行中的录制可以停止")
            return None

        logger.info("⏹️ 停止WebSocket视频录制...")

        # Capture stop time
        self.recording_stop_time = time.time()

        # Handle different recording methods
        if getattr(self, 'recording_method', None) == 'scrcpy_native':
            return self._stop_scrcpy_native_recording()
        else:
            # Use traditional recording stop
            return super().stop_recording()

    def _stop_scrcpy_native_recording(self):
        """停止WebSocket原生scrcpy录制

        Returns:
            str: 录制视频文件路径
        """
        logger.info("⏹️ 停止WebSocket原生scrcpy录制...")

        try:
            # Stop scrcpy recording
            video_path = self.scrcpy_recorder.stop_recording()

            if video_path:
                # Update metadata
                if self.recording_metadata:
                    self.recording_metadata.update({
                        'stop_time': self.recording_stop_time,
                        'stop_time_iso': datetime.fromtimestamp(self.recording_stop_time).isoformat(),
                        'gui_actions': self.recording_action_timeline,
                    })

                    if self.recording_start_time:
                        duration = self.recording_stop_time - self.recording_start_time
                        self.recording_metadata['duration_seconds'] = duration

                    # Get recording stats
                    stats = self.scrcpy_recorder.get_recording_stats()
                    if stats.get('file_size'):
                        self.recording_metadata['file_size_bytes'] = stats['file_size']

                    # Calculate action timeline summary
                    if self.recording_action_timeline:
                        action_types = [action['action_type'] for action in self.recording_action_timeline]
                        action_counts = {}
                        for action_type in action_types:
                            action_counts[action_type] = action_types.count(action_type)

                        self.recording_metadata['action_timeline_summary'] = {
                            'total_actions': len(self.recording_action_timeline),
                            'actions_per_second': len(self.recording_action_timeline) / duration if duration > 0 else 0,
                            'most_common_action': max(action_counts, key=action_counts.get) if action_counts else None,
                            'action_distribution': action_counts,
                            'first_action_time': self.recording_action_timeline[0]['timestamp_iso'] if self.recording_action_timeline else None,
                            'last_action_time': self.recording_action_timeline[-1]['timestamp_iso'] if self.recording_action_timeline else None
                        }

                # Save metadata to a JSON file alongside the video
                if video_path and self.recording_metadata:
                    metadata_path = video_path.replace('.mp4', '_metadata.json')
                    import json
                    with open(metadata_path, 'w', encoding='utf-8') as f:
                        json.dump(self.recording_metadata, f, indent=2, ensure_ascii=False)

                # Update recording state
                self.recording_active = False

                duration = self.recording_stop_time - self.recording_start_time if self.recording_start_time else 0
                logger.info(f"✅ WebSocket原生scrcpy录制完成: {video_path} (时长: {duration:.1f}s)")

                return video_path
            else:
                logger.error("❌ WebSocket原生scrcpy录制失败: 没有返回文件路径")
                return None

        except Exception as e:
            logger.error(f"❌ 停止WebSocket原生scrcpy录制异常: {str(e)}")
            return None
        finally:
            # Cleanup state
            self.recording_active = False
            self.recording_method = None


def decode_base64(data):
    """Decode base64, padding being optional.

    :param data: Base64 data as an ASCII byte string
    :returns: The decoded byte string.

    """
    # missing_padding = len(data) % 4
    # if missing_padding != 0:
    #     data += b'=' * (4 - missing_padding)
    return base64.b64decode(data)


if __name__ == '__main__':
    device = WebsocketController('ws://192.168.20.201:6666', 'output')
    device._open()
    print(device.get_current_state())

    device.start_app('Contacts')

    device.show_highlight(10, 10, 10)
    time.sleep(2)
    device.hide_highlight()

    # device.long_touch(1000, 210, 1000)
    # device.view_set_text('hello world')
    # device.back()
    # device.home()
    # time.sleep(1)
    # device.take_screenshot()
    # device.disconnect()
