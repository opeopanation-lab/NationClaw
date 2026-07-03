"""
The interfaces to call system-level APIs of the target device.
Can reuse many from the droidbot library.
"""

from nationclaw.device.phone import WebsocketController
from nationclaw.device.browser import BrowserDeviceController
from nationclaw.device.computer import get_computer_device
from nationclaw.device.device_manager import DeviceManager
