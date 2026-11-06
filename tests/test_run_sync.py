"""run_sync.py as a program: first-run decision, run summary, failures, exit codes, logging.

Entra is the fake Tenant, Samba is fakes.FakeSamba, the state database is a real SQLite file.
"""
import copy
import json
import sqlite3

import pytest

import fakes
from conftest import StopService
from fakes import ALL_PENDING, FakeSamba, user

TWO_USERS = {"u1": user("u1"), "u2": user("u2")}
TWO_HASHES = {"u1": "aa", "u2": "bb"}


def directory(users=None, hashes=None):
    FakeSamba.users = copy.deepcopy(TWO_USERS if users is None else users)
    FakeSamba.hashes = copy.deepcopy(TWO_HASHES if hashes is None else hashes)


def rows(workdir):
    connection = sqlite3.connect(workdir.db)
    try:
        return connection.execute("SELECT sourceanchor, object_type FROM azureobject ORDER BY 1").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()


def log_types(workdir):
    try:
        lines = open(workdir.log, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    return [json.loads(line)["type"] for line in lines if line.startswith("{")]


def old_identity(workdir):
    fakes.write_identity(workdir.identity, age_minutes=24 * 60)


def stop_after(cycles):
    """End --service-mode after the given number of cycles (= sleeps of the service interval)."""
    return lambda seconds, sleeps: len([s for s in sleeps if s != 30]) >= cycles


# ---------------------------------------------------------------- first run is decided by the tenant

def test_empty_database_but_tenant_already_syncing_does_not_ask_or_enable(run_cli, tenant, workdir):
    directory()
    result = run_cli(tty=False)

    assert result.exit_code == 0
    assert tenant.count("set_adsyncenabled") == 0
    assert tenant.count("set_azureadobject") == 2          # empty database: everything is sent
    assert rows(workdir) == [("u1", "user"), ("u2", "user")]


def test_first_run_in_a_terminal_enables_directory_sync(run_cli, tenant, workdir):
    tenant.is_dir_syncing = False
    directory()
    result = run_cli(tty=True)

    assert result.exit_code == 0
    assert tenant.count("set_adsyncenabled") == 1
    assert "enable_ad_sync" in log_types(workdir)


def test_not_enabled_tenant_without_terminal_is_fatal(run_cli, tenant, workdir):
    tenant.is_dir_syncing = False
    directory()
    result = run_cli(tty=False)

    assert result.exit_code == 2
    assert "Fatal: DirSync is not enabled" in result.err
    assert tenant.count("set_adsyncenabled") == 0
    assert tenant.count("set_azureadobject") == 0
    assert "fatal" in log_types(workdir)


def test_dry_run_on_a_not_enabled_tenant_changes_nothing_and_does_not_fail(run_cli, tenant, workdir):
    tenant.is_dir_syncing = False
    directory()
    result = run_cli("--dryrun", tty=False)

    assert result.exit_code == 0
    assert not tenant.wrote_anything()
    assert rows(workdir) == []


def test_password_hash_sync_is_switched_on_when_the_feature_bit_is_missing(run_cli, tenant):
    tenant.dir_sync_features = 0
    directory()
    run_cli(tty=False)

    assert tenant.count("set_sync_features") == 1
    assert tenant.dir_sync_features & 1


def test_password_hash_sync_is_left_alone_when_already_on(run_cli, tenant):
    tenant.dir_sync_features = 1 | 8
    directory()
    run_cli(tty=False)

    assert tenant.count("set_sync_features") == 0


def test_password_hash_sync_is_not_touched_when_not_configured(run_cli, tenant):
    tenant.dir_sync_features = 0
    directory()
    run_cli(tty=False, conf={"hash_synchronization": "false"})

    assert tenant.count("set_sync_features") == 0
    assert tenant.count("set_userpassword") == 0


def test_a_service_asks_the_tenant_once_per_process(run_cli, tenant, workdir):
    old_identity(workdir)                     # no start check: only the real reads are counted
    directory()
    run_cli("--service-mode", tty=False, stop_when=stop_after(3))

    assert tenant.count("get_syncconfiguration") == 1


def test_tenant_check_that_failed_is_repeated_in_the_next_cycle(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    failures = [Exception("boom")]
    tenant.fail["get_syncconfiguration"] = lambda detail: failures.pop(0) if failures else None
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(2))

    assert tenant.count("get_syncconfiguration") == 2
    assert "Sync run failed: Exception: boom" in result.err
    assert tenant.count("set_azureadobject") == 2


# ---------------------------------------------------------------- identity

def test_missing_identity_without_terminal_is_fatal(run_cli, workdir, tenant):
    directory()
    fakes.write_identity(workdir.identity, status="bootstrapping")
    result = run_cli(tty=False)

    assert result.exit_code == 2
    assert "is not initialised" in result.err
    assert tenant.calls == []


def test_missing_identity_in_dry_run_is_fatal_even_in_a_terminal(run_cli, workdir, tenant):
    directory()
    fakes.write_identity(workdir.identity, status="bootstrapping")
    result = run_cli("--dryrun", tty=True)

    assert result.exit_code == 2
    assert "Run once interactively without --dryrun" in result.err
    assert tenant.calls == []


def test_missing_identity_in_a_terminal_continues_to_bootstrap(run_cli, workdir, tenant):
    directory()
    fakes.write_identity(workdir.identity, status="bootstrapping")
    result = run_cli(tty=True)

    assert result.exit_code == 0
    assert tenant.count("set_azureadobject") == 2


# ---------------------------------------------------------------- new application: the start check

def test_initial_run_on_a_new_application_waits_then_syncs(run_cli, tenant, workdir, clock):
    directory()
    tenant.permissions = [ALL_PENDING, fakes.ALL_OK, fakes.ALL_OK]    # fail, pass, pass
    result = run_cli(tty=True)

    assert result.exit_code == 0
    assert "Entra is still applying the permissions" in result.out
    assert "Sync run finished: send=2, send_nthash=2" in result.out
    assert tenant.count("set_azureadobject") == 2
    assert clock.sleeps == [30, 30]


@pytest.mark.parametrize("arguments,rounds", [((), 2), (("--service-mode",), 1)], ids=["initial run", "service"])
def test_initial_run_needs_two_rounds_service_needs_one(run_cli, tenant, arguments, rounds):
    directory()
    run_cli(*arguments, tty=False, stop_when=stop_after(1))

    probe_clients = {c[0] for c in tenant.calls if c[1] == "call_graphapi"}
    assert len(probe_clients) == rounds


def test_service_checks_a_new_application_only_in_the_first_cycle(run_cli, tenant, workdir):
    directory()
    run_cli("--service-mode", tty=False, stop_when=stop_after(4))

    assert tenant.count("call_graphapi") == 2           # groups + users of one probe round


def test_timeout_of_the_start_check_is_an_ordinary_failure_in_a_service(run_cli, tenant, workdir, clock):
    directory()
    tenant.default_permissions = ALL_PENDING
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(1))

    assert "Sync run failed: RuntimeError: Entra has not activated the permissions" in result.err
    assert tenant.count("set_azureadobject") == 0


def test_timeout_of_the_start_check_is_a_traceback_in_the_initial_run(run_cli, tenant, workdir):
    directory()
    tenant.default_permissions = ALL_PENDING
    result = run_cli(tty=True)

    assert isinstance(result.raised, RuntimeError)
    assert "30 minutes" in str(result.raised)


# ---------------------------------------------------------------- summary line

def test_summary_counts_what_was_sent(run_cli, workdir):
    directory()
    result = run_cli(tty=False)

    assert "Sync run finished: send=2, send_nthash=2" in result.out


def test_second_run_without_changes_says_nothing_to_send(run_cli, workdir):
    directory()
    run_cli(tty=False)
    result = run_cli(tty=False)

    assert "Sync run finished: nothing to send" in result.out


def test_changed_object_is_sent_again(run_cli, tenant, workdir):
    directory()
    run_cli(tty=False)
    FakeSamba.users["u1"]["displayName"] = "changed"
    tenant.sent.clear()
    result = run_cli(tty=False)

    assert [entry["SourceAnchor"] for entry in tenant.sent] == ["u1"]
    assert "send=1" in result.out and "send_nthash" not in result.out


def test_force_sends_everything_again(run_cli, tenant, workdir):
    directory()
    run_cli(tty=False)
    tenant.sent.clear()
    run_cli("--force", tty=False)

    assert len(tenant.sent) == 2


def test_dry_run_summary_is_marked_and_writes_nothing(run_cli, tenant, workdir):
    directory()
    result = run_cli("--dryrun", tty=False)

    assert "DRY RUN ON" in result.out
    assert "Sync run finished (dry run): " in result.out
    assert not tenant.wrote_anything()
    assert rows(workdir) == []


def test_summary_points_to_the_log_when_something_failed(run_cli, tenant, workdir):
    directory()
    tenant.fail["set_azureadobject"] = lambda detail: Exception("rejected") if detail == "u2" else None
    result = run_cli(tty=False)

    assert "error=1" in result.out
    assert "see %s" % workdir.log in result.out
    assert rows(workdir) == [("u1", "user")]


def test_object_that_failed_to_send_gets_no_password_hash(run_cli, tenant, workdir):
    directory()
    tenant.fail["set_azureadobject"] = lambda detail: Exception("rejected") if detail == "u2" else None
    run_cli(tty=False)

    assert [anchor for anchor, _ in tenant.hashes] == ["u1"]


# ---------------------------------------------------------------- service loop

def test_quiet_service_cycles_stay_quiet_after_the_first_one(run_cli, workdir):
    old_identity(workdir)
    directory()
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(3))

    assert result.out.count("Sync run finished") == 1        # first cycle sent everything; nothing after


def test_first_service_cycle_is_announced_even_when_nothing_is_sent(run_cli, workdir):
    old_identity(workdir)
    directory(users={}, hashes={})
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(2))

    assert result.out.count("Sync run finished: nothing to send") == 1


def test_service_cycle_with_a_change_is_reported(run_cli, workdir, tenant):
    old_identity(workdir)
    directory()

    def change_after_first_cycle(seconds, sleeps):
        FakeSamba.users["u1"]["displayName"] = "changed"       # between cycle 1 and cycle 2
        return len(sleeps) >= 2                                # stop at the end of cycle 2

    result = run_cli("--service-mode", tty=False, stop_when=change_after_first_cycle)

    assert result.out.count("Sync run finished") == 2
    assert "Sync run finished: send=1, send_nthash=" not in result.out
    assert "send=1" in result.out


def test_failed_cycle_is_reported_and_backs_off(run_cli, tenant, workdir, clock):
    old_identity(workdir)
    directory()
    tenant.fail["get_syncconfiguration"] = Exception("entra down")
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(4))

    assert "Sync run failed: Exception: entra down (details in %s), next try in 120 seconds" % workdir.log in result.err
    assert clock.sleeps == [120, 240, 480, 960]


def test_backoff_is_capped_at_one_hour(run_cli, tenant, workdir, clock):
    old_identity(workdir)
    directory()
    tenant.fail["get_syncconfiguration"] = Exception("entra down")
    run_cli("--service-mode", tty=False, stop_when=stop_after(8))

    assert max(clock.sleeps) == 3600


def test_backoff_resets_after_a_good_cycle_and_the_recovery_is_announced(run_cli, tenant, workdir, clock):
    old_identity(workdir)
    directory(users={}, hashes={})
    outcomes = [Exception("entra down"), None, None]
    tenant.fail["get_syncconfiguration"] = lambda detail: outcomes.pop(0) if outcomes else None
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(3))

    assert clock.sleeps[0] == 120 and clock.sleeps[1] == 60
    assert "Sync run failed" in result.err
    assert result.out.count("Sync run finished: nothing to send") == 1


def test_failure_in_the_initial_run_raises(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    tenant.fail["get_syncconfiguration"] = Exception("entra down")
    result = run_cli(tty=True)

    assert str(result.raised) == "entra down"
    assert "error" in log_types(workdir)


def test_fatal_error_ends_a_service_without_retrying(run_cli, tenant, workdir, clock):
    tenant.is_dir_syncing = False
    directory()
    result = run_cli("--service-mode", tty=False, stop_when=stop_after(1))

    assert result.exit_code == 2
    assert clock.sleeps == []


# ---------------------------------------------------------------- hash sending

def test_hash_rejected_with_result_waits_once_and_retries(run_cli, tenant, workdir, clock):
    old_identity(workdir)
    directory()
    attempts = {"u1": 0}

    def too_fast(anchor):
        if anchor == "u1" and attempts["u1"] == 0:
            attempts["u1"] = 1
            return Exception("Result: object not found yet")

    tenant.fail["set_userpassword"] = too_fast
    result = run_cli(tty=False)

    assert [anchor for anchor, _ in tenant.hashes] == ["u1", "u2"]
    assert clock.sleeps.count(30) == 1


def test_hash_that_still_fails_is_logged_and_skipped(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    tenant.fail["set_userpassword"] = lambda anchor: Exception("Result: gone") if anchor == "u1" else None
    result = run_cli(tty=False)

    assert [anchor for anchor, _ in tenant.hashes] == ["u2"]
    assert "use --force" in result.out
    assert "error=1" in result.out


# ---------------------------------------------------------------- deletions

def test_object_missing_in_samba_is_deleted_in_entra_and_in_the_database(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    run_cli(tty=False, conf={"do_delete": "true"})
    del FakeSamba.users["u2"]
    tenant.cloud_users = [{"onPremisesImmutableId": "u1", "onPremisesSyncEnabled": True},
                          {"onPremisesImmutableId": "u2", "onPremisesSyncEnabled": True}]
    run_cli(tty=False, conf={"do_delete": "true"})

    assert tenant.count("remove_azureadoject") == 1
    assert rows(workdir) == [("u1", "user")]


def test_dry_run_deletes_nothing_in_entra_or_the_database(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    run_cli(tty=False, conf={"do_delete": "true"})
    del FakeSamba.users["u2"]
    tenant.cloud_users = [{"onPremisesImmutableId": "u2", "onPremisesSyncEnabled": True}]
    run_cli("--dryrun", tty=False, conf={"do_delete": "true"})

    assert tenant.count("remove_azureadoject") == 0
    assert rows(workdir) == [("u1", "user"), ("u2", "user")]


# ---------------------------------------------------------------- single sign-on account

SSO = {"enable_single_sign_on": "true"}


def test_sso_dry_run_only_logs_what_would_happen(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    FakeSamba.sso_exists = False
    run_cli("--dryrun", tty=True, conf=SSO)

    assert tenant.count("set_desktop_sso") == 0 and tenant.count("set_desktop_sso_enabled") == 0
    assert FakeSamba.last.calls == []


def test_sso_without_terminal_logs_an_error_instead_of_asking(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    FakeSamba.sso_exists, FakeSamba.sso_expired = True, True
    run_cli(tty=False, conf=SSO)

    assert tenant.count("set_desktop_sso") == 0
    assert "error" in log_types(workdir)


def test_sso_in_a_terminal_creates_and_enables_the_account(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    FakeSamba.sso_exists = False
    run_cli(tty=True, conf=SSO)

    assert tenant.count("set_desktop_sso_enabled") == 1
    assert tenant.count("set_desktop_sso") == 1
    assert FakeSamba.last.calls == ["create_azureadssoacc", "set_password_azureadssoacc"]


def test_sso_account_that_exists_and_is_current_is_left_alone(run_cli, tenant, workdir):
    old_identity(workdir)
    directory()
    FakeSamba.sso_exists, FakeSamba.sso_expired = True, False
    run_cli(tty=True, conf=SSO)

    assert tenant.count("set_desktop_sso") == 0


# ---------------------------------------------------------------- logging

def test_real_run_logs_to_the_logfile_not_to_the_console(run_cli, workdir):
    directory()
    result = run_cli(tty=False)

    assert log_types(workdir).count("send") == 2
    assert '"type": "send"' not in result.out + result.err


def test_dry_run_logs_to_the_console_and_not_to_the_logfile(run_cli, workdir):
    directory()
    result = run_cli("--dryrun", tty=False)

    assert '"type": "send"' in result.out
    assert log_types(workdir) == []


def test_without_logfile_the_log_goes_to_stderr(run_cli, workdir):
    directory()
    result = run_cli(tty=False, conf={"logfile": ""})

    assert '"type": "send"' in result.err


def test_exactly_one_log_handler_is_installed(run_cli, workdir):
    import logging
    directory()
    run_cli(tty=False)
    run_cli(tty=False)

    assert len(logging.getLogger().handlers) == 1


def test_dry_run_flag_on_the_command_line_beats_the_configuration(run_cli, tenant, workdir):
    directory()
    run_cli("--dryrun", tty=False, conf={"dry_run": "false"})
    assert not tenant.wrote_anything()

    run_cli("--no-dryrun", tty=False, conf={"dry_run": "true"})
    assert tenant.count("set_azureadobject") == 2


def test_dry_run_in_the_configuration_is_honoured(run_cli, tenant, workdir):
    directory()
    run_cli(tty=False, conf={"dry_run": "true"})

    assert not tenant.wrote_anything()
