"""平台能力层：不同平台的不同「获取方式」收敛在这里。见 docs/架构_多平台与批量下载.md。"""

from app.platforms.base import AuthorItem, AuthorPage, NotSupported
from app.platforms.registry import list_author, resolve, supports_author

__all__ = ["AuthorItem", "AuthorPage", "NotSupported", "list_author", "resolve", "supports_author"]
