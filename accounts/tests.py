from django.test import TestCase

from accounts.models import User


class UserRoleTests(TestCase):
    def test_role_helpers(self) -> None:
        admin = User.objects.create_user(username="admin", password="pass", role=User.Role.ADMIN)
        analyst = User.objects.create_user(
            username="analyst", password="pass", role=User.Role.SECURITY_ANALYST
        )
        viewer = User.objects.create_user(username="viewer", password="pass", role=User.Role.VIEWER)

        self.assertTrue(admin.is_admin)
        self.assertFalse(admin.is_security_analyst)
        self.assertFalse(admin.is_viewer)

        self.assertTrue(analyst.is_security_analyst)
        self.assertFalse(analyst.is_admin)

        self.assertTrue(viewer.is_viewer)
        self.assertFalse(viewer.is_admin)
