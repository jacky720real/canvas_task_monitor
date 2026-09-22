"""Microsoft Graph 邮箱连接器。

【鉴权模式说明 — 重要坑点】
本实现使用 client_credentials（应用权限 / Application Permission）。
- 该模式要求 Azure AD 管理员在「应用注册」里授予应用级 Mail.Read 权限，
  并且通常需要管理员 consent。
- **绝大多数学校的 Azure 租户不允许学生自助授予应用权限。**
  如果申请不下来，本连接器会直接 401/403 失败。

【备选路径（按推荐顺序）】
1. 若你有租户管理员权限 → 用 client_credentials（本文件实现）。
2. 若你只有个人账号 → 改用 Authorization Code flow（委托权限 / Delegated），
   首次登录一次授权，之后用 refresh_token。
   本规格暂不实现，代码留 TODO 注释标注。
3. **最稳的路径 → 直接切 IMAP**（见 imap_mail.py）。
   在 settings.yaml 里把 mail.provider 改成 "imap" 即可，无需改代码。

【另两点实现说明】
- external_id 用 graph:{internetMessageId}。若中途把 provider 从 graph 换成 imap，
  同一封邮件会因前缀不同（imap:）被当成新条目，属于已知取舍。
- 取令牌也是一次出站请求，同样要过本连接器独立的 TokenBucket。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..core.models import RawItem
from .base import BaseConnector

logger = logging.getLogger(__name__)

#: 只拉最近 7 天
DEFAULT_LOOKBACK_DAYS = 7
SOURCE = "mail"

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_SCOPE = "https://graph.microsoft.com/.default"
_SELECT_FIELDS = "subject,from,receivedDateTime,bodyPreview,internetMessageId"
_TOKEN_EXPIRY_SKEW = 60.0  # 提前 60 秒视为过期，避免用到临界令牌
_PAGE_TOP = 50


class GraphMailConnector(BaseConnector):
    """基于 Microsoft Graph 的邮箱连接器（client_credentials 模式）。"""

    name = "mail"

    def __init__(self, cfg: dict[str, Any]) -> None:
        """按 settings.yaml 的 mail.graph 段构造。

        传整段配置（而不是一长串位置参数）的理由：以后加字段只改 settings.yaml，
        连接器侧用 .get(..., 默认值) 兜底，不必再去改调用方与构造函数签名。
        """
        super().__init__(rate_limit_rps=float(cfg.get("rate_limit_rps", 2)))
        self.tenant_id = str(cfg.get("tenant_id", ""))
        self.client_id = str(cfg.get("client_id", ""))
        self.client_secret = str(cfg.get("client_secret", ""))
        self.user = str(cfg.get("user", ""))
        self.mailbox_folder = str(cfg.get("mailbox_folder", "Inbox"))
        self.filter_from_domains = list(cfg.get("filter_from_domains") or [])
        self.lookback_days = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
        retry = cfg.get("retry") or {}
        self.max_attempts = max(int(retry.get("max_attempts", 3)), 1)
        self.backoff_base = float(retry.get("backoff_base", 1.5))
        self._token_url = _TOKEN_URL_TEMPLATE.format(tenant_id=self.tenant_id)
        self._token_lock = asyncio.Lock()
        self._token_value = ""
        self._token_expires_at = 0.0
        self._client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        """惰性创建并复用 AsyncClient（令牌端点与 Graph 端点同客户端、用绝对 URL）。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    # TODO(auth): 委托权限（Authorization Code flow）暂未实现。
    # 需要 OAuth 回调地址与 refresh_token 的本地安全存储，按需再补。

    async def _access_token(self) -> str:
        """取应用令牌并缓存到过期前 60 秒。"""
        async with self._token_lock:
            now = time.monotonic()
            if self._token_value and now < self._token_expires_at:
                return self._token_value

            await self.bucket.acquire()
            client = await self._http()
            response = await client.post(
                self._token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": _SCOPE,
                    "grant_type": "client_credentials",
                },
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Graph 取令牌失败（HTTP {response.status_code}）：{response.text[:200]}"
                    "；若是 401/403，说明该租户未授予应用级 Mail.Read 权限，"
                    '请改用 IMAP（settings.yaml 里 mail.provider 改为 "imap"）。'
                )
            payload = response.json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise RuntimeError("Graph 取令牌响应中缺少 access_token")
            expires_in = float(payload.get("expires_in") or 3600)
            self._token_value = token
            self._token_expires_at = now + max(expires_in - _TOKEN_EXPIRY_SKEW, 0.0)
            return token

    async def _request(
        self, url: str, params: dict[str, Any] | None, token: str
    ) -> httpx.Response:
        """发一次 GET；429 / 5xx / 网络异常按指数退避重试，其它 4xx 直接抛出。"""
        client = await self._http()
        for attempt in range(1, self.max_attempts + 1):
            await self.bucket.acquire()
            retry_wait: float | None = None
            try:
                response = await client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                retry_wait = float(self.backoff_base**attempt)
                logger.warning(
                    "Graph 请求异常（第 %d/%d 次）：%s", attempt, self.max_attempts, exc
                )
            else:
                if response.status_code == 429 or response.status_code >= 500:
                    # 429 优先遵从其 Retry-After；注意 Retry-After: 0 是合法值，不能用 or 短路
                    retry_after = _retry_after_seconds(response)
                    retry_wait = (
                        retry_after
                        if retry_after is not None
                        else float(self.backoff_base**attempt)
                    )
                    logger.warning(
                        "Graph 返回 %d（第 %d/%d 次），%.1fs 后重试",
                        response.status_code,
                        attempt,
                        self.max_attempts,
                        retry_wait,
                    )
                else:
                    response.raise_for_status()
                    return response
            if retry_wait is not None and attempt < self.max_attempts:
                await asyncio.sleep(retry_wait)
        raise RuntimeError(f"Graph 请求重试 {self.max_attempts} 次后仍失败：{url}")

    async def fetch(self) -> list[RawItem]:
        """拉取近 N 天、发件人命中白名单的邮件（按 @odata.nextLink 翻页）。"""
        token = await self._access_token()
        since = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        url: str | None = (
            f"{_GRAPH_BASE}/users/{quote(self.user, safe='')}"
            f"/mailFolders/{quote(self.mailbox_folder, safe='')}/messages"
        )
        params: dict[str, Any] | None = {
            "$filter": f"receivedDateTime ge {since}",
            "$select": _SELECT_FIELDS,
            "$top": str(_PAGE_TOP),
        }

        items: list[RawItem] = []
        while url:
            response = await self._request(url, params, token)
            payload = response.json()
            for message in payload.get("value") or []:
                item = self._to_item(message)
                if item is not None:
                    items.append(item)
            # @odata.nextLink 自带全部查询参数，翻页时不能再叠加 params
            url = payload.get("@odata.nextLink")
            params = None
        logger.info(
            "Graph 拉取完成：%d 封（近 %d 天，发件人白名单 %d 条）",
            len(items),
            self.lookback_days,
            len(self.filter_from_domains),
        )
        return items

    def _to_item(self, message: dict[str, Any]) -> RawItem | None:
        """把一条 Graph 邮件转成 RawItem；发件人不命中白名单则返回 None。"""
        sender = (message.get("from") or {}).get("emailAddress") or {}
        address = str(sender.get("address") or "")
        if not self._matches_sender(address):
            return None

        message_id = str(message.get("internetMessageId") or "").strip().strip("<>")
        fallback_id = str(message.get("id") or "")
        if message_id:
            external_id = f"graph:{message_id}"
        elif fallback_id:
            external_id = f"graph:{fallback_id}"
        else:
            logger.warning("Graph 邮件缺少 id 与 internetMessageId，跳过")
            return None

        return RawItem(
            source=SOURCE,
            external_id=external_id,
            course_id=None,
            data={
                "subject": message.get("subject") or "",
                "from": address,
                # 时间字段缺失时填 None（不是空串、更不 skip）：宁可让下游看到 null，
                # 也不要因为一个字段异常就漏掉整封通知。
                "receivedDateTime": message.get("receivedDateTime") or None,
                "bodyPreview": message.get("bodyPreview") or "",
            },
        )

    def _matches_sender(self, address: str) -> bool:
        """发件人是否命中 filter_from_domains（同时匹配根域与子域）。"""
        if not self.filter_from_domains:
            return True
        lowered = address.lower()
        return any(
            lowered.endswith((f"@{domain}", f".{domain}"))
            for domain in (item.lower() for item in self.filter_from_domains)
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """读取 Retry-After（秒）；缺失或非法时返回 None。"""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None

