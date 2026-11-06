"""identity_ready(), the dry-run guards of AdConnect/SambaInfo and the log statistics."""
import json
import logging

import pytest

import fakes


# -- identity_ready

def test_identity_ready_for_a_complete_directory(adconnect):
    assert adconnect().identity_ready() is True


def test_identity_not_ready_without_state(adconnect, tmp_path):
    assert adconnect(directory=str(tmp_path / "nothing")).identity_ready() is False


@pytest.mark.parametrize("status", ["bootstrapping", "failed", "", None])
def test_identity_not_ready_when_status_is_not_ready(adconnect, tmp_path, status):
    directory = str(tmp_path / "s")
    fakes.write_identity(directory, status=status)
    assert adconnect(directory=directory).identity_ready() is False


def test_identity_not_ready_without_certificate_file(adconnect, tmp_path):
    directory = str(tmp_path / "nopfx")
    fakes.write_identity(directory, pfx=False)
    assert adconnect(directory=directory).identity_ready() is False


def test_identity_of_another_tenant_is_not_ready(adconnect, tmp_path):
    directory = str(tmp_path / "other")
    fakes.write_identity(directory, tenant_id="another-tenant")
    assert adconnect(directory=directory, tenant_id="tenant-1").identity_ready() is False


def test_identity_without_configured_tenant_accepts_any_tenant(adconnect, tmp_path):
    directory = str(tmp_path / "any")
    fakes.write_identity(directory, tenant_id="whatever")
    assert adconnect(directory=directory, tenant_id=None).identity_ready() is True


# -- dry run: nothing is written to Entra

WRITES = [
    ("enable_ad_sync", (), "set_adsyncenabled"),
    ("set_desktop_sso_enabled", (), "set_desktop_sso_enabled"),
    ("set_desktop_sso", ("example.test", "secret"), "set_desktop_sso"),
    ("enable_password_hash_sync", (), "set_sync_features"),
    ("send_obj_to_az", ({"SourceAnchor": "a"},), "set_azureadobject"),
    ("delete_user", ("a",), "remove_azureadoject"),
    ("delete_group", ("a",), "remove_azureadoject"),
    ("delete_device", ("a",), "remove_azureadoject"),
    ("send_hashnt", ("hash", "a"), "set_userpassword"),
]


@pytest.mark.parametrize("method,arguments,call", WRITES, ids=[w[0] for w in WRITES])
def test_dry_run_sends_nothing_to_entra(adconnect, tenant, method, arguments, call):
    getattr(adconnect(confirmations=1, dry_run=True), method)(*arguments)
    assert tenant.count(call) == 0


@pytest.mark.parametrize("method,arguments,call", WRITES, ids=[w[0] for w in WRITES])
def test_real_run_sends_to_entra(adconnect, tenant, method, arguments, call):
    getattr(adconnect(confirmations=1, dry_run=False), method)(*arguments)
    assert tenant.count(call) == 1


def test_a_new_adconnect_defaults_to_dry_run(libsync, identity_dir):
    assert libsync.AdConnect(entra_aba_dir=identity_dir).dry_run is True


# -- dry run: nothing is written to Samba

class RecordingSamDB:
    def __init__(self):
        self.calls = []

    def newcomputer(self, *args, **kwargs):
        self.calls.append("newcomputer")

    def setpassword(self, *args, **kwargs):
        self.calls.append("setpassword")


def bare_sambainfo(libsync, dry_run):
    """The real SambaInfo without a Samba database behind it (its constructor needs one)."""
    info = libsync.RealSambaInfo.__new__(libsync.RealSambaInfo)
    info.dry_run = dry_run
    info.samdb_loc = RecordingSamDB()
    return info


def test_dry_run_does_not_create_the_sso_account(libsync):
    info = bare_sambainfo(libsync, dry_run=True)
    info.create_azureadssoacc()
    info.set_password_azureadssoacc(password="secret")
    assert info.samdb_loc.calls == []


def test_real_run_creates_the_sso_account_and_sets_its_password(libsync):
    info = bare_sambainfo(libsync, dry_run=False)
    info.create_azureadssoacc()
    info.set_password_azureadssoacc(password="secret")
    assert info.samdb_loc.calls == ["newcomputer", "setpassword"]


# -- statistics of the run summary

def test_log_entries_are_counted_per_type_and_reset(libsync):
    libsync.write_log_json_data("send", {"a": 1})
    libsync.write_log_json_data("send", {"a": 2})
    libsync.write_log_json_data("error", "x")

    assert libsync.take_stats() == {"send": 2, "error": 1}
    assert libsync.take_stats() == {}


def test_log_entry_is_one_json_line_with_type_timestamp_and_data(libsync, caplog):
    with caplog.at_level(logging.INFO, logger="azure_ad_sync"):
        libsync.write_log_json_data("send", {"SourceAnchor": "a"})

    entry = json.loads(caplog.records[-1].getMessage())
    assert entry["type"] == "send"
    assert entry["data"] == {"SourceAnchor": "a"}
    assert "timestamp" in entry
