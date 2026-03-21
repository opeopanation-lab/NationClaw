import time
import os
import copy
import queue
import threading
import random
import traceback
from datetime import datetime

import structlog
from typing import cast, Callable, Iterable, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from mobileclaw.config import AgentConfig

logger = structlog.get_logger(__name__)


class AutoAgent:
    def __init__(self, config: AgentConfig):
        self.config = config
        from . import device, fm, chat, file
        self.name = config.name
        self.org_name = config.org_name
        self.permission = getattr(self.config, 'permission', 'normal')

        self.device_manager = device.DeviceManager(self)
        self.fm = fm.FM_Interface(self)
        self.chat = chat.Chat_Interface(self)
        self.file = file.File_Interface(self)

        self.print_model_configuration()
        self._enabled = True
        self._idle_task_count = 0

        # Track current task execution stack (for nested tasks)
        self._current_task_stack = []  # List of (task, actions_and_results) tuples
        self.actions_and_results_max_len = 100  # TODO change this to a config

        # Message handling queue and synchronization
        self._message_queue = queue.Queue()
        self._message_handling_lock = threading.Lock()
        self._handling_message = False
        self._message_pause_event = threading.Event()
        self._message_pause_event.set()  # Initially not paused
        self._wake_event = threading.Event()  # For interrupting adaptive sleep

        self.start()

    def start(self):
        self.fm._open()
        self.chat._open()
        self.file._open()

    def stop(self):
        self._enabled = False
        self.fm._close()
        self.chat._close()
        self.file._close()

    def _adaptive_sleep(self):
        """Adaptive sleep based on idle task count.
        Sleep time increases exponentially with consecutive idle tasks,
        up to a maximum of 10 minutes (600 seconds).
        """
        sleep_time = min(2 ** self._idle_task_count, 600)
        self._sleep(sleep_time)

    def serve(self):
        """
        Start agent serving.
        """
        while self._enabled:
            self.execute_task('Complete pending tasks or do your routine work.')
            self._adaptive_sleep()

    def _log_and_report(self, content, actions_and_results, task_tag="📋"):
        """
        Helper method to append content to actions_and_results, log to log.md, and send to self.

        Args:
            content: Content to log and report
            actions_and_results: The actions_and_results list to append to
            task_tag: Emoji tag to prefix the content with
        """
        # Prefix content with task tag
        prefixed_content = f"{task_tag} {content}"

        # Append to actions_and_results
        actions_and_results.append(prefixed_content)

        # Append to log.md using file module
        try:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_content = f"\n## {timestamp}\n{prefixed_content}\n"

            # Check if log.md exists, create with header if not
            full_log_path = self.file.get_log_path_today()

            if not os.path.exists(full_log_path):
                with open(full_log_path, 'w') as f:
                    # Create log.md with header
                    f.write("# Agent Log\n\n")

            # Append the content
            with open(full_log_path, 'a') as f:
                f.write(log_content)
        except Exception as e:
            logger.error(f"Failed to write to log.md: {e}")
            import traceback
            traceback.print_exc()

        # Send to self
        try:
            logger.info(prefixed_content)
            self.chat.send_to_log(prefixed_content)
        except Exception as e:
            logger.error(f"Error in send_to_log: {e}")

    def _conclude_task(self, task, actions_and_results, _recursion_depth=0):
        """
        Conclude a task by saving useful information to knowledge and memory files.
        This function is called at the end of execute_task and do_with_device.

        Args:
            task: The task description that was executed
            actions_and_results: List of actions taken and their results during execution

        Returns:
            None
        """
        try:
            # Create a summary of the task execution for the conclude_task mode
            task_summary = f"Review and save useful information from the following completed task:\n{task}"
            task_actions_results = copy.copy(actions_and_results)

            # Execute in conclude_task mode with limited steps
            logger.info(f"Concluding task: {task[:50]}...")
            self.execute_task(task=task_summary, actions_and_results=task_actions_results, mode='conclude_task', max_steps=10, _recursion_depth=_recursion_depth+1)

        except Exception as e:
            logger.warning(f"Error concluding task: {e}")
            # Don't fail the main task if conclusion fails

    def get_agent_info(self):
        """
        Gather agent context information including profile, memory, and basic info.

        Returns:
            str: Formatted string with agent profile and memory content
        """
        import os
        from datetime import datetime

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Read task guidelines from working directory
        profile_path = self.file.agent_profile_path
        profile_content = ""
        if os.path.exists(profile_path):
            with open(profile_path, 'r', encoding='utf-8') as f:
                profile_content = f.read()

        # Read today's memory
        memory_path_today = self.file.get_memory_path_today()
        memory_today_content = ""
        if os.path.exists(memory_path_today):
            with open(memory_path_today, 'r', encoding='utf-8') as f:
                memory_today_content = f.read()

        # Read long-term memory
        longterm_memory_path = os.path.join(self.file.agent_dir, 'long_term_memory.md')
        longterm_memory_content = ""
        if os.path.exists(longterm_memory_path):
            with open(longterm_memory_path, 'r', encoding='utf-8') as f:
                longterm_memory_content = f.read()

        # Get relative paths for display
        profile_path_rel = os.path.relpath(profile_path, self.file.agent_dir)
        memory_path_today_rel = os.path.relpath(memory_path_today, self.file.agent_dir)
        longterm_memory_path_rel = os.path.relpath(longterm_memory_path, self.file.agent_dir)

        agent_info = f"""
- Current Time: {current_time}
- Agent Name: {self.name}
- Agent Permission: {self.permission}
- Agent Profile ({profile_path_rel}):
```
{profile_content}
```
- Today's Memory ({memory_path_today_rel}):
```
{memory_today_content}
```
- Long-term Memory ({longterm_memory_path_rel}):
```
{longterm_memory_content}
```"""

        return agent_info

    def get_current_task_info(self):
        """
        Get information about the current ongoing task.

        Returns:
            tuple: (task, actions_and_results) or (None, []) if no task is running
        """
        if not self._current_task_stack:
            return (None, [])
        return self._current_task_stack[-1]  # Return the top of the stack

    def execute_task(self, task, actions_and_results=[], max_steps=30, _recursion_depth=0, mode='normal'):
        """
        Let the agent execute a task
        The execution process is a loop. At each step, let the model decide what actions to take next.

        Args:
            task: Task description
            max_steps: Maximum number of steps to execute
            _recursion_depth: Internal parameter to track recursion depth (max 3 levels)
            mode: Execution mode. Supported modes:
                  - 'normal': Full functionality with all APIs available
                  - 'handle_message': Handle incoming messages with memory updates
                  - 'conclude_task': Save task information to knowledge and memory files
        """
        # Prevent infinite recursion
        if _recursion_depth >= 3:
            logger.warning(f"Maximum recursion depth reached for task: {task}")
            return [f"Error: Maximum recursion depth (3) reached. Cannot execute subtask: {task}"]

        # Generate a random emoji as task tag
        task_emojis = ['🔴', '🟠', '🟡', '🟢', '🔵', '🟣', '🟤', '⚫', '⚪',
                       '🟥', '🟧', '🟨', '🟩', '🟦', '🟪', '🟫', '⬛', '⬜',
                       '❤️', '🧡', '💛', '💚', '💙', '💜', '🤎', '🖤', '🤍']
        task_tag = random.choice(task_emojis)

        # Get agent context information
        agent_info = self.get_agent_info()

        # Get available devices and models
        available_devices = self.device_manager.get_available_devices()
        available_models = self.fm.get_available_models()

        # Get available skills
        available_skills = self.file.list_skills()

        # Initialize execution state
        actions_and_results = actions_and_results
        indent = "  " * _recursion_depth  # Indent for subtasks
        self._log_and_report(f'{indent}Start task: {task}', actions_and_results, task_tag)

        # Push task to stack for tracking
        self._current_task_stack.append((task, actions_and_results))

        try:
            agent_api = self._create_agent_api_for_execution(actions_and_results, _recursion_depth, mode, task_tag, indent)

            # Track if task is idle (finished in step 0 with only end_task called)
            finished_step = -1

            for step in range(max_steps):
                # Pause normal tasks if a message is being handled
                if mode == 'normal':
                    self._message_pause_event.wait()

                if len(actions_and_results) > self.actions_and_results_max_len:
                    actions_and_results = actions_and_results[-self.actions_and_results_max_len:]

                # Extract and limit medias from actions_and_results for the prompt
                media_content_parts = self._build_media_content_parts(actions_and_results)

                # Prepare params for task_step
                params = {
                    'task': task,
                    'mode': mode,
                    'agent_info': agent_info,
                    'actions_and_results': actions_and_results,
                    'available_devices': available_devices,
                    'available_models': available_models,
                    'available_skills': available_skills,
                    'recursion_depth': _recursion_depth,
                    'media_content_parts': media_content_parts,
                }

                # Call model to generate step code
                thought, code = self.fm.call_func('task_step', params)

                # Add thought to results
                if thought:
                    self._log_and_report(f'{indent}Step {step} Thought: {thought}', actions_and_results, task_tag)

                # Stop if no code generated
                if not code:
                    self._log_and_report(f'{indent}Step {step} Action: (WARNING - no code parsed)', actions_and_results, task_tag)
                    continue

                code_line = f'{indent}Step {step} Action:\n```\n{code}\n```'
                self._log_and_report(code_line, actions_and_results, task_tag)

                # Execute the code
                try:
                    # Reset call count and set current step for output logging
                    agent_api._call_count = 0
                    agent_api._current_step = step

                    exec_globals = {'agent': agent_api}
                    exec(code, exec_globals)

                    # Check task status from agent API
                    if agent_api._task_status != 'ongoing':
                        if agent_api._task_status == 'finished':
                            finished_step = step
                        break

                except Exception as e:
                    err_msg = f"{indent}Step {step} Output: Error: {e}"
                    logger.error(err_msg)
                    traceback.print_exc()
                    self._log_and_report(err_msg, actions_and_results, task_tag)
                    continue

            if step + 1 >= max_steps and agent_api._task_status != 'finished':
                self._log_and_report(f'[WARNING] Task stopped due to step limit: {max_steps}. You may need to start a new task to complete the remaining work.', actions_and_results, task_tag=task_tag)

            # Determine whether it is an idle task (finished within 1 step, no action executed)
            idle_flag = False
            if finished_step == 0:
                idle_flag = True
            # Update idle task count based on whether this task was idle
            if _recursion_depth == 0:
                if idle_flag:
                    self._idle_task_count += 1
                else:
                    self._idle_task_count = 0  # Reset counter on non-idle task

            # Conclude task by saving useful information (only for normal mode)
            if mode == 'normal' and not idle_flag:
                self._conclude_task(task, actions_and_results, _recursion_depth=_recursion_depth)

            return actions_and_results
        finally:
            # Pop task from stack
            if self._current_task_stack and self._current_task_stack[-1][0] == task:
                self._current_task_stack.pop()

    def handle_message(self, message, history, sender, channel):
        """
        Handle a new chat message with handle_message mode.
        This function is called when a new message arrives.
        Messages are processed sequentially - if a message is already being handled,
        this call will wait until the previous message is finished.

        Args:
            message: The incoming message. Can be a string, image/file path, or a list of them
            history: Recent previous messages in the conversation, represented as text
            sender: Who (name/id) sent this message
            channel: Through which the message was received
        """
        # Acquire lock to ensure only one message is handled at a time
        # If another message is being handled, this will wait
        with self._message_handling_lock:
            try:
                # Pause normal tasks while handling message
                self._message_pause_event.clear()
                self._wake_event.set()  # Wake up from adaptive sleep immediately
                self._handling_message = True

                message_content = str(message)
                history_content = str(history)

                # Get current task info
                current_task, current_task_actions = self.get_current_task_info()
                current_task_actions = copy.copy(current_task_actions)

                # Build task context section
                task_context = ""
                if current_task:
                    task_context = f"""
- Current Ongoing Task Context
The agent is currently working on the following task:
Task: {current_task}

"""

                # Format history content
                history_formatted = f"```\n{history_content}\n```" if history_content else '(No previous messages)'

                # Create task description that includes the message and history
                task_description = f"""Handle the following message:
- Message: `{message_content}`
- Sender: `{sender}`
- Channel: `{channel}`
- Recent conversation:
{history_formatted}
{task_context}"""

                # Execute task directly in handle_message mode (for memory maintenance and response generation)
                logger.info(f"Handling message from {sender} via {channel}")
                results = self.execute_task(task_description, mode='handle_message', actions_and_results=current_task_actions)
                return results

            except Exception as e:
                logger.error(f"Error handling message: {e}")
                import traceback
                traceback.print_exc()
                return [f"Error: {str(e)}"]

            finally:
                # Resume normal tasks after handling message
                self._handling_message = False
                self._message_pause_event.set()
        
    def send_message(self, message, receiver, channel=None):
        """
        Send a message to receiver through channel.
        This API is used for messaging through the agent.chat module.
        For sending messages through other apps (wechat, etc.), use do_with_device function.

        Args:
            message: Can be a string, an image/file (represented as a path) or a list of them
            receiver: Name/id of the message receiver (can be a user or a group)
            channel: Channel through which to send the message (e.g., 'zulip')

        Returns:
            str: Confirmation message
        """
        logger.info(f"Sending message to {receiver} via {channel}")
        return self.chat.send_message(message, receiver, channel)

    def sleep(self, seconds: float):
        self._sleep(seconds)

    def _sleep(self, seconds: float):
        """Let the agent sleep for several seconds, but can be interrupted."""
        self._wake_event.clear()
        self._wake_event.wait(timeout=seconds)

    def _initialize_working_dir(self):
        """Initialize the working directory structure based on the template."""
        if hasattr(self, 'file') and self.file:
            self.file._initialize_working_dir()

    def _get_working_dir_tree(self, show_non_markdown=False) -> str:
        """Get a text description of the working directory tree.
        By default, only shows markdown files.

        Args:
            show_non_markdown: If True, also show non-markdown files
        Returns:
            str: Text description of the directory tree
        """
        if hasattr(self, 'file') and self.file:
            return self.file.get_working_dir_tree(show_non_markdown)
        return "(File interface not initialized)"

    def _build_media_content_parts(self, actions_and_results, max_images=5):
        """Extract image tuples from actions_and_results, keep only the last max_images,
        and convert them to API content parts.

        Args:
            actions_and_results: List of text strings and image tuples (path, base64)
            max_images: Maximum number of images to include in the prompt (FIFO)

        Returns:
            list: Content parts suitable for appending to API messages
        """
        # Collect all image tuples
        all_medias = []
        for item in actions_and_results:
            if isinstance(item, tuple) and len(item) == 2:
                all_medias.append(item)

        # Keep only the last N images (FIFO)
        limited_medias = all_medias[-max_images:] if len(all_medias) > max_images else all_medias

        # Convert to content parts
        content_parts = []
        for media_path, media_base64 in limited_medias:
            try:
                if media_base64:
                    if media_path:
                        content_parts.append({
                            "type": "text",
                            "text": f"[Image: {media_path}]"
                        })
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{media_base64}"
                        }
                    })
            except Exception as e:
                logger.warning(f"Failed to process image: {e}")
        return content_parts

    def _create_vars_preview(self, vars_dict: dict, str_preview_len: int = 500, collection_preview_len: int = 100, preview_threshold: int = 10000) -> dict:
        """Create preview strings for variables to be displayed in prompts.

        Args:
            vars_dict: Dictionary of variable names to values
            str_preview_len: Maximum number of characters to show for string previews
            collection_preview_len: Maximum number of elements to show for collections
            preview_threshold: If len(str(value)) < preview_threshold, show the whole content
        Returns:
            dict: Dictionary mapping variable names to preview strings
        """
        def preview_value(value, depth=0, max_depth=2, indent_level=0, show_all=False):
            """Recursively create preview for a value."""
            # Prevent infinite recursion
            if depth > max_depth:
                return f"{type(value).__name__} object"

            # Determine effective limits based on show_all flag
            effective_str_len = preview_threshold if show_all else str_preview_len
            effective_collection_len = preview_threshold if show_all else collection_preview_len

            if isinstance(value, str):
                # Check if string has multiple lines
                lines = value.split('\n')
                if len(lines) > 1:
                    # Format multi-line strings like lists
                    num_lines_to_show = min(len(lines), int(effective_collection_len))
                    has_more = len(lines) > effective_collection_len

                    # Use multi-line format if more than 2 lines
                    if num_lines_to_show > 2 or has_more:
                        indent = "  " * (indent_level + 1)
                        preview_lines = ["\"\"\""]
                        for i, line in enumerate(lines[:int(effective_collection_len)]):
                            # Truncate each line if too long
                            if len(line) > effective_str_len:
                                remaining_chars = len(line) - int(effective_str_len)
                                line = line[:int(effective_str_len)] + f"... ({remaining_chars} more chars)"
                            preview_lines.append(f"{indent}{line}")
                        if has_more:
                            preview_lines.append(f"{indent}... ({len(lines) - int(effective_collection_len)} more lines)")
                        preview_lines.append("  " * indent_level + "\"\"\"")
                        return "\n".join(preview_lines)
                    else:
                        # Single-line format for 1-2 lines
                        preview_items = []
                        for line in lines[:int(effective_collection_len)]:
                            if len(line) > effective_str_len:
                                remaining_chars = len(line) - int(effective_str_len)
                                line = line[:int(effective_str_len)] + f"... ({remaining_chars} more chars)"
                            preview_items.append(line)
                        return f"'{chr(92)}n'.join([{', '.join([repr(l) for l in preview_items])}])"
                else:
                    # Single-line string
                    if len(value) > effective_str_len:
                        remaining_chars = len(value) - int(effective_str_len)
                        return f"'{value[:int(effective_str_len)]}... ({remaining_chars} more chars)'"
                    return f"'{value}'"

            elif isinstance(value, (int, float, bool)):
                return str(value)

            elif isinstance(value, list):
                if len(value) == 0:
                    return "[]"

                # Determine number of items to show
                num_items_to_show = min(len(value), int(effective_collection_len))
                has_more = len(value) > effective_collection_len

                # Use multi-line format if more than 2 items
                if num_items_to_show > 2 or has_more:
                    indent = "  " * (indent_level + 1)
                    preview_lines = ["["]
                    for i, item in enumerate(value[:int(effective_collection_len)]):
                        item_preview = preview_value(item, depth + 1, max_depth, indent_level + 1, show_all)
                        preview_lines.append(f"{indent}{item_preview},")
                    if has_more:
                        preview_lines.append(f"{indent}... ({len(value) - int(effective_collection_len)} more)")
                    preview_lines.append("  " * indent_level + "]")
                    return "\n".join(preview_lines)
                else:
                    # Single-line format for 1-2 items
                    preview_items = []
                    for item in value[:int(effective_collection_len)]:
                        preview_items.append(preview_value(item, depth + 1, max_depth, indent_level, show_all))
                    return f"[{', '.join(preview_items)}]"

            elif isinstance(value, tuple):
                if len(value) == 0:
                    return "()"

                # Determine number of items to show
                num_items_to_show = min(len(value), int(effective_collection_len))
                has_more = len(value) > effective_collection_len

                # Use multi-line format if more than 2 items
                if num_items_to_show > 2 or has_more:
                    indent = "  " * (indent_level + 1)
                    preview_lines = ["("]
                    for i, item in enumerate(value[:int(effective_collection_len)]):
                        item_preview = preview_value(item, depth + 1, max_depth, indent_level + 1, show_all)
                        preview_lines.append(f"{indent}{item_preview},")
                    if has_more:
                        preview_lines.append(f"{indent}... ({len(value) - int(effective_collection_len)} more)")
                    preview_lines.append("  " * indent_level + ")")
                    return "\n".join(preview_lines)
                else:
                    # Single-line format for 1-2 items
                    preview_items = []
                    for item in value[:int(effective_collection_len)]:
                        preview_items.append(preview_value(item, depth + 1, max_depth, indent_level, show_all))
                    return f"({', '.join(preview_items)})"

            elif isinstance(value, dict):
                if len(value) == 0:
                    return "{}"

                # Determine number of items to show
                num_items_to_show = min(len(value), int(effective_collection_len))
                has_more = len(value) > effective_collection_len

                # Use multi-line format if more than 2 items
                if num_items_to_show > 2 or has_more:
                    indent = "  " * (indent_level + 1)
                    preview_lines = ["{"]
                    for i, (k, v) in enumerate(list(value.items())[:int(effective_collection_len)]):
                        key_str = preview_value(k, depth + 1, max_depth, indent_level + 1, show_all)
                        val_str = preview_value(v, depth + 1, max_depth, indent_level + 1, show_all)
                        preview_lines.append(f"{indent}{key_str}: {val_str},")
                    if has_more:
                        preview_lines.append(f"{indent}... ({len(value) - int(effective_collection_len)} more)")
                    preview_lines.append("  " * indent_level + "}")
                    return "\n".join(preview_lines)
                else:
                    # Single-line format for 1-2 items
                    preview_items = []
                    for k, v in list(value.items())[:int(effective_collection_len)]:
                        key_str = preview_value(k, depth + 1, max_depth, indent_level, show_all)
                        val_str = preview_value(v, depth + 1, max_depth, indent_level, show_all)
                        preview_items.append(f"{key_str}: {val_str}")
                    return f"{{{', '.join(preview_items)}}}"

            else:
                return f"{type(value).__name__} object"

        vars_preview = {}
        for var_name, var_value in vars_dict.items():
            if var_name == 'actions_and_results':
                continue  # Skip actions_and_results as it's shown separately
            # Check if value is small enough to show completely
            show_all = len(str(var_value)) < preview_threshold
            vars_preview[var_name] = preview_value(var_value, show_all=show_all)

        return vars_preview

    def execute_on_device(self, func: Callable[[Any], Any], device: Any) -> Any:
        """Execute a given function on a single device.

        Args:
            func: A callable function that accepts a `device` parameter
            device: Device instance
        Returns:
            Any: Return value of `func(device)`
        """
        if not callable(func):
            raise TypeError("func must be callable")
        device_name = getattr(device, 'device_name', str(device))
        logger.debug(f"Executing function on device: {device_name}")
        return func(device)

    def execute_on_devices(self, func: Callable[[Any], Any], devices: Iterable[Any], parallel: bool = False) -> dict[str, Any]:
        """Execute a given function on multiple devices, either serially or in parallel.

        Args:
            func: A callable function that accepts a `device` parameter
            devices: Iterable of device instances
            parallel: If True, execute in parallel; otherwise execute serially
        Returns:
            dict[str, Any]: { device_name: return_value }
        """
        if not callable(func):
            raise TypeError("func must be callable")

        device_list = list(devices)
        results: dict[str, Any] = {}

        if not device_list:
            return results

        if not parallel:
            logger.debug("Executing function serially on multiple devices")
            for device in device_list:
                device_name = device.device_name
                results[device_name] = func(device)
            return results

        logger.debug("Executing function in parallel on multiple devices")
        with ThreadPoolExecutor(max_workers=len(device_list)) as executor:
            future_to_name = {}
            for device in device_list:
                device_name = device.device_name
                future = executor.submit(func, device)
                future_to_name[future] = device_name

            for future in as_completed(future_to_name):
                device_name = future_to_name[future]
                results[device_name] = future.result()

        return results

    def print_model_configuration(self):
        """Print model configuration information used in the execution script."""
        if getattr(self.config, 'use_wisewk_service', False):
            logger.info("✅ Using Wisewk service")
        if getattr(self.config, 'use_custom_fm', False):
            logger.info("✅ Using custom FM model")
        if getattr(self.config, 'use_custom_gui_vlm', False):
            logger.info("✅ Using custom GUI-VLM model")

    def get_current_task_line(self):
        """Get the current line number being executed in the task."""
        return None

    def get_task_execution_summary(self):
        """Get the execution summary of the current task."""
        return None

    # ==================== Domain-Specific APIs for Task Execution ====================
    # These APIs are used in generated Python code for task execution

    def do_with_device(self, task, device=None):
        """
        Execute a task on a device.

        Args:
            task: Natural language description of what to do
            device: The name/id of an available device

        Returns:
            tuple: (actions_and_results, screenshots)
                - actions_and_results: list of text strings and image tuples
                - screenshots: list of (screen_path, screen_base64) tuples
        """
        device_obj = self.device_manager.get_device(device)
        if not device_obj:
            logger.error(f"Device not found: {device}")
            return [f"Error: Device '{device}' not found"], []

        try:
            actions_and_results, screenshots = device_obj.execute_task(task)
            return actions_and_results, screenshots
        except Exception as e:
            logger.error(f"Error executing instruction on device: {e}")
            return [f"Error: {str(e)}"], []

    def query_model(self, params, model_name=None):
        """
        Query the foundation model.

        Args:
            params: List of query parameters (text, image, file_path, etc.)
            model_name: Specifies the preferred model to use in this query

        Returns:
            list: Model response as a list of text and images
        """
        # Convert params to appropriate format for fm interface
        if isinstance(params, str):
            params = [params]

        # Load file paths in params
        IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff'}
        TEXT_EXTENSIONS = {'.md', '.txt', '.csv', '.json', '.xml', '.html', '.yaml', '.yml', '.log'}
        resolved_params = []
        for item in params:
            if isinstance(item, str) and not item.startswith(('http://', 'https://')):
                path = item if os.path.isabs(item) else os.path.join(self.file.agent_dir, item)
                if os.path.isfile(path):
                    ext = os.path.splitext(path)[1].lower()
                    if ext in IMAGE_EXTENSIONS:
                        import base64
                        from io import BytesIO
                        from PIL import Image
                        img = Image.open(path)
                        buf = BytesIO()
                        img.save(buf, format='PNG')
                        base64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                        resolved_params.append((path, base64_str))
                        continue
                    elif ext in TEXT_EXTENSIONS:
                        with open(path, 'r', encoding='utf-8') as f:
                            resolved_params.append(f.read())
                        continue
            resolved_params.append(item)

        try:
            # Use the query_model function from function_hub_local
            response = self.fm.call_func('query_model', {
                'query': resolved_params,
                'model_name': model_name or 'default'
            })

            if response is None:
                return ["Error: No response from model"]

            return [response] if isinstance(response, str) else response
        except Exception as e:
            logger.error(f"Error querying model: {e}")
            return [f"Error: {str(e)}"]

    def _create_agent_api_for_execution(self, actions_and_results, recursion_depth=0, mode='normal', task_tag='📋', indent=''):
        """
        Create an agent API object for task execution.
        Similar to MemoryAPI in memory system.

        Args:
            actions_and_results: The shared actions_and_results list for capturing results
            recursion_depth: Current recursion depth for nested tasks
            mode: Execution mode ('normal', 'handle_message', 'conclude_task')
            task_tag: Emoji tag for logging
            indent: Indentation prefix for subtasks
        """
        class FileAPI:
            """File operations API for task execution."""
            def __init__(self, file_interface, agent_api):
                self._file = file_interface
                self._agent_api = agent_api

            def _check_and_count(self):
                self._agent_api._check_call_count()

            def read(self, file_path, line_start, line_end):
                self._check_and_count()
                result = self._file.read(file_path, line_start, line_end)
                self._agent_api._capture_result('file.read', result)
                return result

            def search(self, file_or_dir_path, text, line_limit=100):
                self._check_and_count()
                result = self._file.search(file_or_dir_path, text, line_limit)
                self._agent_api._capture_result('file.search', result)
                return result

            def write(self, file_path, content):
                self._check_and_count()
                result = self._file.write(file_path, content)
                self._agent_api._capture_result('file.write', result)
                return result

            def append(self, file_path, content):
                self._check_and_count()
                result = self._file.append(file_path, content)
                self._agent_api._capture_result('file.append', result)
                return result

            def insert(self, file_path, insert_line, content):
                self._check_and_count()
                result = self._file.insert(file_path, insert_line, content)
                self._agent_api._capture_result('file.insert', result)
                return result

            def replace(self, file_path, match_text, replace_text):
                self._check_and_count()
                result = self._file.replace(file_path, match_text, replace_text)
                self._agent_api._capture_result('file.replace', result)
                return result

            def delete(self, file_path):
                self._check_and_count()
                result = self._file.delete(file_path)
                self._agent_api._capture_result('file.delete', result)
                return result

            def remove_lines(self, file_path, line_start, line_end):
                self._check_and_count()
                result = self._file.remove_lines(file_path, line_start, line_end)
                self._agent_api._capture_result('file.remove_lines', result)
                return result

            def parse_file(self, file_path):
                self._check_and_count()
                result = self._file.parse_file(file_path)
                self._agent_api._capture_result('file.parse_file', result)
                return result

            def generate_file(self, file_path, requirement, materials):
                self._check_and_count()
                result = self._file.generate_file(file_path, requirement, materials)
                self._agent_api._capture_result('file.generate_file', result)
                return result

        class AgentAPI:
            RESULT_MAX_INLINE_LEN = 3000
            RESULT_BRIEF_LEN = 1000

            def __init__(self, agent, actions_and_results, recursion_depth, mode, task_tag, indent):
                self._agent = agent
                self._actions_and_results = actions_and_results
                self._recursion_depth = recursion_depth
                self._mode = mode
                self._task_tag = task_tag
                self._indent = indent
                self._task_status = 'ongoing'
                self._call_count = 0
                self._current_step = 0
                self.file = FileAPI(agent.file, self)

            def _check_call_count(self):
                self._call_count += 1
                if self._call_count > 1:
                    raise Exception("Only one agent command per step is allowed. Please split into multiple steps.")

            def _log_output(self, content):
                """Log output using the unified _log_and_report with Step N Output prefix."""
                if isinstance(content, tuple) and len(content) == 2:
                    # Image tuple — append the image into the list
                    self._actions_and_results.append(content)
                else:
                    text = f"{self._indent}Step {self._current_step} Output: {content}"
                    self._agent._log_and_report(text, self._actions_and_results, self._task_tag)

            def _capture_result(self, api_name, result):
                """Capture API return value into actions_and_results via _log_output."""
                if result is None:
                    self._log_output(f"{api_name} finished")
                    return

                # Handle list results that may contain image tuples
                if isinstance(result, list):
                    text_parts = []
                    for item in result:
                        if isinstance(item, tuple) and len(item) == 2:
                            self._log_output(item)
                        elif isinstance(item, str):
                            text_parts.append(item)
                    if text_parts:
                        combined = '\n'.join(text_parts)
                        self._capture_text_result(api_name, combined)
                    return

                self._capture_text_result(api_name, result)

            def _capture_text_result(self, api_name, result):
                """Capture a text result, saving to temp file if too large."""
                result_str = str(result)
                if len(result_str) <= self.RESULT_MAX_INLINE_LEN:
                    self._log_output(f"{api_name} returned:\n{result_str}")
                else:
                    # Save to temp file
                    try:
                        temp_dir = self._agent.file.agent_temp_dir
                        os.makedirs(temp_dir, exist_ok=True)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = f"result_{api_name}_{timestamp}.md"
                        filepath = os.path.join(temp_dir, filename)
                        with open(filepath, 'w', encoding='utf-8') as f:
                            f.write(result_str)
                        brief = result_str[:self.RESULT_BRIEF_LEN]
                        rel_path = os.path.relpath(filepath, self._agent.file.agent_dir)
                        self._log_output(
                            f"{api_name} returned (full content saved to {rel_path}):\n{brief}..."
                        )
                    except Exception as e:
                        logger.warning(f"Failed to save result to temp file: {e}")
                        self._log_output(f"{api_name} returned (truncated):\n{result_str[:self.RESULT_BRIEF_LEN]}...")

            def _save_screenshot(self, screenshot, label='screenshot'):
                """Save a screenshot to _temp/screenshots/ and append image tuple to actions_and_results."""
                import base64
                from io import BytesIO
                buf = BytesIO()
                screenshot.save(buf, format='PNG')
                base64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                # Save to file
                try:
                    screenshots_dir = os.path.join(self._agent.file.agent_temp_dir, 'screenshots')
                    os.makedirs(screenshots_dir, exist_ok=True)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{label}_{timestamp}.png"
                    filepath = os.path.join(screenshots_dir, filename)
                    screenshot.save(filepath, format='PNG')
                    rel_path = os.path.relpath(filepath, self._agent.file.agent_dir)
                except Exception as e:
                    logger.warning(f"Failed to save screenshot to file: {e}")
                    rel_path = f"{label}_{datetime.now().strftime('%H%M%S')}"
                return rel_path, base64_str

            def do_with_device(self, task, device=None):
                self._check_call_count()
                if self._mode in ['handle_message', 'conclude_task']:
                    raise Exception(f"do_with_device is not allowed in {self._mode} mode.")
                device_steps, device_screenshots = self._agent.do_with_device(task, device=device)
                # Store trajectory for infer_from_last_trajectory
                self._last_device_steps = device_steps
                # Get the post-action screen from device_screenshots
                if device_screenshots:
                    last_path, last_base64 = device_screenshots[-1]
                    self._log_output((last_path, last_base64))
                # Build a string summary from text entries in device_actions
                result_text = '\n'.join(
                    item for item in device_steps if isinstance(item, str)
                )
                result_text += f'\nYou can get more detailed information from this trace (intermediate screenshots included) by calling `agent.infer_from_last_trajectory`'
                self._capture_result('do_with_device', result_text)
                return result_text

            def infer_from_last_trajectory(self, question):
                """Infer information from the last device action trajectory.

                Args:
                    question: The question to answer based on the trajectory.
                """
                self._check_call_count()
                if not hasattr(self, '_last_device_steps') or not self._last_device_steps:
                    raise Exception("No device trajectory available. Call do_with_device first.")
                # Build query params: text + images from the trajectory
                params = [f"Based on the following device action trajectory, answer this question: {question}\n\n# Trajectory\n"]
                for item in self._last_device_steps:
                    if isinstance(item, str):
                        params.append(item + '\n')
                    elif isinstance(item, tuple) and len(item) == 2:
                        params.append(item)
                result = self._agent.query_model(params)
                self._capture_result('infer_from_last_trajectory', result)
                return result

            def query_model(self, params, model_name=None):
                self._check_call_count()
                result = self._agent.query_model(params, model_name)
                self._capture_result('query_model', result)
                return result

            def execute_task(self, task, max_steps=20):
                self._check_call_count()
                if self._mode in ['handle_message', 'conclude_task']:
                    raise Exception(f"execute_task is not allowed in {self._mode} mode.")
                result = self._agent.execute_task(task, max_steps=max_steps, _recursion_depth=self._recursion_depth + 1)
                self._capture_result('execute_task', result)
                return result

            def take_note(self, text):
                self._check_call_count()
                self._log_output(f"Note: {text}")

            def send_message(self, message, receiver=None, channel=None):
                self._check_call_count()
                result = self._agent.send_message(message, receiver, channel)
                self._capture_result('send_message', result)
                return result

            def end_task(self, status):
                """End the current task with a status.

                Args:
                    status: 'finished', 'failed', or 'infeasible'
                """
                self._check_call_count()
                if status not in ('finished', 'failed', 'infeasible'):
                    raise ValueError(f"Invalid task status: {status}. Must be 'finished', 'failed', or 'infeasible'.")
                self._task_status = status

            def get_device_screen(self, device=None):
                """Get the current screen of a device as a screenshot.
                The screenshot will be saved to _temp/screenshots/ and included in the next step's context.

                Args:
                    device: Name/id of the device. If None, uses the first available device.
                """
                self._check_call_count()
                device_obj = self._agent.device_manager.get_device(device)
                if not device_obj:
                    raise Exception(f"Device not found: {device}")
                screenshot = device_obj.take_screenshot()
                if screenshot is None:
                    raise Exception(f"Failed to take screenshot from device: {device}")
                rel_path, base64_str = self._save_screenshot(screenshot, label=device or 'device_screen')
                self._log_output(f'Current screen: {rel_path}')
                self._log_output((rel_path, base64_str))

            def read_image(self, image_path):
                """Read an image file and include it in the next step's context.

                Args:
                    image_path: Relative path (from agent dir) or absolute path to the image file.
                """
                self._check_call_count()
                import base64
                from io import BytesIO
                from PIL import Image
                if not os.path.isabs(image_path):
                    image_path = os.path.join(self._agent.file.agent_dir, image_path)
                img = Image.open(image_path)
                buf = BytesIO()
                img.save(buf, format='PNG')
                base64_str = base64.b64encode(buf.getvalue()).decode('utf-8')
                rel_path = os.path.relpath(image_path, self._agent.file.agent_dir)
                result_text = f'Image loaded: {rel_path}'
                self._log_output(result_text)
                self._log_output((rel_path, base64_str))

        return AgentAPI(self, actions_and_results, recursion_depth, mode, task_tag, indent)

