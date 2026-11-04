"""WhatsApp through a self-hosted WA-AKG gateway.

WA-AKG (a Next.js + Baileys WhatsApp gateway) exposes a REST API keyed by
``X-API-Key``. Harvey uses four calls: send a text, list chats, read one
chat's history, and check which numbers are on WhatsApp.

The rest of Harvey is keyed on email addresses: the outbox, the approval
ladder, dedup, opt-outs, conversations. Rather than fork all of that, a
WhatsApp contact gets a reserved address that can never be mailed:

    5511912345678@whatsapp.invalid        a phone number (…@s.whatsapp.net)
    lid.123456789@whatsapp.invalid        a WhatsApp "LID" (…@lid)

``.invalid`` is reserved by RFC 2606, so nothing outside Harvey can ever
deliver to it. ``RoutedProvider`` sends those addresses through WhatsApp
and everything else through the configured mailbox, so email and WhatsApp
run side by side through the same outbox.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from harvey.integrations.mail_provider import InboundMessage, MailProvider, SendResult

logger = logging.getLogger("harvey.whatsapp")

WA_DOMAIN = "whatsapp.invalid"
# Prospect.source for a contact Harvey found (a company's phone) and for
# someone who wrote to the WhatsApp number first.
WA_SOURCE = "whatsapp"
WA_INBOUND_SOURCE = "whatsapp_inbound"

_PHONE_JID = "@s.whatsapp.net"
_LID_JID = "@lid"
_LID_PREFIX = "lid."
_DIGITS = re.compile(r"\D+")

# What a chat message's text is called in WA-AKG's stored rows.
_TEXT_TYPES = {"TEXT", "EXTENDED_TEXT", "CONVERSATION", ""}


# SendResult.error prefix for "nothing was sent, try later" (gateway down,
# session logged out): the sender keeps the message approved.
WA_UNAVAILABLE = "whatsapp unavailable:"


class WhatsAppError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# Added to the handler's prompt when the conversation is on WhatsApp.
WHATSAPP_REPLY_RULES = """

This conversation is on WhatsApp, not email. Write ONLY the message text:
- Reply in the language the prospect wrote in.
- Chat style: short, natural, one to four short sentences. No subject line,
  no "Hi <name>," email opener, no sign-off or signature block.
- No markdown headings or bullet lists; at most one emoji, and only if they
  used emoji first.
- One question or one next step per message."""

# Country calling codes whose main language isn't English, for the writer.
_LANGUAGE_BY_CC = {"55": "Brazilian Portuguese", "351": "European Portuguese",
                   "34": "Spanish", "52": "Spanish", "54": "Spanish", "57": "Spanish",
                   "56": "Spanish", "51": "Spanish", "598": "Spanish", "595": "Spanish",
                   "244": "Portuguese", "258": "Portuguese"}


def language_for_address(address: str, default_country_code: str = "") -> str:
    digits = jid_for_address(address).split("@")[0]
    if not digits or not digits.isdigit():
        digits = default_country_code or ""
    for n in (3, 2):
        if digits[:n] in _LANGUAGE_BY_CC:
            return _LANGUAGE_BY_CC[digits[:n]]
    return ""


def whatsapp_writing_rules(address: str, config, sequence: bool) -> str:
    """What the writer's prompt adds for messages that go out on WhatsApp."""
    wa = config.channels.whatsapp
    language = language_for_address(address, wa.default_country_code)
    which = ("each of the 3 messages; message 2 and 3 are short nudges (under 35 words)"
             if sequence else "this message")
    lines = [
        "",
        "",
        "CHANNEL: WHATSAPP. These go to the business's WhatsApp number, not an inbox.",
        f"- Write {which} as a WhatsApp chat message: under 60 words, short",
        "  sentences, no email greeting line like 'Dear', no sign-off or signature block.",
        "- Say who you are and why you're writing in the first sentence: a message",
        "  from an unknown number that doesn't is reported as spam.",
        "- One clear, easy question to answer at the end. No links in the first message.",
        "- No markdown, no bullet lists, at most one emoji.",
        "- Still return a short \"subject\" (it isn't sent; it labels the message).",
        "- Harvey adds an opt-out line under the first messages; don't write one.",
    ]
    if language:
        lines.append(f"- Write in {language}, natural and informal-professional.")
    return "\n".join(lines)


# ── Addresses ────────────────────────────────────────────────────────────


def normalize_phone(raw: str, default_country_code: str = "") -> str:
    """Digits with the country code ("5511912345678"), or "" when it can't
    be a phone number. A number without "+" or "00" gets the default
    country code unless it already starts with it and is long enough."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    international = raw.startswith("+") or raw.startswith("00")
    digits = _DIGITS.sub("", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    cc = _DIGITS.sub("", default_country_code or "")
    if not international:
        digits = digits.lstrip("0")   # trunk prefix ("011 ...", "(011) ...")
        if cc and not (digits.startswith(cc) and len(digits) >= len(cc) + 10):
            digits = cc + digits
    # E.164: at most 15 digits; anything under 10 is a short code or junk.
    if not 10 <= len(digits) <= 15:
        return ""
    return digits


def is_wa_address(address: str) -> bool:
    return (address or "").strip().lower().endswith("@" + WA_DOMAIN)


def wa_address(phone_or_jid: str, default_country_code: str = "") -> str:
    """The reserved address for a phone number or a WhatsApp JID, or ""."""
    value = (phone_or_jid or "").strip().lower()
    if value.endswith(_LID_JID):
        local = _DIGITS.sub("", value[: -len(_LID_JID)].split(":")[0])
        return f"{_LID_PREFIX}{local}@{WA_DOMAIN}" if local else ""
    if value.endswith(_PHONE_JID):
        local = _DIGITS.sub("", value[: -len(_PHONE_JID)].split(":")[0])
        return f"{local}@{WA_DOMAIN}" if local else ""
    if "@" in value:
        return ""                     # a group, broadcast, newsletter: never
    digits = normalize_phone(value, default_country_code)
    return f"{digits}@{WA_DOMAIN}" if digits else ""


def jid_for_address(address: str) -> str:
    """The WhatsApp JID behind a reserved address ("" if it isn't one)."""
    if not is_wa_address(address):
        return ""
    local = address.strip().lower().rsplit("@", 1)[0]
    if local.startswith(_LID_PREFIX):
        return local[len(_LID_PREFIX):] + _LID_JID
    return local + _PHONE_JID


def phone_for_address(address: str) -> str:
    """"+5511912345678" for a phone address; "" for a LID or anything else."""
    jid = jid_for_address(address)
    return "+" + jid[: -len(_PHONE_JID)] if jid.endswith(_PHONE_JID) else ""


# ── The WA-AKG client ────────────────────────────────────────────────────


class WaAkgClient:
    """Thin async client for the WA-AKG REST API."""

    def __init__(self, base_url: str, api_key: str, session: str,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 20.0):
        base = (base_url or "").strip().rstrip("/")
        if base and not base.endswith("/api"):
            base += "/api"
        self.base = base
        self.api_key = (api_key or "").strip()
        self.session = (session or "").strip()
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base, timeout=self._timeout, transport=self._transport,
            headers={"X-API-Key": self.api_key, "Accept": "application/json"},
        )

    async def _request(self, method: str, path: str, body: dict | None = None):
        async with self._client() as c:
            resp = await c.request(method, path, json=body)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("message") or data.get("error") or "")
            raise WhatsAppError(f"WA-AKG {method} {path.split('?')[0]}: "
                                f"HTTP {resp.status_code} {detail}".strip(),
                                status=resp.status_code)
        if isinstance(data, dict) and data.get("status") is False:
            raise WhatsAppError(f"WA-AKG {method} {path}: {data.get('message') or 'failed'}")
        return data

    @staticmethod
    def _data(payload):
        """WA-AKG wraps most answers as {"status", "message", "data"}."""
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _sid(self) -> str:
        return quote(self.session, safe="")

    async def send_text(self, jid: str, text: str) -> dict:
        payload = await self._request(
            "POST", f"/messages/{self._sid()}/{quote(jid, safe='')}/send",
            {"message": {"text": text}},
        )
        data = self._data(payload)
        return data if isinstance(data, dict) else {}

    async def list_chats(self) -> list[dict]:
        data = self._data(await self._request("GET", f"/chat/{self._sid()}"))
        return [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []

    async def chat_messages(self, jid: str) -> list[dict]:
        """Up to the last 100 messages, oldest first."""
        data = self._data(await self._request(
            "GET", f"/chat/{self._sid()}/{quote(jid, safe='')}"))
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    async def check_numbers(self, numbers: list[str]) -> dict[str, str]:
        """{number: jid} for the numbers that are on WhatsApp (50 per call)."""
        found: dict[str, str] = {}
        for i in range(0, len(numbers), 50):
            batch = numbers[i:i + 50]
            data = self._data(await self._request(
                "POST", f"/chat/{self._sid()}/check", {"numbers": batch}))
            results = data.get("results") if isinstance(data, dict) else data
            for r in results or []:
                if isinstance(r, dict) and r.get("exists") and r.get("number"):
                    found[_DIGITS.sub("", str(r["number"]))] = str(r.get("jid") or "")
        return found

    async def session_info(self) -> dict:
        data = self._data(await self._request("GET", "/sessions"))
        for s in data if isinstance(data, list) else []:
            if isinstance(s, dict) and s.get("sessionId") == self.session:
                return s
        raise WhatsAppError(f"WA-AKG has no session named '{self.session}'")


# ── The provider ─────────────────────────────────────────────────────────


def _parse_time(value) -> datetime | None:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            n = float(value)
            return datetime.fromtimestamp(n / 1000 if n > 1e12 else n, timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str) and value:
        try:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class WhatsAppProvider(MailProvider):
    """Sends and reads WhatsApp messages through WA-AKG.

    Reading: every private chat whose last message is newer than the read
    marker is opened, and the messages the contact sent after Harvey's (or
    your) last message there become one inbound message. Several short
    WhatsApp messages in a row are one reply, so they get one answer. The
    newest message's id is the dedup key, so a later message in the same
    chat is read as a new reply that includes the earlier ones.

    The marker starts at the moment WhatsApp is first switched on, so chats
    from before that are never answered.
    """

    name = "whatsapp"

    def __init__(self, config, env, transport: httpx.AsyncBaseTransport | None = None,
                 state_path: str | Path | None = None):
        self.config = config
        self.wa = config.channels.whatsapp
        self.client = WaAkgClient(
            getattr(env, "wa_akg_url", ""), getattr(env, "wa_akg_api_key", ""),
            self.wa.session, transport=transport,
        )
        if state_path is None:
            from harvey.paths import PROJECT_ROOT
            state_path = PROJECT_ROOT / "data" / "whatsapp_state.json"
        self._state_path = Path(state_path)
        self._pending_marker: str = ""

    # marker persistence

    def _load_marker(self) -> datetime:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            t = _parse_time(raw.get("read_after", ""))
            if t:
                return t
        except (OSError, ValueError, AttributeError):
            pass
        now = datetime.now(timezone.utc)
        self._save_marker(now.isoformat())
        return now

    def _save_marker(self, iso: str):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"read_after": iso}), encoding="utf-8")
        except OSError as e:
            logger.warning(f"WhatsApp: couldn't save the read marker: {e}")

    # MailProvider

    def is_configured(self) -> bool:
        return bool(self.wa.enabled and self.client.base and self.client.api_key
                    and self.client.session)

    async def send_email(self, to_email: str, subject: str, body: str,
                         thread_ref: str = "", in_reply_to: str = "") -> SendResult:
        """WhatsApp has no subject: only the body goes."""
        jid = jid_for_address(to_email)
        if not jid:
            return SendResult(ok=False, error=f"not a WhatsApp address: {to_email}")
        text = (body or "").strip()
        if not text:
            return SendResult(ok=False, error="empty message")
        try:
            data = await self.client.send_text(jid, text)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            return SendResult(ok=False, error=f"{WA_UNAVAILABLE} {e}"[:300])
        except WhatsAppError as e:
            # 401/403: wrong key; 404/503: session missing or not connected.
            # None of them sent anything.
            if e.status in (401, 403, 404, 503):
                return SendResult(ok=False, error=f"{WA_UNAVAILABLE} {e}"[:300])
            return SendResult(ok=False, error=str(e)[:300])
        except httpx.HTTPError as e:
            # A timeout after the request went out may still have sent it.
            return SendResult(ok=False, error=f"maybe sent: {e}"[:300])
        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        msg_id = str(key.get("id") or "")
        return SendResult(ok=True, message_id=f"wa:{msg_id}" if msg_id else "",
                          thread_ref=str(key.get("remoteJid") or jid))

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        read_after = self._load_marker()
        lookback = timedelta(hours=max(1, int(self.wa.lookback_hours or 48)))
        floor = max(read_after, datetime.now(timezone.utc) - lookback)
        chats = await self.client.list_chats()
        newest = read_after
        out: list[InboundMessage] = []
        for chat in chats:
            jid = str(chat.get("jid") or "")
            if not (jid.endswith(_PHONE_JID) or jid.endswith(_LID_JID)):
                continue                          # groups, broadcasts, channels
            last = chat.get("lastMessage") or {}
            last_at = _parse_time(last.get("timestamp"))
            if last_at is None or last_at <= floor:
                continue
            # Every chat newer than the marker is read (no cut-off at
            # `limit`): the marker moves past all of them, so one left
            # unread now would never be read.
            newest = max(newest, last_at)
            msg = await self._read_chat(jid, chat, floor)
            if msg:
                out.append(msg)
        self._pending_marker = (newest - timedelta(seconds=1)).isoformat() \
            if newest > read_after else ""
        return out

    async def _read_chat(self, jid: str, chat: dict, floor: datetime) -> InboundMessage | None:
        messages = await self.client.chat_messages(jid)
        theirs: list[dict] = []
        for m in messages:                 # oldest first
            if m.get("fromMe"):
                theirs = []                # only what they wrote since our last word
                continue
            at = _parse_time(m.get("timestamp"))
            if at is None or at <= floor:
                continue
            if str(m.get("type") or "").upper() not in _TEXT_TYPES and not m.get("content"):
                continue                   # a sticker, a voice note with no caption
            theirs.append(m)
        if not theirs:
            return None
        text = "\n".join(str(m.get("content") or "").strip() for m in theirs).strip()
        if not text:
            return None
        newest = theirs[-1]
        key = str(newest.get("keyId") or newest.get("id") or "")
        address = wa_address(jid)
        if not key or not address:
            return None
        name = (newest.get("pushName") or chat.get("name") or chat.get("notify") or "").strip()
        return InboundMessage(
            provider_id=f"wa:{key}",
            from_email=address,
            subject="WhatsApp",
            body=text,
            message_id=f"wa:{key}",
            thread_ref=jid,
            date=str(newest.get("timestamp") or ""),
            headers={"push_name": name, "channel": "whatsapp"},
        )

    def confirm_read(self) -> None:
        if self._pending_marker:
            self._save_marker(self._pending_marker)
            self._pending_marker = ""

    async def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, ("WhatsApp isn't configured: set WA_AKG_URL and WA_AKG_API_KEY "
                           "in .env, and channels.whatsapp.enabled/session in harvey.yaml")
        try:
            info = await self.client.session_info()
        except (WhatsAppError, httpx.HTTPError) as e:
            return False, str(e)
        status = str(info.get("status") or "unknown")
        if status.upper() != "CONNECTED":
            return False, f"session '{self.client.session}' is {status}: scan the QR code in WA-AKG"
        return True, f"session '{self.client.session}' is connected"


# ── Routing between the mailbox and WhatsApp ─────────────────────────────


class RoutedProvider(MailProvider):
    """Email addresses go to the mailbox, WhatsApp addresses to WhatsApp.

    Either side may be missing (WhatsApp only, or email only). A read that
    fails on one side doesn't hide the other side's replies; it fails as a
    whole only when every configured side failed.
    """

    def __init__(self, email: MailProvider | None, whatsapp: WhatsAppProvider):
        self.email = email
        self.whatsapp = whatsapp
        self.name = email.name if email is not None else whatsapp.name
        self.last_problem = ""

    def _sides(self) -> list[MailProvider]:
        return [p for p in (self.email, self.whatsapp)
                if p is not None and p.is_configured()]

    def is_configured(self) -> bool:
        return bool(self._sides())

    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to=""):
        if is_wa_address(to_email):
            return await self.whatsapp.send_email(to_email, subject, body, thread_ref, in_reply_to)
        if self.email is None:
            return SendResult(ok=False, error="no mailbox is configured for email")
        return await self.email.send_email(to_email, subject, body, thread_ref, in_reply_to)

    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        out: list[InboundMessage] = []
        errors: list[str] = []
        sides = self._sides()
        for side in sides:
            try:
                out += await side.get_replies(limit)
            except Exception as e:
                logger.warning(f"Reading {side.name} failed: {e}")
                errors.append(f"{side.name}: {e}")
        self.last_problem = "; ".join(errors)
        if sides and len(errors) == len(sides):
            raise WhatsAppError(self.last_problem)
        return out

    def confirm_read(self) -> None:
        for side in self._sides():
            side.confirm_read()

    async def test_connection(self) -> tuple[bool, str]:
        results = []
        ok_all = True
        for side in (self.email, self.whatsapp):
            if side is None:
                continue
            ok, detail = await side.test_connection()
            ok_all = ok_all and ok
            results.append(f"{side.name}: {detail}")
        return ok_all, "; ".join(results)


# ── Turning found companies into WhatsApp contacts ───────────────────────


async def prospect_companies(state, config, env, limit: int = 50,
                             client: WaAkgClient | None = None) -> dict:
    """Companies Harvey found that have a phone and nobody to write to yet:
    those on WhatsApp become WhatsApp contacts (status "new"), so the writer
    drafts for them like any other prospect. Only numbers WA-AKG confirms are
    on WhatsApp are used; nothing is sent here."""
    from harvey.models.prospect import Prospect

    wa = config.channels.whatsapp
    client = client or WaAkgClient(env.wa_akg_url, env.wa_akg_api_key, wa.session)
    companies = await state.companies_with_phone_and_no_contacts(limit=limit * 3)
    numbers: dict[str, object] = {}
    for company in companies:
        digits = normalize_phone(company.phone, wa.default_country_code)
        if digits and digits not in numbers:
            numbers[digits] = company
        if len(numbers) >= limit:
            break
    summary = {"checked": len(numbers), "on_whatsapp": 0, "added": 0}
    if not numbers:
        return summary
    found = await client.check_numbers(list(numbers))
    summary["on_whatsapp"] = len(found)
    for digits, jid in found.items():
        company = numbers.get(digits)
        if company is None:
            continue
        address = wa_address(jid or digits)
        if not address or await state.get_prospect_by_email(address):
            continue
        prospect = Prospect(
            company_id=company.id,
            email=address,
            email_status="verified",      # WA-AKG confirmed the number is on WhatsApp
            email_verified=True,
            phone="+" + digits,
            phone_verified=True,
            company=company.name,
            industry=company.industry,
            title="WhatsApp",
            source=WA_SOURCE,
            source_url=company.website or company.source_url,
            personalization_notes=company.notes or company.description,
        )
        await state.add_prospect(prospect)
        summary["added"] += 1
    return summary


def inbound_prospect(address: str, push_name: str = ""):
    """A contact for someone who wrote to the WhatsApp number first."""
    from harvey.models.prospect import Prospect

    name = (push_name or "").strip()
    first, _, last = name.partition(" ")
    return Prospect(
        email=address,
        email_status="verified",
        email_verified=True,
        phone=phone_for_address(address),
        phone_verified=bool(phone_for_address(address)),
        first_name=first[:60],
        last_name=last[:60],
        title="WhatsApp",
        source=WA_INBOUND_SOURCE,
        status="contacted",
    )
