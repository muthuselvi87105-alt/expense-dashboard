"""Private file storage. Files live under PRIVATE_MEDIA_ROOT and are only
served through views that check access."""
import uuid
from pathlib import Path

from django.conf import settings


def _root():
    root = Path(settings.PRIVATE_MEDIA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_private(folder, original_name, content):
    suffix = Path(original_name).suffix.lower()[:10]
    key = f"{folder}/{uuid.uuid4().hex}{suffix}"
    path = _root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return key


def open_private(key):
    path = (_root() / key).resolve()
    if _root().resolve() not in path.parents:
        raise FileNotFoundError(key)
    return path.open("rb")
