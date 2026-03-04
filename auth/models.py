from dataclasses import dataclass


@dataclass
class CurrentUser:
    """The authenticated user for the current request."""

    id: str
    email: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
