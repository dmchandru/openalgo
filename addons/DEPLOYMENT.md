# Deploying this fork on AWS

Written for this deployment specifically: a fork of `marketcalls/openalgo` with
a local overlay in `addons/`, trading a real account from India.

**Recommendation: one EC2 instance with an Elastic IP, installed with the
project's own `install/install.sh`.** Not Elastic Beanstalk. The reasons below
are not preferences — each one is a way EB either has already failed here or
will fail on a schedule set by the exchange.

## Why not Elastic Beanstalk

**The database is destroyed on every deploy.** EB replaces `/var/app/current`
with each new bundle. `.ebextensions/01_flask.config` creates
`/var/app/current/db`, which is *inside* that directory. `db/openalgo.db` holds
the encrypted WhatsApp device session, the signal group configuration, the open
positions and the `scalping_sl_state` rows the risk monitor watches. Deploy
while a position is open and that position is left with **nothing watching its
stop**. Working around this needs EFS, RDS, or a symlink out of the deploy path
— three ways to buy back something EC2 gives for free.

**The static IP requirement collides with EB's lifecycle.** Under the SEBI/NSE
implementation standards for API-based order placement, the IP your orders come
from must be registered with the broker, and
[the project's own note](https://docs.openalgo.in/installation-guidelines/static-ip.html)
records the exchange rule that it may be changed **no more than once in a
calendar week**. EB replaces instances on platform updates, configuration
changes and health events, and a replaced instance comes up with a new public
IP. When that happens, order placement is rejected by the broker and you may not
be able to re-register the new address for up to a week. A single EC2 instance
with an Elastic IP simply does not have this failure mode.

**The platform fights the worker this codebase needs.** Two of these were the
actual cause of the failed deploy on 2026-09-18:

- EB's default WSGI module is `application:application`. The `WSGIPath: "app:app"` in `.ebextensions` did not take effect — settings saved on the environment take precedence over `.ebextensions` — and the deploy died with `ModuleNotFoundError: No module named 'application'`.
- EB's default worker is `gthread`. Flask-SocketIO keeps state in-process and the green/real-thread rules throughout this codebase are written for eventlet's patched stdlib. See the eventlet section of [`/CLAUDE.md`](../CLAUDE.md).
- Neither `gunicorn` nor `eventlet` was in `requirements.txt`, and the platform's only install step is `pip install -r requirements.txt`. Both are now appended there, but note what EB installed on its own: **gunicorn 26.2.0**, and gunicorn 26 removed the eventlet worker entirely. The platform's default toolchain is incompatible with the worker this code requires.

`Procfile` and the `requirements.txt` additions fix all three and the deploy can
be made to work. They are kept because they are correct anyway — the Procfile is
inert outside EB. But fixing the boot does not fix the first two problems, and
those are the ones that cost money.

## What to build instead

| | |
| --- | --- |
| Service | EC2, single instance, no load balancer, no autoscaling |
| Region | `ap-south-1` (Mumbai) — nearest to the Indian exchanges |
| Instance | `t3.small` (2 vCPU, 2 GB). The documented minimum is 2 GB RAM, or 0.5 GB plus 2 GB swap, so `t3.micro` works with swap configured |
| OS | Ubuntu Server 22.04 LTS or later |
| Storage | Default EBS root volume, 20 GB. This is where the database lives and why it survives |
| Networking | **Elastic IP**, associated permanently. AWS bills every public IPv4 address, roughly a few dollars a month |
| Security group | 443 and 80 from anywhere (Let's Encrypt needs 80), 22 from your own address only. Nothing else — the WebSocket proxy on 8765 and ZeroMQ on 5555 are localhost-only |
| Backups | A daily EBS snapshot via Data Lifecycle Manager |

Single instance is not a cost compromise. One Gunicorn worker is mandatory
because Flask-SocketIO state is in-process, and a WhatsApp linked device can
only run in one place — two instances would read every group message twice and
place every order twice.

## Installing

Point DNS at the Elastic IP first; the installer requests a certificate for the
domain and needs it resolving.

```sh
ssh ubuntu@<elastic-ip>
mkdir -p ~/openalgo-install && cd ~/openalgo-install
wget https://raw.githubusercontent.com/marketcalls/openalgo/main/install/install.sh
chmod +x install.sh
sudo ./install.sh
```

It prompts for the domain, broker and API credentials, then sets up nginx with a
Let's Encrypt certificate and auto-renewal, a systemd unit `openalgo.service`,
and gunicorn on a Unix socket with `--worker-class eventlet -w 1`. The
application lands in `/var/python/openalgo/`.

### Then re-point it at this fork

The installer clones `marketcalls/openalgo`, which does not contain `addons/`.
Fix that once, and the overlay arrives with the next pull:

```sh
cd /var/python/openalgo
sudo git remote set-url origin https://github.com/dmchandru/openalgo.git
sudo git remote add upstream https://github.com/marketcalls/openalgo.git
sudo git pull origin main
sudo systemctl restart openalgo
```

Nothing else is needed for the add-on: `app.py` calls `install_addons(app)` at
import, which creates its tables, attaches the WhatsApp hook and registers
`/whatsapp-signals`.

The installer's own script is not edited to do this. Its clone URL is hardcoded
to upstream, and re-pointing the remote afterwards keeps the fork's upstream
footprint where [`README.md`](README.md) says it is.

## Upgrading

```sh
cd ~/openalgo-install
wget https://raw.githubusercontent.com/marketcalls/openalgo/main/install/update.sh
chmod +x update.sh
sudo ./update.sh
```

`update.sh` backs up the databases, pulls `origin` on the current branch,
reinstalls dependencies, runs the migrations and restarts the service without
touching `.env`. Because `origin` is this fork, that pull brings `addons/` too.

Taking upstream changes is a separate step, done on a workstation rather than on
the server, so a merge conflict is never resolved on a live trading box:

```sh
git fetch upstream
git merge upstream/main
uv run pytest addons/whatsapp_signals/tests/ -q
git push origin main
# then on the server:
sudo ./update.sh
```

After any upgrade, check the log for `has changed shape` or `could not attach` —
that is the WhatsApp hook reporting that upstream moved the method it wraps. It
fails safe, which is precisely why it has to be checked rather than noticed.

## Why the database needs nothing special here

`*.db` is in `.gitignore`, so `db/openalgo.db` is never tracked. A `git pull`
cannot clobber it, `update.sh` backs it up before pulling, and it sits on the
EBS root volume, which survives reboots, stop/start and every upgrade. The only
thing that destroys it is terminating the instance, which is what the daily
snapshot is for.

This is the whole of the persistence story, and it is the clearest single
argument for EC2 over Elastic Beanstalk: there is no persistence story to write.

## Operational notes

**Broker tokens expire daily at about 3:00 AM IST**, so expect to log in each
trading day. This is the broker's schedule, not a deployment problem.

**Keep `APP_KEY`, `API_KEY_PEPPER` and `FERNET_SALT` stable forever.** The
WhatsApp device session and the stored broker tokens are encrypted with keys
derived from the last two. Rotating them means re-pairing the device and losing
stored credentials. Back up `.env` somewhere outside the instance.

**`gunicorn` is pinned below 26 on purpose.** Gunicorn 26 removed the eventlet
worker. The project is migrating to `gthread --threads 64 --workers 1`, but that
work is
[explicitly experimental](https://docs.openalgo.in/installation-guidelines/getting-started/gthread-migration.html)
and its own documentation says not to run it on a production trading account you
cannot afford to babysit. Stay on eventlet and gunicorn 25 until that lands;
revisit at each upstream merge.

**Do not add a load balancer** without re-reading the static IP section. An ALB
does not change the address your orders leave from, but the instance behind it
being replaced does.

## If you stay on Elastic Beanstalk anyway

The boot is fixed, so it will run. Before trading real money on it, at minimum:

- Associate an Elastic IP with the environment's instance and register it with the broker, accepting that an instance replacement still breaks order placement until you re-associate it.
- Move the database off the deploy path — EFS, or `DATABASE_URL` pointed at RDS. A symlink to `/var/app/db` survives deploys but not instance replacement.
- Set the environment to single instance, and never let a platform update run during market hours.

That is three workarounds to reach where a single EC2 instance starts.
