# Telegram channel — status and Phase 6 plan

## Status at the end of Phase 5: prepared, not connected

| Item | State |
|---|---|
| Adapter `channels/telegram.py` | Built and tested with `MockTransport` (tests/test_telegram.py, deterministic) |
| Bot token | **None.** Not read, not stored, not required. `TELEGRAM_*` lines in `%LOCALAPPDATA%\hermes\.env` stay commented out |
| Bot registration (BotFather), webhook, public endpoint | **Not done** |
| Real messages sent or received | **None** |
| Production transport | `Disconnected` — any send raises; nothing can leave the machine |
| Console | Owner can issue a single-use link code for a registered customer: `POST /api/admin/customers/<cus_id>/telegram-code` (owner session, audited) |

## How identity works

1. The owner registers the customer (Memory tab) and issues a Telegram link code (valid 15 minutes, single use,
   only its SHA-256 hash stored in `federation/channels.db`; a new code voids the previous unused one).
2. The customer sends `/link CODE` to the bot. The numeric Telegram user id (never the username) is bound to
   that one customer. 5 wrong codes lock that Telegram account out of linking for 15 minutes.
3. Every later message from that account is handled as that customer: it goes to `<company>-receptionist`
   with `FED_CUSTOMER=<cus_id>`, history rebuilt from `conversations.db`, customer memory scope only.

There is **no** path from Telegram to the owner or to any admin action. Usernames, display names and message
text ("I am the owner", `/admin`) are never used to decide who someone is. Unlinked accounts get a fixed
"not linked" reply and the attempt is audited (`telegram.unauthorized`).

Message text is passed to the agent as one argument of a fixed `hermes -p <agent> -z <prompt>` call (no shell).
What the agent can then do is limited by its profile and the federation guard, as for the portal.

## Reliability

* `updates` table: each `update_id` is processed once; a redelivery does nothing. An update received but never
  finished (process stopped) is processed again after 15 minutes; the conversation store's message key still
  prevents a second agent answer.
* `outbox` table: replies are queued; a failed send is retried with backoff (5 s doubling, max 5 minutes),
  at most 5 attempts, then marked `dead`. A send failure never re-runs the agent.
* Malformed updates, group chats, bots and non-text messages are rejected and audited.

## Phase 6 steps (each needs the owner's approval)

1. **Create the bot** with BotFather (owner, by hand). Put the token only in the console settings file
   `C:\Users\aliab\hermes-federation\.env` as `TELEGRAM_BOT_TOKEN=...` — never in Git, chat, profiles or agent `.env` files.
2. **Add a real transport** class next to `MockTransport` with the same `send(chat_id, text)`:
   HTTPS POST to `api.telegram.org/bot<token>/sendMessage`, timeout 10 s, no redirects, token never logged
   (scrub like `console/models.py`). Raise on non-200 so the outbox retries.
3. **Receive updates by long polling first** (`getUpdates` with `offset`), run by the console process on a
   thread like the sync loop. No public endpoint is needed for polling. A webhook comes later only behind
   HTTPS (Cloudflare Tunnel / Tailscale Funnel) with Telegram's `secret_token` header verified on every call.
4. **Flush the outbox** on a timer (`bridge.flush()` every few seconds).
5. **Owner controls** in the Console: show linked accounts, unlink (`bridge.unlink`), issue codes (button next
   to each customer).
6. **Live test plan**: one test customer, one owner-controlled Telegram account; link, converse, redelivery,
   unlink, deactivate; verify audit rows and that no other customer's data is visible.
7. **Rollback**: stop the polling thread (setting off), revoke the token in BotFather; `channels.db` can stay.

WhatsApp is out of scope for Phase 5 and is not prepared here.
