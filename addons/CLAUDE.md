# CLAUDE.md — addons/

Guidance for Claude Code working anywhere under `addons/`. This file carries
what is **not discoverable by reading the code**: why this directory exists at
all, the constraints that are not negotiable, and the defects already paid for
once.

Read this before changing anything here. The parent
[`/CLAUDE.md`](../CLAUDE.md) still applies — every platform invariant in it
(eventlet, NullPool, `services/risk/`, order paths, trader-facing messages)
binds this code too.

## This is a fork overlay, and that is a hard constraint

This repository is a **fork of `marketcalls/openalgo` deployed on AWS Elastic
Beanstalk**. Upstream is pulled regularly. Every file outside `addons/` is
upstream's, and every line written into one of them is a merge conflict that
recurs on every pull, forever.

So the rule is not a style preference:

> **New local functionality goes in `addons/`. Nothing else is edited.**

The current upstream footprint is **one file, six lines** — the
`install_addons(app)` call in `app.py`. If a change seems to need a second
upstream edit, that is the signal to find another way, not to make the edit.
[`addons/README.md`](README.md) records the contract and every wrapper
installed against upstream code.

Specifically, do **not**:

- add a page under `frontend/src/` — upstream CI force-commits a built
  `frontend/dist/` to `main`, so a fork that also builds `dist` conflicts on
  generated bundles at every pull. Add-ons serve their own pages from their own
  blueprint in plain HTML/CSS/JS. This also means the EB deploy needs no
  Node.js.
- add a file under `docs/` or `upgrade/` — each add-on documents itself and
  ships its own migration script.
- add a line to `utils/db_sessions.py`, `upgrade/migrate_all.py`, or any other
  upstream registry — register from inside `addons/` instead.
- alter an upstream table. Add-ons create their own, prefixed with their name.

### Reaching into upstream behaviour

Where an add-on must observe upstream, it wraps a method at install time rather
than editing the file. That costs obviousness, so every wrapper must be
idempotent, verify the signature it wraps, **refuse to install** if it changed,
call the original unchanged, and be listed in `addons/README.md`.

`whatsapp_signals/hooks.py` is the worked example. Copy its shape.

## Remotes and merging upstream

| Remote | Points at | Used for |
| --- | --- | --- |
| `origin` | this deployment's own repository | where work is pushed |
| `upstream` | `https://github.com/marketcalls/openalgo.git` | read-only, pulled from |

If `git remote -v` shows `origin` pointing at `marketcalls/openalgo`, the
topology has not been set up yet and a push would be aimed at upstream. Fix
that before pushing anything.

Pulling upstream:

```sh
git fetch upstream
git log --oneline HEAD..upstream/main          # what is new
git merge upstream/main                        # conflicts should be app.py only, if any
uv run pytest addons/whatsapp_signals/tests/ -q
grep -n "install_addons" app.py                # the hook survived the merge
uv run python -m addons.whatsapp_signals.migrate --status
```

Then start the app and check the log for `has changed shape` or
`could not attach`, which is a wrapper reporting that upstream moved the method
it hooks. The add-on keeps the platform running when that happens; it just
stops reading signals, silently from the user's point of view, which is why the
check belongs in the upgrade routine and not in someone's memory.

## The add-ons

### `whatsapp_signals`

Reads trading signals from a WhatsApp group, places the orders, and keeps their
stops current as the group revises them. Full documentation:
[`whatsapp_signals/README.md`](whatsapp_signals/README.md).

Module map, in the order a message moves through them:

| Module | Responsibility |
| --- | --- |
| `hooks.py` | wraps the upstream bot's inbound handler; offers group messages |
| `ingest.py` | one worker thread, bounded queue, gating, duplicate suppression |
| `parser.py` | regex tier. **No I/O** — that is what makes it testable |
| `llm.py` | model tier, for what the regex tier cannot read |
| `resolver.py` | master-contract lookup: does this contract exist, which expiry, what lot |
| `executor.py` | mode gate, caps, the order, and the stop row |
| `db.py` | three `wa_signal_*` tables |
| `routes.py` + `web/` | the operator page at `/whatsapp-signals` |

#### Invariants — do not break these

**No second risk evaluator.** Stops, targets and trailing belong to
`services/scalping_risk_monitor_service.py`. An entry here writes one row into
`scalping_sl_state`; a later "SL to 110" updates that same row. If a change
seems to need a price loop, an exit timer or a stop comparison in this add-on,
it is the wrong change — the platform treats a second evaluator as a defect and
[`/CLAUDE.md`](../CLAUDE.md) documents the four ways the last one went wrong.

**Ambiguity resolves to doing nothing.** A missed signal costs an opportunity;
an invented one costs money. This governs the parser (a report is not an
instruction), the LLM tier (a reply missing what its action needs is
discarded), and follow-up matching (an unqualified "SL to 110" with two
positions open is refused, not guessed).

**The sandbox gate is asymmetric, deliberately.** A group set to sandbox while
the platform is live is **refused** — executing it would send a real order
nobody asked for. A group set to live while the platform is in analyze mode
**runs in the sandbox**, because the global toggle is the operator saying
nothing real should go out. Do not "fix" this into symmetry.

**Exits are sized to the live position, never to our own record.** The risk
monitor may have flattened the leg a second ago on its own stop. Reading the
position book first means a group "exit" after that sends nothing, instead of
opening a short.

**A refused or failed exit leaves the position open and managed.** The stop row
stays, the position row stays open, and the message says STILL OPEN. Reporting
success and clearing state is how a position ends up with nothing watching it.

**One leg, one open position row, one stop row.** The stop is keyed by
`(symbol, exchange, product, mode)`, so two rows would fight over it. An add to
a held leg updates the one row and re-weights its entry price.

#### Defects already found and fixed — do not reintroduce

Each is pinned by a test. If one of these tests starts failing, the fix is the
code, not the test.

| Defect | Test |
| --- | --- |
| `get out of half the position` read as a **full** exit: a full-exit verb with a partial size | `test_parser.py` action table |
| `reduce 25%` built a phantom leg from the letters in "REDU-**CE**", which a later "SL to 110" could match itself against | `test_a_word_ending_in_ce_is_not_an_option_leg` |
| `Target 150 achieved` set a target: a number does not make an instruction | `test_a_report_with_a_number_in_it_is_still_a_report` |
| A `SELL` on a leg already held long **added** to it instead of being refused | `test_a_sell_on_a_leg_already_held_long_is_refused` |
| An add left two different entry prices on record, so "SL to cost" and the monitor's trailing baseline disagreed | `test_adding_to_a_leg_keeps_one_position_at_a_weighted_cost` |
| Session cleanup hand-listed four modules while one order touches a dozen | `executor._remove_sessions` uses `utils.db_sessions` |
| Saving settings for a never-seen group failed on a comparison against None (column defaults apply at flush, not construction) | `test_a_group_can_be_configured_before_it_has_ever_been_seen` |

#### Known unverified assumption

Whether `wars` fires `@wa.on_message` for **group** messages has not been
confirmed against a live linked device — `wars` was not installed in the
environment this was built in. Upstream's own `jid_to_phone` handles `@g.us`
JIDs, which is strong evidence, but it is evidence, not proof.

To confirm: open `/whatsapp-signals` and check that a group appears with a
rising message count after the group posts. If none ever do, that assumption is
what failed, and the fix is localized to `hooks.py`.

#### Working on it

```sh
uv run pytest addons/whatsapp_signals/tests/ -q     # 136 tests
uv run ruff check addons/ && uv run ruff format addons/
```

The project's `testpaths` is `test`, so a bare `uv run pytest` does **not**
collect these; name the directory. The tests set `DATABASE_URL` to a temporary
file at import, so run them as their own invocation rather than alongside the
upstream suite.

Read `test_parser.py` first. Every message that must **not** trade is a row in
it, because that is the failure that costs money.

When changing an order path, mutation-test the guard: break it deliberately and
confirm a named test fails. Two guards were verified this way — the already-flat
reconciliation and the failed-exit-stays-open rule.

## Deployment

Elastic Beanstalk, single instance, following
[the upstream guide](https://docs.openalgo.in/installation-guidelines/getting-started/amazon-elastic-beanstalk).
Two consequences that bear on any change here:

- **A deploy replaces the whole bundle**, and the guide does not address SQLite
  persistence. `db/openalgo.db` holds the encrypted WhatsApp device session,
  the group config, the open positions and the stop rows the risk monitor
  watches. Losing it mid-session leaves real positions unwatched. `db/` belongs
  on a mounted EBS volume, or `DATABASE_URL` on RDS.
- **Single instance is mandatory**, and not only for the usual reason (one
  Gunicorn worker, in-process SocketIO state). A WhatsApp linked device can run
  in one place; two instances would read every group message twice and double
  every order.

`APP_KEY`, `API_KEY_PEPPER` and `FERNET_SALT` must be identical across deploys.
The device session blob is encrypted with a key derived from the last two.
