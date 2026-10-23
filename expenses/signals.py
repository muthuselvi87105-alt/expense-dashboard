from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from .audit import audit


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    audit(user, "auth.login", user, request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    if user:
        audit(user, "auth.logout", user, request=request)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    audit(None, "auth.login_failed", None, {"username": credentials.get("username", "")}, result="FAILED",
          request=request, record_type="User")
