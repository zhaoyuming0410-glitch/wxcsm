"""wx4 引擎的领域错误。"""


class Wx4Error(Exception):
    """wx4 取数引擎可预期错误,直接面向用户展示。"""


class WechatNotRunningError(Wx4Error):
    """需要微信运行(登录态)才能抓取密钥。"""


class KeyNotFoundError(Wx4Error):
    """在内存里没找到可用的数据库密钥。"""


class NotDecryptableError(Wx4Error):
    """给定密钥无法解开该数据库。"""
