"""IMAP 邮箱连接器。

【为什么优先用这条路径】
学校租户通常不给学生授予 Graph 应用级 Mail.Read 权限（见 graph_mail.py 的鉴权说明），
而 IMAP 只要"邮箱地址 + 授权码/密码"就能跑通，是个人账号最稳的一条路。
切换方式：settings.yaml 里把 mail.provider 改成 "imap"，代码无需改动。

【边界说明】
external_id 优先取邮件的 Message-ID。
边界说明：邮件 Message-ID 理论上可能重复（极少见）。
现实场景中学邮箱几乎不会遇到；若遇到，UNIQUE 约束会保留首次入库版本，
后续同 ID 邮件被视作同一封，属于可接受的降级行为。没有 Message-ID 时退化为 imap:seq:<序号>。

【实现说明】
imaplib 是同步库，这里用 asyncio.to_thread 把它挡在事件循环之外；
每次 fetch 前用本连接器独立的 TokenBucket 做礼貌间隔。
data 字段名（subject / from / receivedDateTime / bodyPreview）必须与
core/hashing.py 的 mail 白名单保持一致，否则哈希会永远变化、每轮都误报变更。
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

from ..core.models import RawItem
from .base import BaseConnector

logger = logging.getLogger(__name__)

#: 只拉最近 7 天的邮件
DEFAULT_LOOKBACK_DAYS = 7
SOURCE = "mail"

_IMAP_DATE_FORMAT = "%d-%b-%Y"  # IMAP SINCE 的格式：02-Jan-2026（月份是英文缩写）
_SSL_TIMEOUT_SECONDS = 30.0
_BODY_PREVIEW_CHARS = 500
_MAX_DATE_CHARS = 200  # 畸形日期保留原文时的截断上限
_TAG_PATTERN = re.compile(r"<[^>]+>")


class ImapMailConnector(BaseConnector):
    """基于 IMAP4_SSL 的邮箱连接器。"""

    name = "mail"

    def __init__(self, cfg: dict[str, Any]) -> None:
        """按 settings.yaml 的 mail.imap 段构造。

        传整段配置（而不是一长串位置参数）的理由：以后加字段只改 settings.yaml，
        连接器侧用 .get(..., 默认值) 兜底，不必再去改调用方与构造函数签名。
        """
        super().__init__(rate_limit_rps=float(cfg.get("rate_limit_rps", 2)))
        self.host = str(cfg.get("host", ""))
        self.port = int(cfg.get("port", 993))
        self.username = str(cfg.get("username", ""))
        self.password = str(cfg.get("password", ""))
        self.folder = str(cfg.get("folder", "INBOX"))
        self.lookback_days = int(cfg.get("lookback_days", DEFAULT_LOOKBACK_DAYS))

    async def fetch(self) -> list[RawItem]:
        """拉取近 N 天的邮件（阻塞部分丢到线程池执行）。"""
        await self.bucket.acquire()
        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> list[RawItem]:
        since = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).strftime(
            _IMAP_DATE_FORMAT
        )
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(
            self.host, self.port, ssl_context=context, timeout=_SSL_TIMEOUT_SECONDS
        )
        try:
            conn.login(self.username, self.password)
            status, _ = conn.select(self.folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"IMAP 打开目录失败：{self.folder}")
            status, data = conn.search(None, f'(SINCE "{since}")')
            if status != "OK":
                raise RuntimeError(f'IMAP 搜索失败：SINCE "{since}"')

            items: list[RawItem] = []
            for sequence in data[0].split():
                try:
                    item = self._fetch_one(conn, sequence.decode())
                except Exception as exc:  # noqa: BLE001 —— 故意广catch：单封邮件不能拖垮整批
                    logger.error("解析邮件失败（序号 %s）：%s", sequence, exc)
                    continue
                if item is not None:
                    items.append(item)
            logger.info("IMAP 拉取完成：%d 封（近 %d 天）", len(items), self.lookback_days)
            return items
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 —— 礼貌性收尾，失败无需打扰调用方
                logger.debug("IMAP logout 异常，已忽略")

    def _fetch_one(self, conn: imaplib.IMAP4_SSL, sequence: str) -> RawItem | None:
        status, data = conn.fetch(sequence, "(RFC822)")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            logger.warning("邮件 %s 无正文内容，跳过", sequence)
            return None
        message = email.message_from_bytes(data[0][1])
        message_id = _clean_message_id(message.get("Message-ID"))
        external_id = f"imap:{message_id}" if message_id else f"imap:seq:{sequence}"
        return RawItem(
            source=SOURCE,
            external_id=external_id,
            course_id=None,
            payload={
                "subject": _decode_header_value(message.get("Subject")),
                "from": _decode_header_value(message.get("From")),
                "receivedDateTime": _normalize_date(message.get("Date")),
                "bodyPreview": _extract_body(message)[:_BODY_PREVIEW_CHARS],
            },
        )


def _clean_message_id(raw: str | None) -> str:
    """去掉 Message-ID 外层的尖括号与空白。"""
    if not raw:
        return ""
    return raw.strip().strip("<>").strip()


def _decode_header_value(raw: str | None) -> str:
    """解码 MIME 头，处理 =?utf-8?B?...?= 之类编码。"""
    if not raw:
        return ""
    parts: list[str] = []
    for fragment, charset in decode_header(raw):
        if isinstance(fragment, bytes):
            parts.append(_decode_bytes(fragment, charset))
        else:
            parts.append(fragment)
    return "".join(parts).strip()


def _decode_bytes(raw: bytes, charset: str | None) -> str:
    """按声明的字符集解码字节。

    真实邮件里会遇到非法字符集名（例如 =?unknown-8bit?B?...?=），
    此时 decode 会抛 LookupError；退化为 utf-8 继续解码，
    绝不让一封怪邮件把整批拉取搞崩。
    """
    if charset:
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            logger.debug("未知字符集 %s，退化为 utf-8", charset)
    return raw.decode("utf-8", errors="replace")


def _normalize_date(raw: str | None) -> str | None:
    """把 RFC 2822 的 Date 头转成 ISO 8601（三态）。

    - 能解析        → ISO 8601 字符串
    - 有值但解析失败 → 原始字符串（strip 后，超长截断到 200 字符）
    - 缺失 / 空串    → None

    为什么畸形日期要保留原文而不是一律 None：LLM 看到 "Mon, 32 Sep 2025"
    能判断这大概是老师手误、也许指 10 月 2 日；看到 None 则完全没有信息可用。
    判定交给 LLM 比写死在代码里更灵活。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        logger.debug("Date 头解析失败，保留原文：%r", text)
        return text[:_MAX_DATE_CHARS]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def _extract_body(message: Message) -> str:
    """取正文：multipart 时优先 text/plain，退而求其次用 text/html 去标签。"""
    if message.is_multipart():
        plain = ""
        html = ""
        for part in message.walk():
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and not plain:
                plain = _decode_payload(part)
            elif content_type == "text/html" and not html:
                html = _decode_payload(part)
        if plain:
            return plain
        return _strip_html(html) if html else ""

    payload = _decode_payload(message)
    return _strip_html(payload) if message.get_content_type() == "text/html" else payload


def _decode_payload(part: Message) -> str:
    """按部件声明的字符集解码。"""
    raw = part.get_payload(decode=True)
    if raw is None:
        payload = part.get_payload()
        return payload if isinstance(payload, str) else ""
    return _decode_bytes(raw, part.get_content_charset())


def _strip_html(text: str) -> str:
    """粗暴去标签并压缩空白，仅用于生成预览文本。"""
    return " ".join(_TAG_PATTERN.sub(" ", text).split())

