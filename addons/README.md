# Add-ons

Code this deployment owns, kept apart from upstream OpenAlgo.

Everything under `addons/` is local to this fork. Upstream knows nothing about
it, so `git pull` from upstream can never conflict with any of it. That is the
whole point of the directory.

## The contract

**Exactly one upstream file is modified.** `app.py` carries these lines, after
its own blueprint registrations:

```python
# Local add-ons (this deployment's own code, kept out of upstream files so
# an upstream pull cannot conflict with it). See addons/README.md.
from addons import install_addons

install_addons(app)
```

An upstream change to `app.py` conflicts with this only if it lands on the same
few lines, which is a one-line resolution rather than a merge.

**Nothing under `frontend/` is touched.** Each add-on serves its own page from
its own blueprint, in plain HTML, CSS and JavaScript. Upstream CI force-commits
a built `frontend/dist/` to `main`; a fork that also builds `dist` conflicts on
generated bundles at every pull, which is the one merge nobody wants to resolve.
Staying out of `frontend/` avoids it entirely, and means no Node.js is needed to
deploy.

**Nothing under `docs/` or `upgrade/` is touched.** Each add-on documents itself
in its own directory and ships its own migration script.

**Upstream tables are not altered.** Add-ons create their own tables, prefixed
with their own name, in the same database.

**An add-on that cannot start is skipped, not fatal.** `install_addons` catches
per add-on and logs. A local feature must never stop the trading platform it
sits on from booting.

## Reaching into upstream

Where an add-on needs to observe upstream behaviour it does so by wrapping a
method at install time rather than editing the file. That trade is deliberate:
a wrapper keeps the diff at zero and costs some obviousness, so every wrapper
must

- be idempotent, and flag the function it installed,
- verify the signature it is wrapping and **refuse to install** if it has
  changed, logging what to fix,
- call the original unchanged, so no upstream behaviour is altered,
- be listed here.

Wrappers currently installed:

| Add-on | Upstream target | Why |
| --- | --- | --- |
| `whatsapp_signals` | `services.whatsapp_bot_service.WhatsAppBotService._handle_inbound` | Offers inbound WhatsApp **group** messages to the signal reader. Upstream ignores them; the original is still called, so commands and alerts behave exactly as before. |

## Remotes

| Remote | Points at | Used for |
| --- | --- | --- |
| `origin` | this deployment's own repository | where work is pushed |
| `upstream` | `https://github.com/marketcalls/openalgo.git` | read-only, pulled from |

A clone of `marketcalls/openalgo` starts with `origin` pointing at upstream, so
the topology has to be set up once before anything is pushed:

```sh
git remote rename origin upstream
git remote add origin <this deployment's repository URL>
git push -u origin main
```

If `git remote -v` shows `origin` on `marketcalls/openalgo`, that has not been
done and a push is aimed at upstream.

## After pulling upstream

```sh
git fetch upstream
git log --oneline HEAD..upstream/main          # what is new
git merge upstream/main                        # conflicts should be app.py only, if any
uv run pytest addons/whatsapp_signals/tests/ -q
grep -n "install_addons" app.py                # the hook survived the merge
uv run python -m addons.whatsapp_signals.migrate --status
```

Then start the app and search the log for `has changed shape` or
`could not attach` — see below.

If an upstream change moved a wrapped method, the add-on logs at start-up that
it could not attach and keeps the platform running. Search the log for
`could not attach` or `has changed shape` after any upgrade.

## Removing an add-on

Delete its directory and its entry from `addons/__init__.py`. Its tables stay
behind holding their data; drop them by hand if that is what you want.

## Add-ons here

| Directory | What it does |
| --- | --- |
| [`whatsapp_signals/`](whatsapp_signals/README.md) | Reads trading signals from a WhatsApp group, places the orders, and keeps their stops current as the group revises them. |
