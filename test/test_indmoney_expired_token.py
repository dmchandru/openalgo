"""An expired INDmoney token must not empty the symbol table (3 Nov 2026).

The morning login resumed the previous day's session: INDstocks tokens last 24
hours, the funds check got 401, but get_margin_data returns
DEFAULT_MARGIN_RESPONSE on any HTTP error, so the dead token looked valid. The
master contract download then got 403 on every segment; master_contract_download
deleted symtoken before looking at the result, and _download_master_contract
overwrote the broker's "error" status with "success" and recorded the download,
so the next login skipped it as "already downloaded today". No quotes, option
chain or feed resolved a symbol until a manual force download worked.

Network, database and socket calls are all replaced.
"""

import atexit
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DB = Path(__file__).resolve().parents[1] / "tmp" / "test_indmoney_expired_token.db"
TEST_DB.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}")
atexit.register(lambda: TEST_DB.unlink(missing_ok=True))

import broker.indmoney.api.auth_api as auth_api  # noqa: E402
import broker.indmoney.api.funds as funds  # noqa: E402
import broker.indmoney.database.master_contract_db as mcd  # noqa: E402
import utils.auth_utils as auth_utils  # noqa: E402

FRAME = pd.DataFrame([{"symbol": "NIFTY", "exchange": "NSE_INDEX", "token": "13"}])


# --- session resume -------------------------------------------------------


@pytest.mark.parametrize(
    "verdict, expected",
    [
        ((False, "Unable to validate user token"), (False, "Unable to validate user token")),
        ((True, None), (True, None)),
        # An outage is not evidence against the token; resume must not be refused for it.
        ((None, "HTTP 503"), (True, None)),
    ],
)
def test_resume_check_fails_only_a_rejected_token(monkeypatch, verdict, expected):
    monkeypatch.setattr(auth_api, "validate_access_token", lambda token: verdict)
    assert funds.test_auth_token("token") == expected


# --- master contract download ---------------------------------------------


class Spy:
    def __init__(self):
        self.deleted = 0
        self.copied = 0
        self.statuses = []
        self.emitted = []

    def emit(self, event, payload):
        self.emitted.append(payload)
        return payload


@pytest.fixture
def spy(monkeypatch):
    s = Spy()
    monkeypatch.setattr(mcd, "socketio", s)
    monkeypatch.setattr(mcd, "delete_symtoken_table", lambda: setattr(s, "deleted", s.deleted + 1))
    monkeypatch.setattr(mcd, "copy_from_dataframe", lambda df, broker="indmoney": setattr(s, "copied", s.copied + 1))
    monkeypatch.setattr(mcd, "delete_indmoney_temp_data", lambda path: None)
    monkeypatch.setattr(mcd, "process_indmoney_csv", lambda path: FRAME.copy())
    import database.master_contract_status_db as status_db

    monkeypatch.setattr(status_db, "update_status", lambda broker, status, message, total_symbols=None:
                        s.statuses.append(status))
    return s


def test_a_rejected_download_keeps_the_existing_symbols(spy, monkeypatch):
    monkeypatch.setattr(mcd, "download_csv_indmoney_data",
                        lambda path: ["equity: HTTP 403", "fno: HTTP 403", "index: HTTP 403"])

    mcd.master_contract_download()

    assert (spy.deleted, spy.copied) == (0, 0)
    assert spy.statuses[-1] == "error"
    assert spy.emitted[-1]["status"] == "error"
    assert "keeping the existing symbols" in spy.emitted[-1]["message"]
    assert "fno: HTTP 403" in spy.emitted[-1]["message"]


def test_a_partial_download_also_keeps_them(spy, monkeypatch):
    """Equity without F&O is not a contract worth replacing the full one with."""
    monkeypatch.setattr(mcd, "download_csv_indmoney_data", lambda path: ["fno: HTTP 403"])

    mcd.master_contract_download()

    assert (spy.deleted, spy.copied) == (0, 0)


def test_a_good_download_still_replaces_the_table(spy, monkeypatch):
    monkeypatch.setattr(mcd, "download_csv_indmoney_data", lambda path: [])

    mcd.master_contract_download()

    assert (spy.deleted, spy.copied) == (1, 1)
    assert spy.statuses[-1] == "success"


# --- the shared download wrapper ------------------------------------------


@pytest.fixture
def wrapper(monkeypatch):
    calls = {"status": [], "stats": 0}
    monkeypatch.setattr(auth_utils, "update_status",
                        lambda broker, status, message, total_symbols=None: calls["status"].append(status))
    monkeypatch.setattr(auth_utils, "update_download_stats",
                        lambda *a, **k: calls.__setitem__("stats", calls["stats"] + 1))
    monkeypatch.setattr(auth_utils, "get_exchange_stats_from_db", lambda: {})
    import database.master_contract_cache_hook as hook
    import sandbox.catch_up_processor as catch_up

    monkeypatch.setattr(hook, "hook_into_master_contract_download", lambda broker: None)
    monkeypatch.setattr(catch_up, "run_catch_up_tasks", lambda: None)
    return calls


def _broker_module(monkeypatch, final_status):
    module = type("M", (), {"master_contract_download": staticmethod(lambda: None)})
    monkeypatch.setattr(auth_utils.importlib, "import_module", lambda path: module)
    monkeypatch.setattr(auth_utils, "get_status", lambda broker: {"status": final_status})


def test_a_failure_the_broker_reported_is_not_marked_success_or_recorded(wrapper, monkeypatch):
    _broker_module(monkeypatch, "error")

    auth_utils._download_master_contract("indmoney")

    assert "success" not in wrapper["status"]
    assert wrapper["stats"] == 0      # the next login downloads again


def test_a_completed_download_is_marked_success_and_recorded(wrapper, monkeypatch):
    _broker_module(monkeypatch, "downloading")

    auth_utils._download_master_contract("indmoney")

    assert wrapper["status"][-1] == "success"
    assert wrapper["stats"] == 1
