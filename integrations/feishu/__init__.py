"""飞书会议桥接（路线 A）：托管浏览器 + BlackHole 音频。

浏览器由独立的桥接守护进程持有（bridge_host.py），8765 服务通过
client.call_bridge 经 Unix socket 调用，服务重启不影响会中浏览器。
"""

from integrations.feishu.bridge import (FeishuBridge, get_bridge,
                                        validate_binding)
from integrations.feishu.client import call_bridge, daemon_alive

__all__ = ["FeishuBridge", "get_bridge", "validate_binding",
           "call_bridge", "daemon_alive"]
