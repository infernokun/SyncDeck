class SyncDeckError(Exception):
    """Base class for errors that are safe to show the user in the QAM."""

    code = "error"

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class SyncthingNotFound(SyncDeckError):
    """Syncthing's config.xml could not be located on disk."""

    code = "syncthing_not_found"


class SyncthingUnreachable(SyncDeckError):
    """Syncthing's config was found but the daemon is not answering."""

    code = "syncthing_unreachable"


class SyncthingAuthError(SyncDeckError):
    """The API key was rejected."""

    code = "syncthing_auth"


class SyncthingApiError(SyncDeckError):
    """Syncthing returned a non-2xx response."""

    code = "syncthing_api"

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "status": self.status, "body": self.body[:500]}


class SavePathError(SyncDeckError):
    """A save path is missing, unresolvable, or unsafe to sync."""

    code = "save_path"
