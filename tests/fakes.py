"""Stand-ins for the two things the tool talks to: Microsoft Entra and Samba.

Nothing here touches a network or a Samba database. The fakes answer the way the real
services answered in our tests, including the "not yet" answers of a new Entra application.
"""
import datetime
import json
import os

DENIED = "Access to Azure Active Directory has been denied"
SERVICES = ("sync", "groups", "users")


class GraphError(Exception):
    """What a failing Graph call looks like to the caller: an exception with .response.status_code."""

    def __init__(self, status):
        super().__init__("HTTP %d" % status)
        self.response = type("Response", (), {"status_code": status})()


def pending(*services, status=403):
    """One round of a new application in which only the named services answer "not yet"."""
    answer = {}
    for service in services:
        answer[service] = "denied" if service == "sync" else status
    return answer


ALL_OK = {}                                   # every service answers
ALL_PENDING = pending("sync", "groups", "users")


class Tenant:
    """The state of one Entra tenant plus the log of what was asked of it.

    permissions: one entry per client that gets created (a client = one msal token). The
    entry says which services answer "not yet" for that client; after the list is used up
    default_permissions applies. A new Entra application behaves like this per request.
    """

    def __init__(self):
        self.tenant_id = "tenant-1"
        self.is_dir_syncing = True
        self.dir_sync_features = 1            # bit 1 = password hash sync
        self.permissions = []
        self.default_permissions = ALL_OK
        self.fail = {}                        # method name -> exception (or callable(kwargs) -> exception/None)
        self.cloud_users = []
        self.cloud_groups = []
        self.calls = []                       # (client index, method, detail)
        self.clients = []
        self.sent = []                        # set_azureadobject payloads
        self.hashes = []                      # (sourceanchor, hashnt)

    # factory handed to libsync.AADInternals
    def new_client(self, proxies=None, tenant_id=None, entra_aba_dir=None, domain=None, verify=True):
        client = FakeEntraClient(self, len(self.clients))
        self.clients.append(client)
        return client

    def names(self, *methods):
        return [c[1] for c in self.calls if not methods or c[1] in methods]

    def count(self, method):
        return len([c for c in self.calls if c[1] == method])

    def wrote_anything(self):
        writes = {"set_adsyncenabled", "set_sync_features", "set_azureadobject", "set_userpassword",
                  "remove_azureadoject", "set_desktop_sso", "set_desktop_sso_enabled"}
        return any(c[1] in writes for c in self.calls)


class FakeEntraClient:
    def __init__(self, tenant, index):
        self.tenant = tenant
        self.index = index
        self.tenant_id = tenant.tenant_id

    # -- plumbing
    def _answer(self):
        script = self.tenant.permissions
        return script[self.index] if self.index < len(script) else self.tenant.default_permissions

    def _record(self, method, detail=None):
        self.tenant.calls.append((self.index, method, detail))
        hook = self.tenant.fail.get(method)
        if hook is not None:
            error = hook(detail) if callable(hook) else hook
            if error is not None:
                raise error

    def _gate(self, service):
        status = self._answer().get(service)
        if status == "denied":
            raise Exception(DENIED)
        if status is not None:
            raise GraphError(status)

    # -- reads
    def get_token(self, scopes=None):
        self.tenant.calls.append((self.index, "get_token", scopes))

    def get_syncconfiguration(self):
        self._record("get_syncconfiguration")
        self._gate("sync")
        return {"IsDirSyncing": self.tenant.is_dir_syncing, "DirSyncFeatures": self.tenant.dir_sync_features}

    def call_graphapi(self, what, **kwargs):
        self._record("call_graphapi", what)
        self._gate(what)
        return {"value": []}

    def list_users(self, **kwargs):
        self._record("list_users")
        self._gate("users")
        yield from self.tenant.cloud_users

    def list_groups(self, **kwargs):
        self._record("list_groups")
        self._gate("groups")
        yield from self.tenant.cloud_groups

    def get_dict_cloudanchor_sourceanchor(self):
        self._record("get_dict_cloudanchor_sourceanchor")
        return {}

    # -- writes
    def set_adsyncenabled(self, enabledirsync=True):
        self._record("set_adsyncenabled", enabledirsync)
        self.tenant.is_dir_syncing = bool(enabledirsync)

    def set_sync_features(self, enable_features=None):
        self._record("set_sync_features", enable_features)
        if "PasswordHashSync" in (enable_features or []):
            self.tenant.dir_sync_features |= 1

    def set_azureadobject(self, **entry):
        self._record("set_azureadobject", entry.get("SourceAnchor"))
        self.tenant.sent.append(entry)

    def set_userpassword(self, hashnt=None, sourceanchor=None):
        self._record("set_userpassword", sourceanchor)
        self.tenant.hashes.append((sourceanchor, hashnt))

    def remove_azureadoject(self, sourceanchor=None, objecttype=None):
        self._record("remove_azureadoject", (sourceanchor, objecttype))

    def set_desktop_sso_enabled(self, enable=True):
        self._record("set_desktop_sso_enabled", enable)

    def set_desktop_sso(self, domain_name, password, computer_name, enable):
        self._record("set_desktop_sso", domain_name)


class FakeSamba:
    """Replaces libsync.SambaInfo. The test fills in the directory content."""

    users = {}
    groups = {}
    devices = {}
    hashes = {}
    sso_exists = True
    sso_expired = False
    scp_exists = True
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.domaine = "example.test"
        self.samdb_loc = object()
        self.dict_all_users_samba = {}
        self.dict_all_group_samba = {}
        self.dict_all_device_samba = {}
        self.dict_id_hash = {}
        self.calls = []
        FakeSamba.last = self

    def generate_all_dict(self):
        self.dict_all_users_samba = {k: dict(v) for k, v in FakeSamba.users.items()}
        self.dict_all_group_samba = {k: dict(v) for k, v in FakeSamba.groups.items()}
        self.dict_all_device_samba = {k: dict(v) for k, v in FakeSamba.devices.items()}
        self.dict_id_hash = dict(FakeSamba.hashes)

    def azure_ad_sso_user_exist(self):
        return FakeSamba.sso_exists

    def azure_ad_sso_user_expire(self):
        return FakeSamba.sso_expired

    def create_azureadssoacc(self, computername="azureadssoacc"):
        self.calls.append("create_azureadssoacc")

    def set_password_azureadssoacc(self, password=None, computername="azureadssoacc"):
        self.calls.append("set_password_azureadssoacc")

    def check_service_connection_point_existe(self):
        return FakeSamba.scp_exists

    def write_service_connection_point(self, tenant_id, azureadname):
        self.calls.append("write_service_connection_point")

    @classmethod
    def reset(cls):
        cls.users, cls.groups, cls.devices, cls.hashes = {}, {}, {}, {}
        cls.sso_exists, cls.sso_expired, cls.scp_exists, cls.last = True, False, True, None


def user(anchor, name=None):
    name = name or anchor
    return {"SourceAnchor": anchor, "onPremisesSamAccountName": name, "usertype": "User", "displayName": name}


def group(anchor, members=()):
    return {"SourceAnchor": anchor, "onPremisesSamAccountName": anchor, "usertype": "Group",
            "groupMembers": list(members)}


# -- identity directory (what AADInternals_python keeps in the --entra-aba-dir)

def write_identity(directory, age_minutes=1, status="ready", tenant_id="tenant-1", pfx=True, created_at=True):
    """Create state.json (+ current.pfx) the way the certificate manager leaves them."""
    os.makedirs(directory, exist_ok=True)
    state = {"status": status, "tenant_id": tenant_id}
    if created_at:
        created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=age_minutes)
        state["created_at"] = created.isoformat().replace("+00:00", "Z")
    with open(os.path.join(directory, "state.json"), "w", encoding="utf-8") as stream:
        json.dump(state, stream)
    if pfx:
        with open(os.path.join(directory, "current.pfx"), "wb") as stream:
            stream.write(b"pfx")


def read_state(base):
    """Same contract as _aad_certificate_manager.read_state: dict, or None when there is none."""
    try:
        with open(os.path.join(str(base), "state.json"), encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None
