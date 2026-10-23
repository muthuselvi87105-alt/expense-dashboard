"""One filter implementation shared by the list API, dashboard and exports (FR-RPT-004)."""
from datetime import date

from django.db.models import F, Q

from .models import Status

STATUS_SCOPES = {
    "active": ("All records except rejected and cancelled (includes drafts)",
               [s for s in Status.values if s not in (Status.REJECTED, Status.CANCELLED)]),
    "submitted": ("Submitted and later (excludes drafts, returned, rejected, cancelled)",
                  [Status.SUBMITTED, Status.MANAGER_REVIEW, Status.MANAGER_APPROVED,
                   Status.FINANCE_VERIFICATION, Status.FINANCE_VERIFIED]),
    "manager_approved": ("Manager approved or finance verified",
                         [Status.MANAGER_APPROVED, Status.FINANCE_VERIFICATION, Status.FINANCE_VERIFIED]),
    "finance_verified": ("Finance verified only", [Status.FINANCE_VERIFIED]),
    "all": ("Every record, including drafts, rejected and cancelled", list(Status.values)),
}
DEFAULT_SCOPE = "active"

FK_FILTERS = {
    "entity": None,  # special: paying or beneficiary
    "department": "department_id",
    "project": "project_id",
    "category": "category_id",
    "vendor": "vendor_id",
    "employee": "submitter_id",
    "payment_method": "payment_method_id",
}


def _ids(value):
    return [v for v in str(value).split(",") if v.strip()]


def _date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def apply_filters(qs, params):
    """Returns (queryset, description dict) for report headers."""
    applied = {}
    scope = params.get("status_scope") or DEFAULT_SCOPE
    if scope not in STATUS_SCOPES:
        scope = DEFAULT_SCOPE
    statuses = _ids(params.get("status", ""))
    if statuses:
        qs = qs.filter(status__in=statuses)
        applied["status"] = ", ".join(statuses)
        scope_label = "Explicit status filter: " + ", ".join(Status(s).label for s in statuses if s in Status.values)
    else:
        label, allowed = STATUS_SCOPES[scope]
        qs = qs.filter(status__in=allowed)
        scope_label = label

    d_from, d_to = _date(params.get("date_from")), _date(params.get("date_to"))
    if d_from:
        qs = qs.filter(expense_date__gte=d_from)
        applied["date_from"] = d_from.isoformat()
    if d_to:
        qs = qs.filter(expense_date__lte=d_to)
        applied["date_to"] = d_to.isoformat()

    for key, field in FK_FILTERS.items():
        values = _ids(params.get(key, ""))
        if not values:
            continue
        applied[key] = ",".join(values)
        if key == "entity":
            qs = qs.filter(Q(paying_entity_id__in=values) | Q(beneficiary_entity_id__in=values))
        else:
            qs = qs.filter(**{f"{field}__in": values})

    for key in ("gst_type", "itc_status", "source_type", "currency"):
        if params.get(key):
            qs = qs.filter(**{key: params[key]})
            applied[key] = params[key]
    if params.get("duplicate") in ("1", "true"):
        qs = qs.filter(duplicate_suspect=True)
        applied["duplicate"] = "yes"
    if params.get("tax_variance") in ("1", "true"):
        qs = qs.filter(tax_variance=True)
        applied["tax_variance"] = "yes"
    if params.get("missing_receipt") in ("1", "true"):
        qs = qs.filter(attachments__isnull=True)
        applied["missing_receipt"] = "yes"
    if params.get("intercompany") in ("1", "true"):
        qs = qs.filter(Q(allocations__isnull=False) | (Q(beneficiary_entity__isnull=False) &
                                                         ~Q(beneficiary_entity_id=F("paying_entity_id"))))
        applied["intercompany"] = "yes"
    if params.get("month"):
        try:
            y, m = (int(x) for x in params["month"].split("-"))
            qs = qs.filter(expense_date__year=y, expense_date__month=m)
            applied["month"] = params["month"]
        except ValueError:
            pass
    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(Q(title__icontains=search) | Q(description__icontains=search) | Q(notes__icontains=search) |
                       Q(ref__icontains=search) | Q(invoice_number__icontains=search) |
                       Q(vendor_name_snapshot__icontains=search))
        applied["search"] = search
    return qs.distinct(), {"filters": applied, "status_scope": scope if not statuses else "custom",
                           "status_rule": scope_label}
