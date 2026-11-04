# WhatsApp (WA-AKG)

Harvey can sell over WhatsApp through [WA-AKG](https://github.com/mrifqidaffaaditya/WA-AKG),
a self-hosted WhatsApp gateway. It runs beside email: one outbox, one approval
queue, one set of daily caps and opt-out rules for both.

What it does:

- **Answers people who write in.** Anyone who messages your WhatsApp number
  becomes a contact, and Harvey drafts an answer in chat style, in their
  language. Several short messages in a row are read as one reply and get one
  answer.
- **Prospects.** Companies Harvey found that have a phone number are checked
  against WhatsApp through WA-AKG. Only numbers on WhatsApp become contacts,
  and the writer drafts short WhatsApp openers for them (Brazilian Portuguese
  for +55 numbers).
- **Follows up and stops.** Up to two short nudges follow the first message,
  like the email sequence, and stop the moment they answer. "SAIR", "pare de
  me mandar mensagem", "me tira da lista", "não tenho interesse" and the like
  opt them out for good.

Nothing goes out without your approval unless you turn on autopilot
(`channels.email.require_approval: false`, the same switch as email).

## A word on bans

WA-AKG uses Baileys, an unofficial WhatsApp Web client. WhatsApp bans numbers
that send a lot of unsolicited messages, and that applies to cold prospecting
above all. To keep the number:

- Use a dedicated business number, not your personal one.
- Keep `max_daily_cold` low (the default is 15) and raise it slowly.
- Approve the first batches by hand and read what goes out.
- Expect answering people who wrote in to be safe, and cold messages to be
  the risk.

Opt-out rules (LGPD in Brazil, GDPR in the EU) apply to WhatsApp as they do
to email. Harvey honours every opt-out, but whether you may write to a
business at all is yours to judge.

## Setup

1. Have WA-AKG running with a session connected (QR code scanned).
2. In WA-AKG, create an API key: Dashboard → Settings → API Keys.
3. Put the gateway and key in `.env`:

   ```
   WA_AKG_URL=http://localhost:3000
   WA_AKG_API_KEY=wag_...
   ```

4. Turn the channel on in `harvey.yaml` (or `harvey.local.yaml` if you trained
   Harvey, since that file wins):

   ```yaml
   channels:
     whatsapp:
       enabled: true
       session: "sales-01"        # the WA-AKG session id
       default_country_code: "55"
       answer_inbound: true
       auto_prospect: false
       max_daily_cold: 15
   ```

5. Check the connection:

   ```bash
   harvey whatsapp test
   ```

6. Turn found companies into WhatsApp contacts by hand the first time:

   ```bash
   harvey whatsapp prospects --limit 20
   ```

   With `auto_prospect: true`, Harvey does this every cycle.

7. Start Harvey (`harvey run`). Drafts appear in the dashboard Outbox.

WhatsApp works with any email provider except Instantly, or on its own. With
Instantly as the email provider, only WhatsApp goes through the outbox.

## How it works

The rest of Harvey is keyed on email addresses, so a WhatsApp contact gets a
reserved address that can never be mailed: `5511912345678@whatsapp.invalid`
(`.invalid` is reserved by RFC 2606). A routing provider sends those addresses
through WA-AKG and everything else through your mailbox. That's why WhatsApp
contacts show up in Contacts, the Outbox and Conversations with that address.

Replies are polled, not pushed, so Harvey doesn't need a public URL: every
cycle it lists WA-AKG's chats, opens the private ones with a new message, and
reads what the contact wrote since your last message. Group chats, broadcasts
and channels are never read. Chats from before WhatsApp was switched on are
never answered; the read marker lives in `data/whatsapp_state.json`. The
reply delay is the heartbeat interval (`usage.heartbeat_interval_minutes`).

If WA-AKG is down or the session is logged out, messages stay approved and go
out on a later cycle instead of failing.

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `false` | Turns the channel on. |
| `session` | `""` | The WA-AKG session whose number Harvey uses. |
| `default_country_code` | `"55"` | Added to numbers written without `+`. |
| `answer_inbound` | `true` | People who write first become contacts and get answers. |
| `auto_prospect` | `false` | Turn found companies into WhatsApp contacts every cycle. |
| `max_daily_cold` | `15` | First (cold) WhatsApp messages a day, inside the email caps. |
| `lookback_hours` | `48` | How far back a chat is read. |
| `opt_out_line` | Portuguese "reply SAIR" line | Added under cold messages instead of the email footer. |
