from .models import AuditEvent


def _ip(request):
    if request is None:
        return None
    return request.META.get("REMOTE_ADDR")


def audit(actor, action, record=None, changes=None, result="OK", request=None, record_type=None):
    AuditEvent.objects.create(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        record_type=record_type or (type(record).__name__ if record is not None else ""),
        record_id=str(getattr(record, "pk", "") or ""),
        result=result,
        changes=changes or {},
        ip_address=_ip(request),
    )


def diff(before, after):
    return {k: {"from": before.get(k), "to": after.get(k)} for k in after if before.get(k) != after.get(k)}
