
import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from expenses.models import Role, RoleAssignment


class Command(BaseCommand):
    help = "Create admin user and assign application super-admin role"

    def handle(self, *args, **kwargs):
        User = get_user_model()

        username = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")

        if not username or not password:
            self.stdout.write(
                self.style.ERROR("Admin environment variables are missing.")
            )
            return

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )

        if created:
            user.set_password(password)
            user.save()
        else:
            changed = False

            if not user.is_staff:
                user.is_staff = True
                changed = True

            if not user.is_superuser:
                user.is_superuser = True
                changed = True

            if not user.is_active:
                user.is_active = True
                changed = True

            if changed:
                user.save()

        assignment, role_created = RoleAssignment.objects.get_or_create(
            user=user,
            role=Role.SUPER_ADMIN,
            entity=None,
        )

        if role_created:
            self.stdout.write("SUPER_ADMIN role assigned.")
        else:
            self.stdout.write("SUPER_ADMIN role already exists.")

        self.stdout.write(
            self.style.SUCCESS("Admin user verified successfully.")
        )
