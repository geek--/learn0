from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin"
        SECURITY_ANALYST = "security_analyst", "Security analyst"
        VIEWER = "viewer", "Viewer"

    role = models.CharField(max_length=32, choices=Role.choices, default=Role.VIEWER)

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN

    @property
    def is_security_analyst(self) -> bool:
        return self.role == self.Role.SECURITY_ANALYST

    @property
    def is_viewer(self) -> bool:
        return self.role == self.Role.VIEWER
