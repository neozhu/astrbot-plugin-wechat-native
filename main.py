"""AstrBot plugin entry point. Import registers the robot type in the dashboard."""

from astrbot.api.star import Context, Star

from .adapter import WeChatNativeAdapter  # noqa: F401


class WeChatNativePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
