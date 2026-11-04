"""WhatsApp through WA-AKG: addresses, the gateway client, reading chats,
routing beside email, inbound contacts, the sender's caps, and Portuguese
opt-outs. WA-AKG is simulated with httpx.MockTransport; every number and
name is made up."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harvey.agents.handler import _opt_out_reason
from harvey.config import ChannelsConfig, WhatsAppChannelConfig
from harvey.integrations.mail_provider import InboundMessage, SendResult, uses_outbox
from harvey.integrations.whatsapp import (
    WA_UNAVAILABLE, RoutedProvider, WaAkgClient, WhatsAppProvider, is_wa_address,
    jid_for_address, language_for_address, normalize_phone, phone_for_address,
    prospect_companies, wa_address,
)
from harvey.models.company import Company
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    Cfg, FakeProvider, StubBrain, _now_iso, make_sender, seed_prospect, state,
)

NUMBER = "5511900000001"          # made up
ADDRESS = f"{NUMBER}@whatsapp.invalid"
JID = f"{NUMBER}@s.whatsapp.net"


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class WaCfg(Cfg):
    class channels(Cfg.channels):
        whatsapp = WhatsAppChannelConfig(enabled=True, session="sales-01")


class WaEnv:
    instantly_api_key = ""
    wa_akg_url = "http://gateway.example:3000"
    wa_akg_api_key = "wag_test"


class Gateway:
    """A tiny WA-AKG: chats, histories, sends, number checks."""

    def __init__(self):
        self.chats = []
        self.history = {}
        self.sent = []
        self.on_whatsapp = set()
        self.send_status = 200
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["X-API-Key"] == "wag_test"
        path = request.url.raw_path.decode()
        if request.method == "GET" and path == "/api/chat/sales-01":
            return httpx.Response(200, json={"status": True, "data": self.chats})
        if request.method == "GET" and path.startswith("/api/chat/sales-01/"):
            jid = httpx.URL(path).path.rsplit("/", 1)[1]
            from urllib.parse import unquote
            return httpx.Response(200, json={"status": True,
                                             "data": self.history.get(unquote(jid), [])})
        if request.method == "POST" and path.endswith("/send"):
            if self.send_status != 200:
                return httpx.Response(self.send_status, json={"status": False,
                                                              "message": "Session not connected"})
            from urllib.parse import unquote
            jid = unquote(path.split("/")[4])
            body = json.loads(request.content)
            self.sent.append((jid, body["message"]["text"]))
            return httpx.Response(200, json={"status": True, "data": {
                "key": {"remoteJid": jid, "fromMe": True, "id": f"OUT{len(self.sent)}"}}})
        if request.method == "POST" and path == "/api/chat/sales-01/check":
            numbers = json.loads(request.content)["numbers"]
            return httpx.Response(200, json={"status": True, "data": {"results": [
                {"number": n, "exists": n in self.on_whatsapp,
                 "jid": f"{n}@s.whatsapp.net" if n in self.on_whatsapp else None}
                for n in numbers]}})
        if request.method == "GET" and path == "/api/sessions":
            return httpx.Response(200, json={"status": True, "data": [
                {"sessionId": "sales-01", "status": "CONNECTED"}]})
        return httpx.Response(404, json={"status": False, "message": "no route"})


@pytest.fixture
def gateway():
    return Gateway()


@pytest.fixture
def provider(gateway, tmp_path):
    return WhatsAppProvider(WaCfg(), WaEnv(), transport=httpx.MockTransport(gateway),
                            state_path=tmp_path / "wa.json")


# ── Addresses ──

@pytest.mark.parametrize("raw,cc,expected", [
    ("+55 (11) 90000-0001", "55", NUMBER),
    ("(11) 90000-0001", "55", NUMBER),
    ("011 90000-0001", "55", NUMBER),
    ("5511900000001", "55", NUMBER),
    ("0055 11 90000-0001", "55", NUMBER),
    ("123", "55", ""),
    ("", "55", ""),
])
def test_phone_numbers_normalise(raw, cc, expected):
    assert normalize_phone(raw, cc) == expected


def test_addresses_round_trip_and_never_cover_groups():
    assert wa_address(JID) == ADDRESS
    assert jid_for_address(ADDRESS) == JID
    assert phone_for_address(ADDRESS) == "+" + NUMBER
    assert wa_address("123456789@lid") == "lid.123456789@whatsapp.invalid"
    assert jid_for_address("lid.123456789@whatsapp.invalid") == "123456789@lid"
    assert wa_address("120363000000@g.us") == ""
    assert wa_address("status@broadcast") == ""
    assert is_wa_address(ADDRESS) and not is_wa_address("jane@acme.example")
    assert jid_for_address("jane@acme.example") == ""


def test_the_reserved_address_passes_the_email_gate_shape():
    from harvey.gate import EMAIL_RE
    assert EMAIL_RE.match(ADDRESS)


def test_language_comes_from_the_country_code():
    assert language_for_address(ADDRESS) == "Brazilian Portuguese"
    assert language_for_address("447700900001@whatsapp.invalid") == ""


# ── Reading chats ──

@pytest.mark.asyncio
async def test_only_what_they_wrote_after_our_last_message_is_one_reply(gateway, provider):
    now = datetime.now(timezone.utc)
    provider._save_marker((now - timedelta(minutes=30)).isoformat())
    gateway.chats = [
        {"jid": JID, "name": "Ana Example",
         "lastMessage": {"content": "quanto custa?", "timestamp": _iso(now)}},
        {"jid": "120363000000@g.us",
         "lastMessage": {"content": "group chatter", "timestamp": _iso(now)}},
        {"jid": "5511900000002@s.whatsapp.net",
         "lastMessage": {"content": "old", "timestamp": _iso(now - timedelta(days=3))}},
    ]
    gateway.history[JID] = [
        {"keyId": "A1", "fromMe": False, "content": "oi", "type": "TEXT",
         "timestamp": _iso(now - timedelta(minutes=20))},
        {"keyId": "B1", "fromMe": True, "content": "Olá! Como posso ajudar?", "type": "TEXT",
         "timestamp": _iso(now - timedelta(minutes=10))},
        {"keyId": "A2", "fromMe": False, "content": "tenho uma loja", "type": "TEXT",
         "pushName": "Ana Example", "timestamp": _iso(now - timedelta(minutes=2))},
        {"keyId": "A3", "fromMe": False, "content": "quanto custa?", "type": "TEXT",
         "pushName": "Ana Example", "timestamp": _iso(now - timedelta(minutes=1))},
    ]
    replies = await provider.get_replies()
    assert len(replies) == 1
    msg = replies[0]
    assert msg.from_email == ADDRESS
    assert msg.body == "tenho uma loja\nquanto custa?"
    assert msg.provider_id == "wa:A3"
    assert msg.headers["push_name"] == "Ana Example"

    provider.confirm_read()
    assert await provider.get_replies() == []      # nothing newer than the marker


@pytest.mark.asyncio
async def test_chats_from_before_whatsapp_was_switched_on_are_never_read(gateway, provider):
    gateway.chats = [{"jid": JID, "lastMessage": {
        "content": "old", "timestamp": _iso(datetime.now(timezone.utc) - timedelta(hours=1))}}]
    assert await provider.get_replies() == []       # first read sets the marker to now


# ── Sending ──

@pytest.mark.asyncio
async def test_send_posts_only_the_body_to_the_jid(gateway, provider):
    result = await provider.send_email(ADDRESS, "subject is not sent", "Oi! Tudo bem?")
    assert result.ok and result.message_id == "wa:OUT1"
    assert gateway.sent == [(JID, "Oi! Tudo bem?")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 503, 401])
async def test_a_gateway_that_cant_send_says_unavailable(gateway, provider, status):
    gateway.send_status = status
    result = await provider.send_email(ADDRESS, "s", "Oi")
    assert not result.ok and result.error.startswith(WA_UNAVAILABLE)


@pytest.mark.asyncio
async def test_test_connection_reads_the_session(provider):
    ok, detail = await provider.test_connection()
    assert ok and "connected" in detail


# ── Routing ──

class _FakeWa(FakeProvider):
    name = "whatsapp"


@pytest.mark.asyncio
async def test_routing_sends_each_address_its_own_way():
    email, wa = FakeProvider(), _FakeWa()
    routed = RoutedProvider(email, wa)
    await routed.send_email(ADDRESS, "s", "b")
    await routed.send_email("jane@acme.example", "s", "b")
    assert [m["to"] for m in wa.sent] == [ADDRESS]
    assert [m["to"] for m in email.sent] == ["jane@acme.example"]


@pytest.mark.asyncio
async def test_one_side_failing_doesnt_hide_the_other():
    email, wa = FakeProvider(), _FakeWa()
    email.inbound = [InboundMessage(provider_id="e1", from_email="jane@acme.example", body="hi")]

    async def boom(limit=50):
        raise RuntimeError("gateway down")
    wa.get_replies = boom
    routed = RoutedProvider(email, wa)
    replies = await routed.get_replies()
    assert [r.provider_id for r in replies] == ["e1"]
    assert "gateway down" in routed.last_problem


@pytest.mark.asyncio
async def test_whatsapp_alone_has_no_email_side():
    routed = RoutedProvider(None, _FakeWa())
    result = await routed.send_email("jane@acme.example", "s", "b")
    assert not result.ok


def test_whatsapp_turns_on_the_outbox_even_with_instantly(monkeypatch):
    monkeypatch.setattr(Cfg.channels.email, "provider", "instantly")
    assert uses_outbox(WaCfg())


# ── Inbound contacts and replies ──

def _handler(state, provider, cfg=None, reply="Claro! Qual o tamanho da sua loja?"):
    from harvey.agents.handler import Handler
    brain = StubBrain(intent="question", reply=reply)
    prompts = []
    original = brain.think

    async def think(prompt, **kw):
        prompts.append(prompt)
        return await original(prompt, **kw)
    brain.think = think
    handler = Handler(brain=brain, state=state, config=cfg or WaCfg(), env=WaEnv())
    handler.provider = provider
    return handler, prompts


@pytest.mark.asyncio
async def test_someone_who_writes_in_becomes_a_contact_and_gets_a_drafted_answer(state):
    wa = _FakeWa()
    wa.inbound = [InboundMessage(
        provider_id="wa:A3", from_email=ADDRESS, subject="WhatsApp",
        body="quanto custa o plano?", message_id="wa:A3", thread_ref=JID,
        headers={"push_name": "Ana Example", "channel": "whatsapp"})]
    handler, prompts = _handler(state, wa)
    assert await handler._run_native() is True

    prospect = await state.get_prospect_by_email(ADDRESS)
    assert prospect is not None
    assert prospect.first_name == "Ana" and prospect.phone == "+" + NUMBER
    queued = await state.get_outbox(status="pending_review")
    assert len(queued) == 1 and queued[0]["to_email"] == ADDRESS
    assert any("This conversation is on WhatsApp" in p for p in prompts)
    convos = await state.get_conversations_by_status("open")
    assert convos and convos[0].channel == "whatsapp"


@pytest.mark.asyncio
async def test_with_answer_inbound_off_a_stranger_is_left_alone(state, monkeypatch):
    monkeypatch.setattr(WaCfg.channels, "whatsapp", WhatsAppChannelConfig(
        enabled=True, session="s", answer_inbound=False))
    wa = _FakeWa()
    wa.inbound = [InboundMessage(provider_id="wa:Z", from_email=ADDRESS, body="oi")]
    handler, _ = _handler(state, wa)
    await handler._run_native()
    assert await state.get_prospect_by_email(ADDRESS) is None
    assert await state.get_outbox(status="pending_review") == []


@pytest.mark.asyncio
async def test_sair_opts_them_out(state):
    await seed_prospect(state, email=ADDRESS, status="contacted")
    wa = _FakeWa()
    wa.inbound = [InboundMessage(provider_id="wa:S", from_email=ADDRESS, body="SAIR")]
    handler, _ = _handler(state, wa)
    await handler._run_native()
    assert (await state.get_prospect_by_email(ADDRESS)).status == "opted_out"


# ── Sending: opt-out line, caps, a gateway that's down ──

def _wa_sender(state, provider):
    from harvey.agents.sender import Sender
    sender = Sender(brain=None, state=state, config=WaCfg(), env=WaEnv())
    sender.provider = provider
    sender.send_pacing = False
    return sender


@pytest.mark.asyncio
async def test_a_first_whatsapp_message_carries_the_opt_out_line_not_the_postal_footer(state):
    pid = await seed_prospect(state, email=ADDRESS)
    await state.add_outbox_item(
        prospect_id=pid, to_email=ADDRESS, subject="oi", body="Oi! Posso te mostrar algo?",
        send_at=_now_iso(), status="approved", kind="sequence", step=1, campaign_id="c1")
    wa = _FakeWa()
    sender = _wa_sender(state, wa)
    from harvey.config import ComplianceConfig
    sender.config.compliance = ComplianceConfig(postal_address="1 Example Street")
    await sender._drain_due()
    assert len(wa.sent) == 1
    body = wa.sent[0]["body"]
    assert body.endswith("é só responder SAIR.")
    assert "Example Street" not in body


@pytest.mark.asyncio
async def test_first_whatsapp_messages_stop_at_their_daily_cap(state, monkeypatch):
    cfg_cap = 2
    monkeypatch.setattr(WaCfg.channels, "whatsapp", WhatsAppChannelConfig(
        enabled=True, session="s", max_daily_cold=cfg_cap))
    wa = _FakeWa()
    sender = _wa_sender(state, wa)
    for i in range(4):
        address = f"55119000001{i:02d}@whatsapp.invalid"
        pid = await seed_prospect(state, email=address)
        await state.add_outbox_item(
            prospect_id=pid, to_email=address, subject="oi", body="Oi! Posso ajudar?",
            send_at=_now_iso(), status="approved", kind="sequence", step=1, campaign_id="c1")
    await sender._drain_due()
    assert len(wa.sent) == cfg_cap
    assert len(await state.get_outbox(status="approved")) == 4 - cfg_cap


@pytest.mark.asyncio
async def test_a_message_the_gateway_couldnt_take_waits_instead_of_failing(state):
    pid = await seed_prospect(state, email=ADDRESS, status="contacted")
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email=ADDRESS, subject="Re: WhatsApp", body="Claro!",
        send_at=_now_iso(), status="approved", kind="reply")

    class Down(_FakeWa):
        async def send_email(self, *a, **k):
            return SendResult(ok=False, error=f"{WA_UNAVAILABLE} HTTP 503")
    sender = _wa_sender(state, Down())
    await sender._drain_due()
    assert (await state.get_outbox_item(item_id))["status"] == "approved"


# ── Prospecting found companies ──

@pytest.mark.asyncio
async def test_companies_with_a_whatsapp_number_become_contacts(state, gateway):
    on = await state.add_company(Company(name="Loja Exemplo", domain="loja.example",
                                         phone="(11) 90000-0001", industry="varejo"))
    await state.add_company(Company(name="Sem Zap", domain="semzap.example",
                                    phone="(11) 3000-0000"))
    await state.add_company(Company(name="No Phone", domain="nophone.example"))
    gateway.on_whatsapp = {NUMBER}
    client = WaAkgClient(WaEnv.wa_akg_url, WaEnv.wa_akg_api_key, "sales-01",
                         transport=httpx.MockTransport(gateway))
    result = await prospect_companies(state, WaCfg(), WaEnv(), client=client)
    assert result == {"checked": 2, "on_whatsapp": 1, "added": 1}
    prospect = await state.get_prospect_by_email(ADDRESS)
    assert prospect.company_id == on and prospect.email_status == "verified"
    assert prospect.status == "new"

    again = await prospect_companies(state, WaCfg(), WaEnv(), client=client)
    assert again["added"] == 0          # the company has a contact now


# ── Portuguese opt-outs ──

@pytest.mark.parametrize("text,opted_out", [
    ("Por favor me tira da lista", True),
    ("pare de me mandar mensagem", True),
    ("SAIR", True),
    ("sair, obrigado", True),
    ("Não tenho interesse.", True),
    ("não me manda mais isso", True),
    ("Não, obrigado", True),
    ("Quero me descadastrar", True),
    ("Não me procure mais", True),
    ("Qual o preço? Tenho interesse", False),
    ("não me mandaram a proposta ainda", False),
    ("se não tenho interesse eu aviso, manda o preço", False),
    ("Pode me mandar mais detalhes?", False),
    ("Não tenho interesse no plano anual, só no mensal", False),
    ("Não, obrigado pela informação!", False),
])
def test_portuguese_opt_outs(text, opted_out):
    assert (_opt_out_reason(text, text) is not None) is opted_out


# ── Config ──

def test_config_reads_a_whatsapp_section():
    channels = ChannelsConfig.model_validate({"whatsapp": {
        "enabled": True, "session": "sales-01", "default_country_code": "+55"}})
    assert channels.whatsapp.enabled and channels.whatsapp.default_country_code == "55"
    assert ChannelsConfig.model_validate({"whatsapp": None}).whatsapp.enabled is False
