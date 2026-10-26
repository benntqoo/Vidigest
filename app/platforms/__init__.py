"""平台能力层：不同平台的不同「获取方式」收敛在这里。见 docs/架构_多平台与批量下载.md。"""

from app.platforms.base import AuthorItem, AuthorPage, NotSupported
from app.platforms.registry import list_author, resolve, supports_author


def login_douyin(profile, timeout: float = 180.0, on_state=None) -> bool:
    """抖音登录（有头窗口 + 扫码）。见 `douyin_browser.login_interactive`。"""
    from app.platforms.douyin_browser import login_interactive

    return login_interactive(profile, timeout, on_state)

__all__ = [
    "AuthorItem",
    "AuthorPage",
    "NotSupported",
    "list_author",
    "login_douyin",
    "resolve",
    "supports_author",
]
