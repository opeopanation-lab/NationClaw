from typing import Optional, Union
from PIL import Image
import os
import structlog
import requests
import threading
import time
import queue
from datetime import datetime
import numpy as np

from mobileclaw.utils.interface import UniInterface

logger = structlog.get_logger(__name__)

class DeviceControllerBase(UniInterface):
    def __init__(self, agent, device_name: str, device_id: str):
        super().__init__(agent)
        self._tag = 'device'
        self.device_bound = None
        self.device_name = device_name
        self.device_id = device_id

        self.width = 0
        self.height = 0
        
        # Recording state management
        self.recording_active = False
        self.recording_thread = None
        self.recording_frames = []
        self.recording_frame_queue = queue.Queue(maxsize=100)  # Limit queue size to prevent memory overflow
        self.recording_start_time = None
        self.recording_stop_time = None
        self.recording_stop_requested = False
        self.recording_output_path = None
        self.recording_metadata = {}  # Store recording metadata
        
        # GUI action recording state
        self.recording_action_timeline = []  # Store action events during recording
        self.recording_frame_actions = {}      # Map frame_number -> action_events
        self.recording_action_counter = 0      # Unique action identifier
        self._last_model_input_scale_x = 1.0
        self._last_model_input_scale_y = 1.0

    def __str__(self) -> str:
        return f"Device Interface: {self.device_name}"

    def execute_task(self, task: str, max_steps: int = 6, keep_recent_images: int = 3):
        """
        Execute a device control task using iterative LLM-generated Python code.

        Args:
            task: Task description (e.g., "Navigate to settings and enable dark mode")
            max_steps: Maximum number of steps to execute
            keep_recent_images: Number of recent images to keep in context (default: 3)

        Returns:
            tuple: (actions_and_results, screenshots)
                - actions_and_results: list of text strings and image tuples
                - screenshots: list of (screen_path, screen_base64) tuples from each step
        """
        import re
        import base64
        from io import BytesIO
        from datetime import datetime

        # Initialize tracking lists
        notes = []
        results = []
        actions_and_results = []
        screenshots = []  # List of (screen_path, screen_base64) tuples from each step
        note_screenshots = []  # List of (path, base64) tuples from take_note_screenshot
        self._last_model_input_scale_x = 1.0
        self._last_model_input_scale_y = 1.0

        # Get device type
        from mobileclaw.device.computer import ComputerDeviceBase
        from mobileclaw.device.browser import BrowserDeviceController
        if isinstance(self, ComputerDeviceBase):
            device_type = 'computer'
            task_tag = '💻'
        elif isinstance(self, BrowserDeviceController):
            device_type = 'browser'
            task_tag = '🌎'
        else:
            device_type = 'phone'
            task_tag = '📱'

        # Log and report task start
        logger.info(f"🚀 Starting device task: {task}")
        self.agent._log_and_report(f'Start device task: {task}', actions_and_results, task_tag=task_tag)

        # Create DeviceAPI instance for execution (persists across steps for task_status tracking)
        device_api = self._create_device_api_for_execution(notes, results, actions_and_results, note_screenshots)

        for step in range(max_steps):
            if not self.agent._enabled:
                self.agent._log_and_report('Device task interrupted because agent is stopping.', actions_and_results, task_tag=task_tag)
                break

            # Pause if a message is being handled
            if hasattr(self.agent, '_message_pause_event'):
                while self.agent._enabled:
                    if self.agent._message_pause_event.wait(timeout=0.2):
                        break
                if not self.agent._enabled:
                    self.agent._log_and_report('Device task interrupted because agent is stopping.', actions_and_results, task_tag=task_tag)
                    break

            # Take screenshot
            screenshot = self.take_screenshot()

            model_screenshot, scale_x, scale_y = self._prepare_screenshot_for_model(screenshot)
            self._last_model_input_scale_x = scale_x
            self._last_model_input_scale_y = scale_y

            # Save screenshot to temp file and convert to base64
            img_byte_arr = BytesIO()
            model_screenshot.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()
            screenshot_base64 = base64.b64encode(img_bytes).decode('utf-8')
            img_byte_arr.close()

            try:
                screenshots_dir = os.path.join(self.agent.file.agent_temp_dir, 'screenshots')
                os.makedirs(screenshots_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"device_step_{step}_{timestamp}.png"
                filepath = os.path.join(screenshots_dir, filename)
                model_screenshot.save(filepath, format='PNG')
                screen_path = os.path.relpath(filepath, self.agent.file.agent_dir)
            except Exception as e:
                logger.warning(f"Failed to save screenshot to file: {e}")
                screen_path = f"device_step_{step}_{datetime.now().strftime('%H%M%S')}"

            screenshots.append((screen_path, screenshot_base64))
            self.agent._log_and_report(f'Step {step} Screen: {screen_path}', actions_and_results, task_tag=task_tag)

            # Build images from most recent step screenshots and note screenshots
            recent_screens = screenshots[-(keep_recent_images):] if keep_recent_images > 0 else screenshots
            recent_notes = note_screenshots[-(keep_recent_images):] if keep_recent_images > 0 else note_screenshots
            images = recent_screens + recent_notes

            # Prepare params for device_use_step
            if len(actions_and_results) > self.agent.actions_and_results_max_len:
                actions_and_results = actions_and_results[-self.agent.actions_and_results_max_len:]
            params = {
                'task': task,
                'actions_and_results': actions_and_results,
                'device_type': device_type,
                'current_screen': screenshot_base64,
                'images': images,
            }

            # Call device_use_step API
            thought, code = self.agent.fm.call_func('device_use_step', params)

            if not self.agent._enabled:
                self.agent._log_and_report('Device task interrupted because agent is stopping.', actions_and_results, task_tag=task_tag)
                break

            # Store screenshot in actions_and_results for history
            actions_and_results.append((screen_path, screenshot_base64))

            # Add thought to results
            if thought:
                self.agent._log_and_report(f'Step {step} Thought: {thought}', actions_and_results, task_tag=task_tag)

            # Stop if no code generated
            if not code:
                warning_msg = f"Step {step} Error: No code parsed from the response. Perhaps forgot to wrap code in code block?"
                logger.warning(warning_msg)
                self.agent._log_and_report(warning_msg, actions_and_results, task_tag=task_tag)
                continue

            self.agent._log_and_report(f'Step {step} Action: `{code}`', actions_and_results, task_tag=task_tag)

            # Execute the code
            try:
                # Create execution environment
                exec_globals = {
                    'device': device_api,
                }

                # Execute the generated code
                exec(code, exec_globals)

                # Check task status from device API
                if device_api._task_status != 'ongoing':
                    logger.info(f"✅ Task status: {device_api._task_status}")
                    self.agent._log_and_report(f'Task status: {device_api._task_status}', actions_and_results, task_tag=task_tag)
                    break

            except Exception as e:
                error_msg = f"Error: step {step} was failed - {e}"
                logger.error(error_msg)
                results.append(f"{error_msg}")
                self.agent._log_and_report(error_msg, actions_and_results, task_tag=task_tag)
                continue # NOTE: decide between break or continue

            # Sleep between steps
            self.agent.sleep(0.5)
        if step + 1 >= max_steps and device_api._task_status == 'ongoing':
            self.agent._log_and_report(f'[WARNING] Task stopped due to step limit: {max_steps}.', actions_and_results, task_tag=task_tag)

        # Take a final screenshot and add to results
        try:
            final_screenshot = self.take_screenshot()
            final_model_screenshot, _, _ = self._prepare_screenshot_for_model(final_screenshot)
            img_byte_arr = BytesIO()
            final_model_screenshot.save(img_byte_arr, format='PNG')
            final_base64 = base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')
            img_byte_arr.close()

            screenshots_dir = os.path.join(self.agent.file.agent_temp_dir, 'screenshots')
            os.makedirs(screenshots_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(screenshots_dir, f"device_final_{timestamp}.png")
            final_model_screenshot.save(filepath, format='PNG')
            final_path = os.path.relpath(filepath, self.agent.file.agent_dir)

            screenshots.append((final_path, final_base64))
            actions_and_results.append((final_path, final_base64))
            self.agent._log_and_report(f'Final Screen: {final_path}', actions_and_results, task_tag=task_tag)
        except Exception as e:
            error_msg = f"[WARNING] Failed to take final screenshot: {e}"
            self.agent._log_and_report(error_msg)

        return actions_and_results, screenshots

    def _create_device_api_for_execution(self, notes, results, actions_and_results, note_screenshots):
        """
        Create a DeviceAPI object that can be used in code execution.
        This object provides the device control APIs while preventing direct access to internal state.

        Args:
            notes: List to append text notes to
            results: List to append task results to
            actions_and_results: List to append actions and results (including images) to
            note_screenshots: List to append (path, base64) tuples from take_note_screenshot
        """
        class DeviceAPI:
            """Device control API for task execution."""
            def __init__(self, device_controller, notes, results, actions_and_results, note_screenshots):
                self._device = device_controller
                self._notes = notes
                self._results = results
                self._actions_and_results = actions_and_results
                self._note_screenshots = note_screenshots
                self._task_status = 'ongoing'

            def end_task(self, status):
                """End the current task with a status.

                Args:
                    status: 'finished', 'failed', or 'infeasible'
                """
                if status not in ('finished', 'failed', 'infeasible'):
                    raise ValueError(f"Invalid task status: {status}. Must be 'finished', 'failed', or 'infeasible'.")
                self._task_status = status

            # Common device actions
            def click(self, x, y):
                """Click at coordinates (x, y)"""
                scaled_x, scaled_y = self._device._scale_coordinates_if_needed(x, y)
                return self._device.click(scaled_x, scaled_y)

            def view_set_text(self, content):
                """Type text content"""
                return self._device.view_set_text(content)

            def type(self, content):
                """Type text into the active input field"""
                return self._device.view_set_text(content)

            def enter(self):
                """Press Enter key"""
                return self._device.enter()

            def scroll(self, direction, start_xy=None):
                """Scroll in direction ('up', 'down', 'left', 'right')"""
                if start_xy:
                    scaled_x, scaled_y = self._device._scale_coordinates_if_needed(start_xy[0], start_xy[1])
                    return self._device.scroll(direction, start_xy=(scaled_x, scaled_y))
                else:
                    return self._device.scroll(direction, start_xy=None)

            def drag(self, start_xy, end_xy):
                """Drag from start_xy to end_xy"""
                scaled_x1, scaled_y1 = self._device._scale_coordinates_if_needed(start_xy[0], start_xy[1])
                scaled_x2, scaled_y2 = self._device._scale_coordinates_if_needed(end_xy[0], end_xy[1])
                return self._device.drag((scaled_x1, scaled_y1), (scaled_x2, scaled_y2))

            def swipe(self, start_xy, end_xy):
                """Swipe from start_xy to end_xy"""
                scaled_x1, scaled_y1 = self._device._scale_coordinates_if_needed(start_xy[0], start_xy[1])
                scaled_x2, scaled_y2 = self._device._scale_coordinates_if_needed(end_xy[0], end_xy[1])
                return self._device.drag((scaled_x1, scaled_y1), (scaled_x2, scaled_y2))

            def back(self):
                """Go back"""
                return self._device.back()

            def home(self):
                """Go to home"""
                return self._device.home()

            def start_app(self, app_name):
                """Start an application"""
                return self._device.start_app(app_name)

            # Computer-specific actions
            def double_click(self, x, y):
                """Double-click at coordinates (x, y)"""
                scaled_x, scaled_y = self._device._scale_coordinates_if_needed(x, y)
                return self._device.double_click(scaled_x, scaled_y)

            def right_click(self, x, y):
                """Right-click at coordinates (x, y)"""
                scaled_x, scaled_y = self._device._scale_coordinates_if_needed(x, y)
                return self._device.right_click(scaled_x, scaled_y)

            def hotkey(self, keys):
                """Press hotkey combination (e.g., 'ctrl c', 'cmd v')"""
                return self._device.hotkey(keys)

            # Phone/Browser-specific actions
            def long_click(self, x, y):
                """Long press at coordinates (x, y)"""
                scaled_x, scaled_y = self._device._scale_coordinates_if_needed(x, y)
                return self._device.long_click(scaled_x, scaled_y)

            def long_touch(self, x, y):
                """Long press at coordinates (x, y)"""
                scaled_x, scaled_y = self._device._scale_coordinates_if_needed(x, y)
                return self._device.long_touch(scaled_x, scaled_y)

            def open_url(self, url):
                """Open URL in browser"""
                return self._device.open_url(url)

            # Note-taking actions
            def take_note(self, text):
                """Record a text note"""
                self._notes.append(text)

            def take_note_screenshot(self, description, bbox=None):
                """
                Take and record a screenshot note with description.
                bbox is optional bounding box (x1, y1, x2, y2) to crop the screenshot.
                Coordinates in bbox will be scaled if needed.
                """
                import base64
                from io import BytesIO

                # Take screenshot
                screenshot = self._device.take_screenshot()

                # Crop if bbox is provided (scale coordinates first)
                if bbox is not None:
                    # bbox should be (x1, y1, x2, y2) - scale all coordinates
                    x1, y1, x2, y2 = bbox
                    scaled_x1, scaled_y1 = self._device._scale_coordinates_if_needed(x1, y1)
                    scaled_x2, scaled_y2 = self._device._scale_coordinates_if_needed(x2, y2)
                    screenshot = screenshot.crop((scaled_x1, scaled_y1, scaled_x2, scaled_y2))

                # Convert to base64
                img_byte_arr = BytesIO()
                screenshot.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()
                screenshot_base64 = base64.b64encode(img_bytes).decode('utf-8')
                img_byte_arr.close()

                # Count existing screenshots to generate unique path
                # Count image tuples in actions_and_results
                screenshot_count = sum(1 for item in self._actions_and_results if isinstance(item, tuple) and len(item) == 2)
                screenshot_path = f"noted_image_{screenshot_count}.png"

                # Add to note_screenshots and actions_and_results
                self._note_screenshots.append((screenshot_path, screenshot_base64))
                self._actions_and_results.append((screenshot_path, screenshot_base64))

                # Add text note with description
                self._notes.append(f"Screenshot: {description}")
                self._notes.append((screenshot_path, screenshot_base64))
                return screenshot

            # Result recording actions
            def record_result(self, content):
                """Record a task result (text)"""
                self._results.append(f"Result: {content}")

            def record_result_screenshot(self, description, bbox=None):
                """
                Take and record a screenshot of task result with description.
                bbox is optional bounding box (x1, y1, x2, y2) to crop the screenshot.
                Coordinates in bbox will be scaled if needed.
                """
                import base64
                from io import BytesIO

                # Take screenshot
                screenshot = self._device.take_screenshot()

                # Crop if bbox is provided (scale coordinates first)
                if bbox is not None:
                    # bbox should be (x1, y1, x2, y2) - scale all coordinates
                    x1, y1, x2, y2 = bbox
                    scaled_x1, scaled_y1 = self._device._scale_coordinates_if_needed(x1, y1)
                    scaled_x2, scaled_y2 = self._device._scale_coordinates_if_needed(x2, y2)
                    screenshot = screenshot.crop((scaled_x1, scaled_y1, scaled_x2, scaled_y2))

                # Convert to base64
                img_byte_arr = BytesIO()
                screenshot.save(img_byte_arr, format='PNG')
                img_bytes = img_byte_arr.getvalue()
                screenshot_base64 = base64.b64encode(img_bytes).decode('utf-8')
                img_byte_arr.close()

                # Count existing screenshots to generate unique path
                # Count image tuples in actions_and_results
                screenshot_count = sum(1 for item in self._actions_and_results if isinstance(item, tuple) and len(item) == 2)
                screenshot_path = f"result_image_{screenshot_count}.png"

                # Add to actions_and_results as an image tuple
                self._actions_and_results.append((screenshot_path, screenshot_base64))

                # Add text result with description
                self._results.append(f"Result Screenshot: {description}")
                self._results.append((screenshot_path, screenshot_base64))
                return screenshot

        return DeviceAPI(self, notes, results, actions_and_results, note_screenshots)

    def _execute_device_action(self, action_str: str, device_type: str, notes: list):
        """
        Execute a device action parsed from LLM response.

        Args:
            action_str: Action string to execute
            device_type: Type of device ('computer', 'phone', 'browser')
            notes: List to append notes to
        """
        import re

        # Handle note-taking actions
        if action_str.startswith('take_note('):
            text_match = re.search(r"text='([^']*)'", action_str)
            if text_match:
                text = text_match.group(1)
                notes.append(text)
                logger.debug(f"📝 Note recorded: {text}")
                return

        elif action_str.startswith('take_note_screenshot('):
            # Take screenshot and add to notes
            screenshot = self.take_screenshot()
            notes.append(f"Screenshot captured at step")
            logger.debug(f"📸 Screenshot note recorded")
            return

        # Handle device control actions
        if action_str.startswith('click('):
            point_match = re.search(r"point='<point>(\d+)\s+(\d+)</point>'", action_str)
            if point_match:
                x, y = int(point_match.group(1)), int(point_match.group(2))
                # Scale coordinates if needed
                scaled_x, scaled_y = self._scale_coordinates_if_needed(x, y)
                self.click(scaled_x, scaled_y)
                logger.debug(f"👆 Clicked at ({scaled_x}, {scaled_y})")

        elif action_str.startswith('type('):
            content_match = re.search(r"content='([^']*)'", action_str)
            if content_match:
                content = content_match.group(1)
                # Check if content ends with \n (submit)
                if content.endswith('\\n'):
                    actual_content = content[:-2]
                    self.view_set_text(actual_content)
                    time.sleep(0.1)
                    self.enter()
                    logger.debug(f"⌨️ Typed and submitted: {repr(actual_content)}")
                else:
                    self.view_set_text(content)
                    logger.debug(f"⌨️ Typed: {repr(content)}")

        elif action_str.startswith('scroll('):
            point_match = re.search(r"'<point>(\d+)\s+(\d+)</point>'", action_str)
            direction_match = re.search(r"direction='([^']+)'", action_str)
            if point_match and direction_match:
                x, y = int(point_match.group(1)), int(point_match.group(2))
                scaled_x, scaled_y = self._scale_coordinates_if_needed(x, y)
                direction = direction_match.group(1)
                self.scroll(direction, start_xy=(scaled_x, scaled_y))
                logger.debug(f"📜 Scrolled {direction} at ({scaled_x}, {scaled_y})")

        elif action_str.startswith('drag('):
            start_match = re.search(r"start_point='<point>(\d+)\s+(\d+)</point>'", action_str)
            end_match = re.search(r"end_point='<point>(\d+)\s+(\d+)</point>'", action_str)
            if start_match and end_match:
                x1, y1 = int(start_match.group(1)), int(start_match.group(2))
                x2, y2 = int(end_match.group(1)), int(end_match.group(2))
                scaled_x1, scaled_y1 = self._scale_coordinates_if_needed(x1, y1)
                scaled_x2, scaled_y2 = self._scale_coordinates_if_needed(x2, y2)
                self._do_drag((scaled_x1, scaled_y1), (scaled_x2, scaled_y2))
                logger.debug(f"✋ Dragged from ({scaled_x1}, {scaled_y1}) to ({scaled_x2}, {scaled_y2})")

        elif action_str.startswith('open_app(') or action_str.startswith('start_app('):
            app_match = re.search(r"app_name='([^']+)'", action_str)
            if app_match:
                app_name = app_match.group(1)
                self.start_app(app_name)
                logger.debug(f"📱 Started app: {app_name}")

        elif action_str.startswith('back('):
            self.back()
            logger.debug(f"⬅️ Pressed back")

        elif action_str.startswith('home('):
            self.home()
            logger.debug(f"🏠 Pressed home")

        elif action_str.startswith('enter('):
            self.enter()
            logger.debug(f"↩️ Pressed enter")

        # Computer-specific actions
        elif device_type == 'computer':
            if action_str.startswith('left_double(') or action_str.startswith('double_click('):
                point_match = re.search(r"point='<point>(\d+)\s+(\d+)</point>'", action_str)
                if point_match:
                    x, y = int(point_match.group(1)), int(point_match.group(2))
                    scaled_x, scaled_y = self._scale_coordinates_if_needed(x, y)
                    self.double_click(scaled_x, scaled_y)
                    logger.debug(f"👆 Double-clicked at ({scaled_x}, {scaled_y})")

            elif action_str.startswith('right_single(') or action_str.startswith('right_click('):
                point_match = re.search(r"point='<point>(\d+)\s+(\d+)</point>'", action_str)
                if point_match:
                    x, y = int(point_match.group(1)), int(point_match.group(2))
                    scaled_x, scaled_y = self._scale_coordinates_if_needed(x, y)
                    self.right_click(scaled_x, scaled_y)
                    logger.debug(f"🖱️ Right-clicked at ({scaled_x}, {scaled_y})")

            elif action_str.startswith('hotkey('):
                key_match = re.search(r"key='([^']+)'", action_str)
                if key_match:
                    keys = key_match.group(1)
                    self.hotkey(keys)
                    logger.debug(f"⌨️ Pressed hotkey: {keys}")

        # Phone/Browser-specific actions
        elif device_type in ['phone', 'browser']:
            if action_str.startswith('long_press(') or action_str.startswith('long_click('):
                point_match = re.search(r"point='<point>(\d+)\s+(\d+)</point>'", action_str)
                if point_match:
                    x, y = int(point_match.group(1)), int(point_match.group(2))
                    scaled_x, scaled_y = self._scale_coordinates_if_needed(x, y)
                    if device_type == 'browser':
                        self.long_touch(scaled_x, scaled_y)
                    else:
                        self.long_click(scaled_x, scaled_y)
                    logger.debug(f"👆 Long-pressed at ({scaled_x}, {scaled_y})")

            elif action_str.startswith('open_url('):
                url_match = re.search(r"url='([^']+)'", action_str)
                if url_match:
                    url = url_match.group(1)
                    self.open_url(url)
                    logger.debug(f"🌐 Opened URL: {url}")

    def get_width_height(self):
        """
        Get device width and height. Can be overridden by subclasses.
        Default implementation returns cached width/height.

        Returns:
            tuple: (width, height) in pixels
        """
        return (self.width, self.height)

    def _prepare_screenshot_for_model(self, screenshot: Image.Image) -> tuple[Image.Image, float, float]:
        max_size = self.agent.config.gui_max_screenshot_width
        original_width, original_height = screenshot.size
        longest_side = max(original_width, original_height)

        if max_size is not None and longest_side <= max_size:
            return screenshot, 1.0, 1.0

        resize_ratio = max_size / longest_side
        resized_width = max(1, int(round(original_width * resize_ratio)))
        resized_height = max(1, int(round(original_height * resize_ratio)))
        resized = screenshot.resize((resized_width, resized_height), Image.Resampling.LANCZOS)
        logger.debug(
            f"Resize screenshot for model: {original_width}x{original_height} -> {resized_width}x{resized_height}"
        )
        return resized, resized_width / original_width, resized_height / original_height

    def _get_coordinate_mode(self) -> str:
        mode = str(getattr(self.agent.config, 'gui_coordinate_scale_mode', 'auto') or 'auto').strip().lower()
        if mode == 'never':
            return 'image_pixels'
        if mode == 'always':
            return 'scale_1000'
        if mode.startswith('scale_'):
            scale_value = mode[len('scale_'):]
            try:
                parsed_value = float(scale_value)
                if parsed_value > 0:
                    return f'scale_{parsed_value}'
            except ValueError:
                logger.warning(f"Invalid gui_coordinate_scale_mode: {mode}, fallback to auto")

        gui_vlm_name = ''
        if getattr(self.agent.config, 'use_custom_gui_vlm', False):
            gui_vlm_name = getattr(self.agent.config, 'custom_gui_vlm_name', '') or ''
        else:
            gui_vlm_name = getattr(self.agent.config, 'wisewk_gui_vlm_name', '') or ''

        gui_vlm_name = gui_vlm_name.lower()
        if gui_vlm_name.startswith('kimi'):
            return 'scale_1'
        if 'seed' in gui_vlm_name:
            return 'scale_1000'
        return 'image_pixels'

    def _scale_coordinates_if_needed(self, x: int | float, y: int | float) -> tuple:
        """
        Restore model coordinates to actual device dimensions.

        Args:
            x: X coordinate from model
            y: Y coordinate from model

        Returns:
            tuple: (scaled_x, scaled_y) in actual device coordinates
        """
        device_width, device_height = self.get_width_height()

        if device_width <= 0 or device_height <= 0:
            logger.error(f"Invalid device dimensions (width={device_width}, height={device_height}), using original coordinates")
            return (0, 0)

        coordinate_mode = self._get_coordinate_mode()

        if coordinate_mode.startswith('scale_'):
            scale_range = float(coordinate_mode[len('scale_'):])
            scaled_x = int(float(x) / scale_range * device_width)
            scaled_y = int(float(y) / scale_range * device_height)
            logger.debug(
                f"Scaled coordinates ({scale_range} range): ({x}, {y}) -> ({scaled_x}, {scaled_y}) "
                f"(device: {device_width}x{device_height})"
            )
        else:
            input_scale_x = getattr(self, '_last_model_input_scale_x', 1.0) or 1.0
            input_scale_y = getattr(self, '_last_model_input_scale_y', 1.0) or 1.0
            scaled_x = int(x / input_scale_x)
            scaled_y = int(y / input_scale_y)
            logger.debug(
                f"Restore image coordinates: ({x}, {y}) -> ({scaled_x}, {scaled_y}) "
                f"(input_scale={input_scale_x:.4f},{input_scale_y:.4f}, device: {device_width}x{device_height})"
            )

        # Ensure coordinates are within device bounds
        scaled_x = max(0, min(scaled_x, device_width - 1))
        scaled_y = max(0, min(scaled_y, device_height - 1))
        return (scaled_x, scaled_y)

    def _open(self):
        self._open_device()
        self.device_bound = (-1, -1, -1, -1)
        self.width = -1
        self.height = -1

    def _open_device(self):
        raise NotImplementedError("open_device not implemented")

    def _close(self):
        # Stop any active recording before closing device
        if self.recording_active:
            self.stop_recording()
        self._close_device()

    def _close_device(self):
        raise NotImplementedError("close_device not implemented")

    def take_picture(self, save_path=None):
        raise NotImplementedError("take_picture not implemented")

    def take_screenshot(self, save_path=None, hide_overlay = True):
        from mobileclaw.device.computer import ComputerDeviceBase

        def _take_screenshot():
            if isinstance(self, ComputerDeviceBase):
                image = self.take_screenshot_impl(save_path=save_path, hide_overlay=hide_overlay)
            else:
                image = self.take_screenshot_impl(save_path=save_path)
            return image

        image = _take_screenshot()

        for i in range(6):
            if self.check_black_screen(image):
                logger.info(f"Black screen detected on attempt {i+1}, retrying after 0.5s")
                self.agent.sleep(0.5)
                image = _take_screenshot()
            else:
                break

        if self.check_black_screen(image):
            logger.info(f"Black screen still detected after {i+1} attempts, requesting manual intervention")
            self._notify_black_screen()
            return _take_screenshot()
        else:
            self.width, self.height = image.size
            return image

    def take_screenshot_impl(self, save_path=None):
        raise NotImplementedError("take_screenshot_impl not implemented")

    def check_black_screen(self, image: Image.Image) -> bool:
        """检测截图是否为黑屏，如果是则通知用户
        
        黑屏判定标准：纯黑像素（RGB全为0）占比达到90%以上
        
        Args:
            image: PIL Image 对象
            
        Returns:
            bool: 如果是黑屏返回 True，否则返回 False
        """
        try:
            # 转换为RGB模式
            if image.mode == 'RGBA':
                img = image.convert('RGB')
            elif image.mode != 'RGB':
                img = image.convert('RGB')
            else:
                img = image
            
            # 转换为numpy数组
            pixels = np.array(img)
            
            # 获取图片尺寸
            height, width = pixels.shape[:2]
            total_pixels = height * width
            
            if total_pixels == 0:
                return False
            
            # 计算纯黑像素（RGB全为0）
            black_mask = (pixels[:, :, 0] == 0) & (pixels[:, :, 1] == 0) & (pixels[:, :, 2] == 0)
            black_pixels = np.sum(black_mask)
            black_ratio = black_pixels / total_pixels

            # 如果黑色像素占比超过95%，视为黑屏
            if black_ratio >= 0.95:
                return True
            
            return False
        except Exception as e:
            logger.debug(f"检测黑屏失败: {str(e)}")
            return False

    def _notify_black_screen(self):
        """通知用户截图为黑屏，并请求手动接管"""
        try:
            # 根据任务语言选择消息
            task_language = getattr(self.agent, 'task_language', 'en')
            if task_language == 'en':
                message = f'The screen captured from device "{self.device_name}" is black. The device may be in a privacy protection screen, and the task execution may encounter errors. Please manually handle it and click "Takeover Ended" when done.'
            else:
                message = f'检测到设备 "{self.device_name}" 的屏幕截图是黑屏，当前该设备可能处于隐私保护界面，任务执行过程可能会出错，请您手动进行处理，处理完成后点击"接管结束"按钮。'
            
            # 请求手动接管（会等待用户确认或超时）
            logger.info(f"⚠️ {message}")
            # self.agent.user.request_manual_takeover(message, timeout=30)
        except Exception as e:
            logger.debug(f"请求手动接管失败: {str(e)}")

    def start_app(self, app_name):
        # Record action for video if recording
        self.record_action_if_recording(
            "start_app",
            app_name=app_name
        )
        # Subclasses should override and call _notify_app_started after successful start
        raise NotImplementedError("start_app not implemented")
    
    def _get_app_info(self, app_name: str, **kwargs) -> dict:
        """获取应用信息（由子类实现）
        
        Args:
            app_name: 应用名称
            **kwargs: 其他可选参数（如 bundle_id 等）
            
        Returns:
            dict: 应用信息字典，包含以下字段：
                - bundle_id: 应用的唯一标识
                - name: 应用名称
                - type: 应用类型 (1: 电脑端, 3: Android端)
                - version: 应用版本
                - display_name: 显示名称（可选）
                - developer: 开发者（可选）
                - description: 描述（可选）
                - category: 类别（可选）
                - icon: 图标（可选）
        """
        raise NotImplementedError("_get_app_info not implemented")
    
    def _notify_app_started(self, app_name: str, **kwargs):
        """应用启动成功后，获取应用信息并发送到Flask后端
        
        Args:
            app_name: 启动的应用名称
            **kwargs: 其他可选参数（如 bundle_id 等），传递给 _get_app_info
        """
        if not self.agent.config.run_with_ide:
            return
        
        try:
            # 获取应用信息
            app_info = self._get_app_info(app_name, **kwargs)
            
            # 发送到 Flask 后端
            self._send_app_info_to_flask(app_info)
            
        except Exception as e:
            logger.debug(f"发送应用信息失败: {str(e)}")
    
    def _send_app_info_to_flask(self, app_info: dict):
        """将应用信息发送到Flask后端
        
        Args:
            app_info: 应用信息字典
        """
        try:
            flask_port = self.config.flask_port
            response = requests.post(
                f'http://localhost:{flask_port}/app_started',
                json={'app_info': app_info, 'task_id': getattr(self.agent.task, 'task_id', '')},
                headers={'Content-Type': 'application/json'},
                timeout=2
            )
            
            if response.status_code == 200:
                logger.debug(f"应用信息已发送到Flask后端: {app_info.get('name', 'unknown')}")
            else:
                logger.debug(f"发送应用信息失败，状态码: {response.status_code}")
                
        except Exception as e:
            logger.debug(f"发送应用信息到Flask失败: {str(e)}")

    def stop_app(self, app_name):
        raise NotImplementedError("stop_app not implemented")

    def push_file(self, local_file_path, remote_file_path):
        raise NotImplementedError("push_file not implemented")

    def pull_file(self, remote_file_path, local_file_path):
        raise NotImplementedError("pull_file not implemented")

    def key_press(self, key):
        # Record action for video if recording
        self.record_action_if_recording(
            "key_press",
            key=key
        )
        raise NotImplementedError("key_press not implemented")

    def back(self):
        # Record action for video if recording
        self.record_action_if_recording("back")
        raise NotImplementedError("back not implemented")

    def home(self):
        # Record action for video if recording
        self.record_action_if_recording("home")
        raise NotImplementedError("home not implemented")

    def long_touch(self, x, y, duration=None):
        # Record action for video if recording
        self.record_action_if_recording(
            "long_touch",
            coordinates=(x, y),
            duration=duration
        )
        raise NotImplementedError("long_touch not implemented")

    def drag(self, start_xy, end_xy, duration=None):
        # check if the drag is within the device bound
        start_xy, end_xy = self._check_drag_bound(start_xy, end_xy)
        self._do_drag(start_xy, end_xy, duration)

    def _check_drag_bound(self, start_xy, end_xy):
        # 获取设备边界
        x_min, y_min, x_max, y_max = self.device_bound

        def is_inside(xy):
            x, y = xy
            return x_min <= x <= x_max and y_min <= y <= y_max

        def line_intersection(p1, p2, q1, q2):
            # 计算两条线段的交点
            def det(a, b, c, d):
                return a * d - b * c

            x1, y1 = p1
            x2, y2 = p2
            x3, y3 = q1
            x4, y4 = q2

            denom = det(x1 - x2, y1 - y2, x3 - x4, y3 - y4)
            if denom == 0:
                return None  # 平行或重合

            det1 = det(x1, y1, x2, y2)
            det2 = det(x3, y3, x4, y4)
            x = det(det1, x1 - x2, det2, x3 - x4) / denom
            y = det(det1, y1 - y2, det2, y3 - y4) / denom

            if (min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2) and
                    min(x3, x4) <= x <= max(x3, x4) and min(y3, y4) <= y <= max(y3, y4)):
                return (x, y)
            return None

        # 矩形的四条边
        edges = [
            ((x_min, y_min), (x_max, y_min)),  # 上边
            ((x_max, y_min), (x_max, y_max)),  # 右边
            ((x_max, y_max), (x_min, y_max)),  # 下边
            ((x_min, y_max), (x_min, y_min))   # 左边
        ]

        if is_inside(start_xy) and is_inside(end_xy):
            return start_xy, end_xy

        intersections = []
        for edge in edges:
            intersection = line_intersection(start_xy, end_xy, *edge)
            if intersection:
                intersections.append(intersection)

        if len(intersections) == 2:
            return intersections[0], intersections[1]
        elif len(intersections) == 1:
            if is_inside(start_xy):
                return start_xy, intersections[0]
            elif is_inside(end_xy):
                return intersections[0], end_xy
        else:
            return start_xy, end_xy

    def _do_drag(self, start_xy, end_xy, duration=None):
        # Record action for video if recording
        self.record_action_if_recording(
            "drag",
            start_xy=start_xy,
            end_xy=end_xy,
            duration=duration
        )
        raise NotImplementedError("_do_drag not implemented")

    def get_current_state(self):
        raise NotImplementedError("get_current_state not implemented")

    def view_set_text(self, text):
        raise NotImplementedError("view_set_text not implemented")

    def view_append_text(self, text):
        raise NotImplementedError("view_append_text not implemented")

    def start_screen_record(self):
        raise NotImplementedError("start_screen_record not implemented")

    def stop_screen_record(self):
        raise NotImplementedError("stop_screen_record not implemented")

    def show_highlight(self, x, y, radius):
        raise NotImplementedError("show_highlight not implemented")

    def hide_highlight(self):
        raise NotImplementedError("hide_highlight not implemented")

    def get_clipboard(self) -> str:
        raise NotImplementedError("get_clipboard not implemented")

    def set_clipboard(self, text: str) -> bool:
        raise NotImplementedError("set_clipboard not implemented")

    def expand_notification_panel(self):
        raise NotImplementedError("expand_notification_panel not implemented")

    def _find_device_with_bilingual_match(self, input_device_name: str, device_mappings: dict) -> str:
        """双语匹配设备名称
        
        支持中英文设备名称的双向匹配，包括：
        - 浏览器 <-> browser
        - 手机 <-> phone  
        - 云手机 <-> cloud phone
        
        Args:
            input_device_name: 用户输入的设备名称
            device_mappings: 配置文件中的设备映射
            
        Returns:
            str: 匹配到的设备名称，如果没有匹配返回None
        """
        # 设备类型的中英文映射
        device_type_mappings = {
            # 中文到英文
            '浏览器': 'browser',
            '手机': 'phone',
            '云手机': 'cloud phone',
            '电脑': 'computer',
            # 英文到中文  
            'browser': '浏览器',
            'phone': '手机',
            'cloud phone': '云手机',
            'cloudphone': '云手机',  # 支持连写
            'cloud_phone': '云手机',  # 支持下划线
            'computer': '电脑',
            'pc': '电脑',
            'desktop': '电脑',
        }
        
        import re
        
        # 标准化输入名称（转为小写，统一分隔符）
        normalized_input = input_device_name.lower().replace('_', ' ').replace('-', ' ')
        
        # 尝试直接匹配
        if input_device_name in device_mappings:
            return input_device_name
            
        # 提取设备类型和数字后缀
        def parse_device_name(name):
            """解析设备名称，返回(设备类型, 数字后缀)"""
            name = name.lower().strip()
            # 匹配末尾的数字
            match = re.match(r'^(.+?)(\d*)$', name)
            if match:
                device_type = match.group(1).strip()
                number = match.group(2) if match.group(2) else ''
                return device_type, number
            return name, ''
        
        input_type, input_number = parse_device_name(normalized_input)

        # 当输入没有数字后缀时，默认候选数字为 ['1', '']，优先匹配 1，同时兼容没有数字的配置键
        candidate_numbers = ['1', ''] if input_number == '' else [input_number]
        
        # 遍历候选数字与配置中的所有设备名称进行匹配，优先匹配 '1'
        for candidate_number in candidate_numbers:
            for config_device_name in device_mappings.keys():
                config_type, config_number = parse_device_name(config_device_name)
                
                # 如果数字后缀不匹配，跳过
                if config_number != candidate_number:
                    continue
                    
                # 检查设备类型是否匹配
                # 1. 直接匹配
                if input_type == config_type:
                    return config_device_name
                    
                # 2. 通过映射表匹配
                if input_type in device_type_mappings:
                    mapped_type = device_type_mappings[input_type]
                    if mapped_type == config_type:
                        return config_device_name
                        
                # 3. 反向映射匹配
                if config_type in device_type_mappings:
                    mapped_config_type = device_type_mappings[config_type]
                    if input_type == mapped_config_type:
                        return config_device_name
        
        return None

    def set_device(self, device_name: str) -> bool:
        """公共方法，会在所有设备类型中查找指定的设备名称，
        并调用子类实现的 _do_device_switch 方法来执行实际的设备切换。
        支持中英文设备名称的双向匹配。
        
        Args:
            device_name: 设备名称，支持中英文，如"浏览器1"、"browser1"等
            
        Returns:
            bool: 切换是否成功
            
        Raises:
            ValueError: 当设备名称不存在于device_mappings中时
            DeviceTypeMismatchError: 当设备类型不匹配时（未来扩展）
        """
        try:
            # 1. 检查设备是否存在于映射表中
            if not hasattr(self.config, 'device_mappings') or not self.config.device_mappings:
                raise ValueError("No device mappings configured")
            
            # 2. 尝试双语匹配查找设备
            matched_device_name = self._find_device_with_bilingual_match(device_name, self.config.device_mappings)
            
            if matched_device_name is None:
                # 提供更详细的错误信息，列出所有可用设备
                available_devices = list(self.config.device_mappings.keys())
                raise ValueError(
                    f"Device '{device_name}' not found in device mappings. "
                    f"Available devices: {available_devices}. "
                    f"Supports bilingual matching: 浏览器/browser, 手机/phone, 云手机/cloud phone, 电脑/computer"
                )
            
            # 3. 获取目标设备ID
            target_device_id = self.config.device_mappings[matched_device_name]
            
            # 4. 调用子类实现的设备切换方法
            success = self._do_device_switch(matched_device_name, target_device_id)
            
            if success:
                # 根据设备名称选择更合适的 emoji（browser/浏览器 -> 🌐，phone/手机 -> 📱，computer/电脑/pc/desktop -> 🖥️）
                _name_lower = str(matched_device_name).lower()
                _emoji = "🖥️"
                if ("browser" in _name_lower) or ("浏览器" in matched_device_name):
                    _emoji = "🌐"
                elif ("phone" in _name_lower) or ("手机" in matched_device_name):
                    _emoji = "📱"
                elif ("computer" in _name_lower) or ("电脑" in matched_device_name) or ("pc" in _name_lower) or ("desktop" in _name_lower):
                    _emoji = "🖥️"
                logger.info(f"{_emoji} 设置当前执行设备为：\"{matched_device_name}\"")
                if device_name != matched_device_name:
                    pass
                    # logger.debug(f"Device name '{device_name}' matched to '{matched_device_name}'")

                # 记录轨迹：设置设备
                try:
                    self.agent.sleep(1)
                    #     action_type="set_device",
                    #     action_params={
                    #         "device": matched_device_name
                    #     }
                    # )
                except Exception as _e:
                    logger.debug(f"记录设备切换轨迹失败: {_e}")
            else:
                logger.error(f"❌ 设置执行设备为：\"{matched_device_name}\" 失败")
                # logger.debug(f"Failed to switch to device: {matched_device_name}")
                
            return success
            
        except Exception as e:
            logger.error(f"❌ 设置执行设备为：\"{device_name}\" 失败: {str(e)}")
            return False

    def _do_device_switch(self, device_name: str, device_id: str) -> bool:
        """执行具体的设备切换操作
        
        这是一个抽象方法，需要由子类实现具体的设备切换逻辑。
        每种设备类型都有不同的切换方式：
        - WebSocket设备需要关闭连接、设置端口转发、重新连接
        - Browser设备需要切换BrowserView
        
        Args:
            device_name: 设备名称
            device_id: 设备ID（从device_mappings中获取）
            
        Returns:
            bool: 切换是否成功
        """
        raise NotImplementedError("_do_device_switch not implemented by subclass")

    def take_screenshot_by_description(self, description: str, save_path: Optional[str] = None) -> Image.Image:
        """根据描述定位元素并截取该区域的截图

        Args:
            description: 要定位的元素的自然语言描述，例如 "搜索按钮" 或 "页面顶部的导航栏"
            save_path: 可选的保存路径

        Returns:
            PIL.Image.Image: 裁剪后的图像对象

        Raises:
            RuntimeError: 当无法定位到描述的元素时
            ValueError: 当描述为空时
        """
        if not description:
            raise ValueError("Description cannot be empty")

        try:
            # 首先获取完整截图
            full_screenshot = self.take_screenshot()

            # 使用UI接口定位元素
            raise NotImplementedError("locate_view requires ui module which has been removed")

            # 获取元素的边界框
            bound = located_view._get_bound()

            if bound is None or bound == (None, None, None, None):
                raise RuntimeError(f"Failed to locate element with description: '{description}'")

            x0, y0, x1, y1 = bound

            # 确保坐标有效
            if x0 is None or y0 is None or x1 is None or y1 is None:
                raise RuntimeError(f"Invalid bounding box for element: '{description}'")

            # 确保边界框在图像范围内
            img_width, img_height = full_screenshot.size
            x0 = max(0, min(int(x0), img_width))
            y0 = max(0, min(int(y0), img_height))
            x1 = max(0, min(int(x1), img_width))
            y1 = max(0, min(int(y1), img_height))

            # 确保x1 > x0 和 y1 > y0
            if x1 <= x0 or y1 <= y0:
                # 如果边界框是点坐标，扩展为小区域
                center_x, center_y = (x0 + x1) // 2, (y0 + y1) // 2
                padding = 50  # 50像素的边距
                x0 = max(0, center_x - padding)
                y0 = max(0, center_y - padding)
                x1 = min(img_width, center_x + padding)
                y1 = min(img_height, center_y + padding)

            # 裁剪图像
            cropped_image = full_screenshot.crop((x0, y0, x1, y1))

            # 保存图像（如果指定了路径）
            if save_path:
                cropped_image.save(save_path)

            logger.info(f"📸 成功截取了“{description}”的图片")
            logger.debug(f"成功截取了“{description}”的截图，截取范围为：({x0}, {y0}, {x1}, {y1})")
            # 关闭原始截图以释放内存
            full_screenshot.close()

            return cropped_image

        except Exception as e:
            logger.info(f"⚠️ 截取“{description}”的图片失败: {str(e)}")
            raise RuntimeError(f"截取“{description}”的图片失败: {str(e)}")

    def start_recording(self, output_path=None):
        """开始视频录制
        
        Args:
            output_path (str, optional): 输出视频文件路径。如果未指定，将自动生成。
            
        Returns:
            str: 录制文件路径
            
        Raises:
            RuntimeError: 当已经在录制时
            ImportError: 当视频编码服务不可用时
        """
        if self.recording_active:
            raise RuntimeError("录制已经在进行中")
            
        try:
            # Import video encoder service
            from mobileclaw.services.video_encoder import VideoEncoderService
            self.recording_encoder = VideoEncoderService()
        except ImportError:
            raise ImportError("视频编码服务不可用，请确保已安装必要的依赖")
        
        # Generate output path if not provided
        if output_path is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            task_name = getattr(self.agent, "current_task_name", "task")
            output_path = os.path.join(
                getattr(self.agent, "workspace_path", os.getcwd()),
                "recordings",
                f"{timestamp}_{task_name}_recording_{self.device_name}.mp4"
            )
            
        # Ensure recordings directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Reset recording state
        self.recording_output_path = output_path
        self.recording_stop_requested = False
        self.recording_frames = []
        self.recording_start_time = time.time()
        self.recording_stop_time = None
        
        # Initialize recording metadata
        task_name = getattr(self.agent, 'current_task_name', 'task')
        self.recording_metadata = {
            'task_name': task_name,
            'device_id': self.device_id,
            'device_name': self.device_name,
            'device_type': getattr(self, '__class__.__name__', 'unknown'),
            'start_time': self.recording_start_time,
            'start_time_iso': datetime.fromtimestamp(self.recording_start_time).isoformat(),
            'stop_time': None,
            'stop_time_iso': None,
            'duration_seconds': None,
            'file_size_bytes': None,
            'frame_count': 0,
            'output_path': output_path,
            'gui_actions': [],  # Will be populated during recording
            'action_timeline_summary': {}  # Will be calculated after recording
        }
        
        # Clear frame queue
        while not self.recording_frame_queue.empty():
            try:
                self.recording_frame_queue.get_nowait()
            except queue.Empty:
                break
        
        # Start recording thread
        self.recording_active = True
        self.recording_thread = threading.Thread(target=self._recording_worker, daemon=True)
        self.recording_thread.start()
        
        logger.info(f"🎬 开始视频录制: {output_path}")
        return output_path

    def stop_recording(self):
        """停止视频录制
        
        Returns:
            str: 录制视频文件路径，如果录制失败返回None
        """
        if not self.recording_active:
            logger.warning("没有进行中的录制可以停止")
            return None
            
        logger.info("⏹️ 停止视频录制...")
        
        # Capture stop time
        self.recording_stop_time = time.time()
        
        # Update metadata with stop information
        if self.recording_metadata:
            self.recording_metadata.update({
                'stop_time': self.recording_stop_time,
                'stop_time_iso': datetime.fromtimestamp(self.recording_stop_time).isoformat(),
                'frame_count': len(self.recording_frames),
                'gui_actions': self.recording_action_timeline,
            })
            if self.recording_start_time:
                duration = self.recording_stop_time - self.recording_start_time
                self.recording_metadata['duration_seconds'] = duration
            
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
        
        # Signal stop
        self.recording_stop_requested = True
        self.recording_active = False
        
        # Wait for recording thread to finish
        if self.recording_thread and self.recording_thread.is_alive():
            self.recording_thread.join(timeout=5.0)
        
        # Process recorded frames into video
        video_path = None
        if self.recording_frames:
            try:
                video_path = self.recording_encoder.encode_frames_to_video(
                    self.recording_frames,
                    self.recording_output_path,
                    self.recording_metadata  # Pass metadata to encoder
                )
                
                # Update metadata with file information
                if video_path and os.path.exists(video_path):
                    file_size = os.path.getsize(video_path)
                    self.recording_metadata['file_size_bytes'] = file_size
                    
                    # Save metadata to a JSON file alongside the video
                    metadata_path = video_path.replace('.mp4', '_metadata.json')
                    import json
                    with open(metadata_path, 'w', encoding='utf-8') as f:
                        json.dump(self.recording_metadata, f, indent=2, ensure_ascii=False)
                    
                    duration = self.recording_stop_time - self.recording_start_time if self.recording_start_time else 0
                    logger.info(f"✅ 视频录制完成: {video_path} (时长: {duration:.1f}s, 帧数: {len(self.recording_frames)}, 大小: {file_size:,} bytes)")
            except Exception as e:
                logger.error(f"❌ 视频编码失败: {str(e)}")
        else:
            logger.warning("没有录制到任何帧")
        
        # Cleanup state
        self.recording_frames = []
        self.recording_stop_requested = False
        self.recording_start_time = None
        self.recording_stop_time = None
        self.recording_thread = None
        
        return video_path

    def is_recording(self):
        """检查是否正在录制
        
        Returns:
            bool: 如果正在录制返回True，否则返回False
        """
        return self.recording_active

    def _recording_worker(self):
        """录制工作线程，持续捕获屏幕帧"""
        import structlog
        logger = structlog.get_logger(__name__)
        
        frame_count = 0
        last_frame_time = time.time()
        
        # Determine appropriate frame interval based on device capabilities
        frame_interval = self._get_optimal_frame_interval()
        
        try:
            while not self.recording_stop_requested:
                try:
                    current_time = time.time()
                    
                    # Limit frame rate to prevent performance issues
                    if current_time - last_frame_time < frame_interval:
                        time.sleep(0.001)  # Small sleep to prevent CPU spinning
                        continue
                    
                    # Capture frame
                    frame = self.take_screenshot()
                    if frame is not None:
                        # Add timestamp to frame
                        frame_data = {
                            "image": frame,
                            "timestamp": current_time,
                            "frame_number": frame_count
                        }
                        
                        # Use queue for thread-safe frame buffering
                        try:
                            self.recording_frame_queue.put_nowait(frame_data)
                        except queue.Full:
                            # If queue is full, remove oldest frame and add new one
                            try:
                                self.recording_frame_queue.get_nowait()
                                self.recording_frame_queue.put_nowait(frame_data)
                            except queue.Empty:
                                pass
                        
                        # Store frames for encoding (limit to prevent memory issues)
                        if len(self.recording_frames) < 3600:  # Max 1 hour at 1fps
                            self.recording_frames.append(frame_data)
                        
                        frame_count += 1
                        last_frame_time = current_time
                    
                except Exception as e:
                    logger.debug(f"录制帧捕获失败: {str(e)}")
                    time.sleep(0.1)  # Brief pause on error
                    
        except Exception as e:
            logger.error(f"录制工作线程异常: {str(e)}")
            
        logger.debug(f"录制线程结束，共捕获 {frame_count} 帧")

    def record_gui_action(self, action_type, **params):
        """Record a GUI action during video recording.

        Args:
            action_type: Type of action (click, scroll, input, etc.)
            **params: Additional action parameters (coordinates, duration, etc.)
        """
        if not self.recording_active:
            return

        import base64
        from io import BytesIO

        action_id = self.recording_action_counter + 1
        current_time = time.time()
        frame_number = len(self.recording_frames)

        # Process screenshot if provided
        screenshot_base64 = None
        if 'screenshot' in params and params['screenshot']:
            try:
                buffered = BytesIO()
                params['screenshot'].save(buffered, format='PNG')
                screenshot_base64 = base64.b64encode(buffered.getvalue()).decode()
            except Exception as e:
                logger.debug(f"Failed to process action screenshot: {e}")

        action_record = {
            "action_id": action_id,
            "action_type": action_type,
            "timestamp": current_time,
            "timestamp_iso": datetime.fromtimestamp(current_time).isoformat(),
            "frame_number": frame_number,
            **params
        }

        # Add screenshot if successfully processed
        if screenshot_base64:
            action_record["screenshot_base64"] = screenshot_base64

        # Store in timeline
        self.recording_action_timeline.append(action_record)

        # Map to frame number for correlation
        if frame_number not in self.recording_frame_actions:
            self.recording_frame_actions[frame_number] = []
        self.recording_frame_actions[frame_number].append(action_record)

        # Update action counter
        self.recording_action_counter = action_id

        logger.debug(f"🎯 Recorded GUI action #{action_id}: {action_type} at frame {frame_number}")

    def record_action_if_recording(self, action_type, **params):
        """Central hook for recording actions during video recording.

        This method provides a single point for all action recording logic
        and should be called throughout the codebase when actions occur.

        Args:
            action_type: Type of action (click, scroll, input, etc.)
            **params: Additional action parameters (coordinates, duration, etc.)
        """
        if self.recording_active:
            self.record_gui_action(action_type, **params)

    def _get_optimal_frame_interval(self):
        """获取适合当前设备的帧间隔时间
        
        Returns:
            float: 帧间隔时间（秒）
        """
        # Default to 10 FPS (0.1 second interval)
        # This can be overridden by device-specific implementations
        return 0.1
