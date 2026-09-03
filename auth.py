"""代理认证:Proxy-Authorization Basic → 用户/角色。"""
import base64


def authenticate(headers, users):
    """校验请求头中的 Basic 凭据,成功返回用户名,失败返回 None。

    users: dict[str, UserCfg];headers 为 http_message.Headers(大小写不敏感查找)。
    """
    val = headers.get("proxy-authorization")
    if not val:
        return None
    scheme, _, cred = val.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(cred.strip(), validate=True).decode("utf-8")
    except Exception:
        return None
    name, _, pw = decoded.partition(":")
    u = users.get(name)
    if u is not None and u.password == pw:
        return name
    return None
