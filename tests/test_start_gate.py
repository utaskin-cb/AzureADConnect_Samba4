"""The check of a newly created Entra application (AdConnect._wait_for_new_identity).

Entra applies the permissions of a new application some time after it is created, per
service and per request. The check waits until the directory sync service, the Graph
groups endpoint and the Graph users endpoint all answer in the same round.
"""
import pytest

import fakes
from fakes import ALL_OK, ALL_PENDING, pending


def clients_used(tenant):
    return len(tenant.clients)


# -- initial run: two rounds in a row

def test_initial_run_needs_two_passing_rounds_30_seconds_apart(adconnect, tenant, clock, capsys):
    adconnect(confirmations=2).connect()

    assert clock.sleeps == [30]
    assert clients_used(tenant) == 2
    assert "Checking again in 30 seconds" in capsys.readouterr().out


def test_failed_rounds_are_retried_until_two_in_a_row_pass(adconnect, tenant, clock, capsys):
    tenant.permissions = [ALL_PENDING, ALL_PENDING]
    adconnect(confirmations=2).connect()

    assert clock.sleeps == [30, 30, 30]          # fail, fail, pass, pass
    assert clients_used(tenant) == 4
    out = capsys.readouterr().out
    assert "attempt 1" in out and "attempt 2" in out


def test_a_failed_round_after_a_pass_starts_the_count_again(adconnect, tenant, clock):
    tenant.permissions = [ALL_OK, ALL_PENDING]    # pass, fail, then everything passes
    adconnect(confirmations=2).connect()

    assert clients_used(tenant) == 4              # pass, fail, pass, pass
    assert clock.sleeps == [30, 30, 30]


def test_alternating_rounds_never_confirm(adconnect, tenant, clock):
    tenant.permissions = [ALL_OK, ALL_PENDING] * 100
    with pytest.raises(RuntimeError) as error:
        adconnect(confirmations=2).connect()

    assert "after 30 minutes" in str(error.value)
    assert clock.now >= 30 * 60


def test_all_three_services_must_answer_in_the_same_round(adconnect, tenant, clock):
    # sync answers in round 1 while users do not; users answer in round 2 while sync does not
    tenant.permissions = [pending("users"), pending("sync"), ALL_OK, ALL_OK]
    adconnect(confirmations=2).connect()

    assert clients_used(tenant) == 4
    assert clock.sleeps == [30, 30, 30]


# -- service: one pass

def test_service_needs_one_passing_round_and_does_not_sleep(adconnect, tenant, clock):
    adconnect(confirmations=1).connect()

    assert clock.sleeps == []
    assert clients_used(tenant) == 1


def test_service_retries_a_failed_round_then_stops_after_one_pass(adconnect, tenant, clock):
    tenant.permissions = [ALL_PENDING]
    adconnect(confirmations=1).connect()

    assert clock.sleeps == [30]
    assert clients_used(tenant) == 2


# -- when no check is needed

def test_application_older_than_an_hour_is_not_checked(adconnect, tenant, clock, tmp_path):
    directory = str(tmp_path / "old")
    fakes.write_identity(directory, age_minutes=61)
    adconnect(directory=directory).connect()

    assert tenant.calls == [(0, "get_token", ["sync-scope"])]    # no probe at all
    assert clock.sleeps == []


@pytest.mark.parametrize("make_state", [
    pytest.param(lambda d: None, id="no state.json"),
    pytest.param(lambda d: fakes.write_identity(d, created_at=False), id="no created_at"),
], )
def test_unreadable_creation_time_means_no_check(adconnect, tenant, clock, tmp_path, make_state):
    directory = str(tmp_path / "x")
    make_state(directory)
    adconnect(directory=directory).connect()

    assert tenant.count("get_syncconfiguration") == 0
    assert clock.sleeps == []


def test_broken_state_json_means_no_check(adconnect, tenant, tmp_path):
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "state.json").write_text("{not json")
    adconnect(directory=str(directory)).connect()

    assert tenant.count("get_syncconfiguration") == 0


# -- once per process

def test_checked_once_per_process_not_once_per_object(adconnect, tenant, clock):
    adconnect(confirmations=2).connect()
    probes_after_first = tenant.count("get_syncconfiguration")
    second = adconnect(confirmations=2)
    second.connect()
    second.get_sync_configuration()

    assert tenant.count("get_syncconfiguration") == probes_after_first + 1   # only the real read
    assert clock.sleeps == [30]


def test_every_cycle_of_a_service_creates_a_new_object_without_checking_again(adconnect, tenant, clock):
    for _ in range(5):
        connection = adconnect(confirmations=1)
        connection.connect()
        connection.send_obj_to_az({"SourceAnchor": "a"})

    assert clients_used(tenant) == 5
    assert tenant.count("get_syncconfiguration") == 1


def test_a_failed_check_is_repeated_by_the_next_connection(adconnect, tenant, clock):
    tenant.default_permissions = ALL_PENDING
    with pytest.raises(RuntimeError):
        adconnect(confirmations=1).connect()

    tenant.default_permissions = ALL_OK
    clock.sleeps.clear()
    adconnect(confirmations=1).connect()           # not skipped because of the failure

    assert tenant.count("get_syncconfiguration") > 1


def test_get_sync_configuration_only_connects_and_reads(adconnect, tenant, clock):
    configuration = adconnect(confirmations=2).get_sync_configuration()

    assert configuration == {"IsDirSyncing": True, "DirSyncFeatures": 1}
    assert clock.sleeps == [30]                    # the check ran once, in connect()


# -- how a round is judged

def test_every_round_uses_a_new_client_because_msal_caches_the_first_token(adconnect, tenant):
    tenant.permissions = [ALL_PENDING, ALL_PENDING]
    adconnect(confirmations=2).connect()

    probing_clients = {c[0] for c in tenant.calls if c[1] == "get_syncconfiguration"}
    assert len(probing_clients) == 4
    assert len([c for c in tenant.calls if c[1] == "get_token"]) == 4


@pytest.mark.parametrize("answer", [
    pytest.param(pending("sync"), id="soap denied"),
    pytest.param(pending("groups", status=403), id="graph 403"),
    pytest.param(pending("users", status=401), id="graph 401"),
])
def test_the_ways_entra_says_not_yet_are_waited_for(adconnect, tenant, clock, answer):
    tenant.permissions = [answer]
    adconnect(confirmations=1).connect()

    assert clients_used(tenant) == 2


def test_names_of_the_pending_services_are_reported(adconnect, tenant, capsys):
    tenant.permissions = [pending("sync", "users")]
    adconnect(confirmations=1).connect()

    out = capsys.readouterr().out
    assert "waiting for: directory sync service, users (Graph)" in out


def test_timeout_message_names_what_is_still_pending(adconnect, tenant):
    tenant.default_permissions = pending("groups")
    with pytest.raises(RuntimeError) as error:
        adconnect(confirmations=1).connect()

    assert "groups (Graph)" in str(error.value)


@pytest.mark.parametrize("error", [
    pytest.param(ValueError("boom"), id="programming error"),
    pytest.param(fakes.GraphError(500), id="graph 500"),
    pytest.param(fakes.GraphError(404), id="graph 404"),
    pytest.param(Exception("connection reset"), id="network"),
])
def test_other_errors_are_not_waited_for(adconnect, tenant, clock, error):
    tenant.fail["get_syncconfiguration"] = error
    with pytest.raises(type(error)):
        adconnect(confirmations=1).connect()

    assert clock.sleeps == []


# -- helper

@pytest.mark.parametrize("error,expected", [
    (Exception("Access to Azure Active Directory has been denied"), True),
    (Exception("wrapped: Access to Azure Active Directory has been denied (Result)"), True),
    (fakes.GraphError(401), True),
    (fakes.GraphError(403), True),
    (fakes.GraphError(404), False),
    (fakes.GraphError(500), False),
    (ValueError("x"), False),
])
def test_permission_pending(libsync, error, expected):
    assert libsync.AdConnect._permission_pending(error) is expected
