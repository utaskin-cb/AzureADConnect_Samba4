"""Test set-up: fake the outside world (Samba, Entra, the AADInternals library) and give every
test a fresh libsync module, a controllable clock and a way to run run_sync.py as a program."""
import importlib
import logging
import os
import runpy
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakes  # noqa: E402


def _install_stub_modules():
    """libsync imports samba, ldb and AADInternals_python at import time. None of them is
    needed for the logic under test, so they are replaced by empty modules."""
    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        sys.modules[name] = module
        return module

    stub("ldb", SCOPE_SUBTREE=2, SCOPE_BASE=0, binary_encode=lambda value: value)
    samba = stub("samba")
    samba.param = stub("samba.param")
    stub("samba.auth", system_session=None)
    stub("samba.credentials", Credentials=object)
    stub("samba.samdb", SamDB=object)
    stub("samba.dsdb", UF_ACCOUNTDISABLE=2)
    samba.getopt = stub("samba.getopt")
    stub("AADInternals_python")
    stub("AADInternals_python.AADInternals", AADInternals=None, SYNC_SCOPE="sync-scope")
    stub("AADInternals_python._aad_certificate_manager", read_state=fakes.read_state)
    stub("AdAccountReader", AdAccountReader=object)


_install_stub_modules()


class Clock:
    """Replaces time.sleep / time.monotonic: sleeping costs no time and is recorded."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.stop_when = None            # callable(seconds, sleeps) -> True ends a service loop

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.stop_when and self.stop_when(seconds, self.sleeps):
            raise StopService()


class StopService(BaseException):
    """Ends the endless loop of --service-mode (BaseException: not caught by 'except Exception')."""


@pytest.fixture
def tenant():
    return fakes.Tenant()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr("time.sleep", clock.sleep)
    return clock


@pytest.fixture
def libsync(tenant, clock):
    """A freshly imported libsync (module flags such as _identity_checked start clean),
    wired to the fake tenant and the fake clock."""
    sys.modules.pop("libsync", None)
    module = importlib.import_module("libsync")
    module.AADInternals = tenant.new_client
    module.RealSambaInfo = module.SambaInfo      # kept for tests of the class itself
    module.SambaInfo = fakes.FakeSamba
    module.time = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    fakes.FakeSamba.reset()
    yield module
    sys.modules.pop("libsync", None)


@pytest.fixture(autouse=True)
def clean_logging():
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.WARNING)


@pytest.fixture
def identity_dir(tmp_path):
    """An identity directory of an application created one minute ago (a new application)."""
    directory = str(tmp_path / "entra_aba")
    fakes.write_identity(directory, age_minutes=1)
    return directory


@pytest.fixture
def adconnect(libsync, identity_dir):
    """Factory for AdConnect objects on the fake tenant."""
    def make(directory=None, confirmations=2, dry_run=False, **attrs):
        connection = libsync.AdConnect(entra_aba_dir=directory or identity_dir)
        connection.confirmations = confirmations
        connection.dry_run = dry_run
        for name, value in attrs.items():
            setattr(connection, name, value)
        return connection
    return make


class Stdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty

    def read(self, *args):
        return ""

    def readline(self, *args):
        return ""


class Result:
    def __init__(self, exit_code, out, err, namespace, raised):
        self.exit_code = exit_code
        self.out = out
        self.err = err
        self.namespace = namespace
        self.raised = raised


DEFAULT_CONF = {
    "dry_run": "false", "hash_synchronization": "true", "sync_device": "false", "proxy": "",
    "SourceAnchorAttr": "objectSid", "write_msDSConsistencyGuid_if_empty": "false",
    "use_msDSConsistencyGuid_if_exist": "false", "do_delete": "false",
    "create_service_connection_point": "false",
}


@pytest.fixture
def workdir(tmp_path, identity_dir):
    """Paths used by a run: configuration, database, log file, identity directory."""
    return types.SimpleNamespace(
        conf=str(tmp_path / "azure.conf"), db=str(tmp_path / "sync.db"),
        log=str(tmp_path / "sync.log"), identity=identity_dir, tmp=tmp_path)


@pytest.fixture
def run_cli(libsync, workdir, monkeypatch, capsys, clock):
    """Run run_sync.py as a program (like systemd or the administrator does) against the
    fakes and return its exit code, stdout and stderr.

        result = run_cli("--service-mode", tty=False, conf={"do_delete": "true"})
    """
    def run(*argv, tty=False, conf=None, stop_when=None):
        options = dict(DEFAULT_CONF)
        options.update(dbpath=workdir.db, entra_aba_dir=workdir.identity, logfile=workdir.log)
        options.update(conf or {})
        with open(workdir.conf, "w", encoding="utf-8") as stream:
            stream.write("[common]\n" + "".join("%s = %s\n" % item for item in options.items()))
        clock.stop_when = stop_when
        monkeypatch.setattr(sys, "argv", ["run_sync.py", "--conf", workdir.conf] + list(argv))
        monkeypatch.setattr(sys, "stdin", Stdin(tty))
        capsys.readouterr()
        namespace, exit_code, raised = None, 0, None
        try:
            namespace = runpy.run_path(os.path.join(ROOT, "run_sync.py"), run_name="__main__")
        except SystemExit as exc:
            exit_code = exc.code
        except StopService:
            pass
        except Exception as exc:       # a run that ends with a traceback in a terminal
            raised = exc
        captured = capsys.readouterr()
        return Result(exit_code, captured.out, captured.err, namespace, raised)
    return run
