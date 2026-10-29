"""AstrBot plugin entry point. Import registers the robot type in the dashboard."""

from astrbot.api.star import Context, Star

from .adapter import WeChatNativeAdapter  # noqa: F401
from .config import PLUGIN_ID
from .connection_view import ConnectionView


class WeChatNativePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        view = ConnectionView(context)
        context.register_web_api(
            f"/{PLUGIN_ID}/connection/bots", view.bots, ["GET"], "列出微信机器人"
        )
        context.register_web_api(
            f"/{PLUGIN_ID}/connection/check", view.check, ["GET"], "只读测试 Native 连接"
        )
