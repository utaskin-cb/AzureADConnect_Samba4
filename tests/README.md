# Tests

Run from the repository root:

    pip install pytest peewee
    python3 -m pytest

No Samba, no Entra tenant and no AADInternals_python submodule are needed. `tests/fakes.py`
holds the stand-ins:

- `Tenant` / `FakeEntraClient`: an Entra tenant. `tenant.permissions` is a list with one
  entry per client that gets created; it says which services (`sync`, `groups`, `users`)
  answer "not yet" for that client. `tenant.fail["set_azureadobject"] = exc` makes a call fail.
  `tenant.calls` records every call.
- `FakeSamba`: replaces `libsync.SambaInfo`; fill `FakeSamba.users`, `.groups`, `.hashes`.

| File | Covers |
|---|---|
| `test_start_gate.py` | check of a newly created Entra application: two rounds in a row on the initial run, one in the service, timeout, once per process |
| `test_identity_and_dry_run.py` | `identity_ready()`, dry run never writes to Entra or Samba, log statistics |
| `test_run_sync.py` | `run_sync.py` as a program: first run decided by the tenant, exit codes, run summary, service loop and backoff, deletions, single sign-on, logging |

Not covered: the LDAP queries of `SambaInfo.generate_all_dict`, the real AADInternals_python
library, the real timing of Entra (only simulated answers).
