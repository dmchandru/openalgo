# WhatsApp Signals

Reads trading signals out of a WhatsApp group, places the orders, and keeps
their stop losses current as the group revises them.

A group posts this over twenty minutes:

```
BUY NIFTY 25000 CE @ 120 SL 100 TGT 150
SL to 110
book half
exit
```

That becomes: a market order for one lot with a stop at 100 and a target at
150; the stop moved to 110; half the position closed; the rest closed. Between
each message the platform's own tick-driven risk monitor is watching the
position, so if the stop is hit before the next message arrives, it is the
monitor that gets there first.

## How it fits together

```
linked WhatsApp device        upstream bot, already receiving group messages
        |                     hooks.py wraps its inbound handler
        v
     ingest.py                one worker, off the bot's loop, strictly in order
        |
        v
     parser.py                regex tier: deterministic, free, reads most of it
     llm.py                   model tier: only what the first cannot read
        |
        v
    resolver.py               does this contract exist? which expiry? what lot?
        |
        v
    executor.py               gates, caps, the order, and the stop row
        |
        v
  scalping_risk_monitor       the platform's stop engine takes the position from here
```

**There is no price loop in this add-on.** Stops, targets and trailing are
owned by `services/scalping_risk_monitor_service.py`, which already watches the
live feed, trails on ticks, and fires a freeze-safe exit sized to the live
position. An entry here writes one row into `scalping_sl_state`; a later "SL to
110" updates that same row. Positions opened from a signal therefore appear in
the `/scalping` terminal's stop list and are covered by its Close-All.

## Setup

1. **Link the device.** Profile menu &rarr; **WhatsApp Bot** &rarr; scan the QR
   with the phone that is in the signal group. This is upstream's pairing; see
   [`docs/whatsapp.md`](../../docs/whatsapp.md).
2. **Let the group post once.** Open `/whatsapp-signals`. Every group the
   device sees appears in the list, disabled, the first time a message arrives
   in it. Nothing is read from a group until you enable it.
3. **Configure the group.** Click **Settings** on it:

   | Setting | What it does |
   | --- | --- |
   | Trade in | Sandbox or live money. Start on sandbox. |
   | Product | MIS for intraday, NRML to carry. Corrected automatically for equity signals. |
   | Lots per signal | Used when the message does not say a size. |
   | Never more than | Hard cap; a message asking for more is trimmed to this. |
   | Open positions cap | Refuses a new entry while this many are open. |
   | Signals per day cap | Refuses everything once the group has traded this many today. |
   | Default stop (% of entry) | Applied when a signal states no stop of its own. |
   | Default target (%) | Same, for targets. Blank means no target. |
   | Trail the stop / step | Hands the position to the monitor's trailing engine. |
   | Only these senders | Blank means any member. Otherwise, phone numbers, comma separated. |
   | Use the model | Whether messages the rules cannot read go to the configured LLM. |
   | Message me what was done | A WhatsApp note to your own chat for every order, refusal and failure. |

4. **Test before trusting.** Paste real messages from the group into **Test a
   message** on the same page. It shows what each would do, resolves the
   contract, and sends nothing.
5. **Enable it.** Watch the signal log for a session on sandbox before
   switching the group to live.

## What it reads

The regex tier is deterministic and handles the shapes a signal group actually
posts. It costs nothing and runs on every message.

| Message | Read as |
| --- | --- |
| `BUY NIFTY 25000 CE @ 120` | entry, 1 lot, default stop |
| `BUY BANKNIFTY 52000 PE ABOVE 200 SL 170 TGT 260` | entry with both levels |
| `NIFTY 25000 CE BUY` / `CMP 120` / `SL 100` / `TGT 150` (one message, four lines) | the same entry |
| `Buy NIFTY 25OCT25 25000 CE around 118` | entry in a named expiry |
| `BUY RELIANCE @ 1450 SL 1420` | equity entry |
| `BUY NIFTY FUT @ 25010` | near-month future |
| `2 lots BUY NIFTY 25000 CE @ 120` | entry, two lots (capped by the group's limit) |
| `SL to 110`, `Revise SL 115`, `Trail SL to 130` | stop change |
| `SL at cost`, `move SL to cost` | stop to the entry price |
| `TGT 150`, `new target 160` | target change |
| `book half`, `book 50%`, `exit 25%`, `get out of half` | partial exit |
| `exit`, `square off`, `book full profit`, `EXIT ALL` | full exit |

**And what it deliberately does not read.** A group posts instructions and
reports in the same voice, seconds apart. These are all left alone:

| Message | Why |
| --- | --- |
| `Target 150 achieved` | already happened |
| `SL hit` | a report; the monitor already acted |
| `Booked half at 150` | past tense |
| `TGT 1 done` | a report |
| `what's the view on banknifty?` | a question |

Ambiguity resolves to doing nothing, every time. A missed signal costs an
opportunity; an invented one costs money.

### The model tier

A message the rules cannot read goes to whichever model is configured for the
Agent, but only if it carries trading vocabulary and does not read as a report.
Most group chatter never reaches it. The model is given a closed schema, and a
reply that is not valid JSON, names an unknown action, or omits what its action
needs is discarded rather than guessed at.

With no model configured, the add-on runs on the regex tier alone.

## Which position a follow-up applies to

- The message names its leg (`NIFTY 25000 CE SL 110`) &rarr; that leg.
- It names nothing and the group holds exactly one position &rarr; that one.
- It names nothing and the group holds several &rarr; **refused**, and the log
  says which positions were open. A stop moved onto the wrong leg is worse than
  a stop not moved.
- `exit` / `exit all` with several open and no leg named &rarr; closes them all,
  which is the one unqualified instruction that is unambiguous.

## Safety

- **A group is disabled until you enable it.** Being in the group grants
  nothing.
- **Sandbox groups cannot fire live orders.** A group set to sandbox while the
  platform is live is refused outright. The reverse is allowed: a live group
  while the platform is in analyze mode runs in the sandbox, because the global
  toggle is you saying nothing real should go out.
- **Caps are enforced before the order path**: lots per signal, open positions,
  signals per day, and an optional sender allowlist.
- **A repeated message does not trade twice.** An identical message that
  already traded within 90 seconds is dropped as a duplicate.
- **Exits are sized to the live position.** If the monitor's stop already
  flattened the leg, a group "exit" sends nothing rather than opening a short.
- **A failed exit leaves the position open and managed.** Its stop stays in
  place and the log says it is still open.
- **Nothing is on `/api/v1/`.** A leaked API key cannot point this deployment at
  a new group. The page and its endpoints are session-authenticated.

## The log

Every message from an enabled group is recorded with what it was read as and
what came of it: `executed`, `rejected`, `failed`, `duplicate` or `ignored`. The
table is capped at 5,000 rows, roughly a month for a busy group.

When something did not trade, the log says why in plain words -- the contract
was not listed, the group was over its cap, the platform was live while the
group was on sandbox, or the message named no position.

## Upgrading

```sh
cd upgrade && uv run migrate_all.py            # upstream migrations
cd .. && uv run python -m addons.whatsapp_signals.migrate
```

The add-on's tables are also created at start-up, so a fresh install needs
nothing. The migration exists for the case start-up cannot cover: a column
added to a table that already has rows. `--status` reports without changing.

## Running it on Elastic Beanstalk

The [Elastic Beanstalk
guide](https://docs.openalgo.in/installation-guidelines/getting-started/amazon-elastic-beanstalk)
deploys a ZIP that **replaces the whole application bundle on every deploy**,
and does not address SQLite persistence. That matters more here than it does
for most of the platform, because `db/openalgo.db` holds:

- the encrypted WhatsApp device session -- losing it means scanning the QR again,
- this add-on's group settings and signal log,
- the open-position rows, and the `scalping_sl_state` rows the risk monitor
  watches.

A deploy that wipes the database while a position is open leaves that position
with **nothing watching its stop**. Before going live, do one of:

- put `db/` on a mounted EBS volume via `.ebextensions`, or
- point `DATABASE_URL` at RDS.

Two more things that guide's defaults imply:

- **Keep it single-instance.** No load balancer, no autoscaling. The platform
  requires one worker (in-process SocketIO state), and a WhatsApp linked device
  can only run in one place. Two instances would double every order.
- **`APP_KEY`, `API_KEY_PEPPER` and `FERNET_SALT` must be identical across
  deploys.** The device session blob is encrypted with a key derived from the
  last two; change either and the session is unrecoverable.

Deploy while flat, not while holding a position.

## When it is not working

Check `/whatsapp-signals` first: the strip at the top says whether the device is
linked, whether the reader is running, and which mode the platform is in.

| Symptom | Where to look |
| --- | --- |
| No groups listed | The device is not linked, or no group has posted since it was. |
| A group is listed but nothing is logged | It is not enabled. |
| Messages logged as `ignored` | Paste one into **Test a message** to see how it read. |
| `not listed in any unexpired contract` | Download the master contract; the strike or expiry may also be wrong. |
| Everything `rejected` with a mode message | The group is on sandbox while the platform is live, or vice versa. |
| Nothing at all after an upstream upgrade | Search the log for `has changed shape`: the inbound hook did not attach. See [`../README.md`](../README.md). |

Technical detail goes to `log/errors.jsonl`, as everywhere else in the platform.

## Tests

```sh
uv run pytest addons/whatsapp_signals/tests/ -q
```

The parser tests are the ones to read first. Every message that must **not**
trade is a row in them, because that is the failure that costs money.
