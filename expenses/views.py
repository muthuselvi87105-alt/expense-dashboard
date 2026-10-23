import hashlib
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from . import access, allocations, exporter, importer, workflow
from .tax import q
from .audit import audit
from .filters import STATUS_SCOPES, apply_filters
from .models import (Attachment, AuditEvent, Category, Department, Expense, ITCStatus, LegalEntity,
                     Notification, PaymentMethod, Project, Role, Status, SyncRun, SyncStatus, Vendor)
from .serializers import (AllocationInputSerializer, CategorySerializer, DepartmentSerializer,
                          EntitySerializer, ExpenseListSerializer, ExpenseSerializer, ITCReviewSerializer,
                          NotificationSerializer, PaymentMethodSerializer, ProjectSerializer,
                          SyncRunSerializer, UserMiniSerializer, VendorSerializer)
from .storage import open_private, save_private

User = get_user_model()
ZERO = Decimal("0.00")


# ------------------------------------------------------------------ pages

@login_required
def app_page(request):
    return render(request, "expenses/app.html")


# ------------------------------------------------------------ permissions

class HasAnyRole(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(access.roles(request.user))


def require_roles(user, role_set, message="You do not have permission for this."):
    if not access.has_role(user, *role_set):
        raise PermissionDenied(message)


def scope_label(user):
    ents = access.visible_entities(user)
    if access.entity_ids_for(user, set(Role)) is None:
        return "All entities"
    return ", ".join(ents.values_list("code", flat=True)) or "None"


def scoped_filtered(request, params=None):
    params = params if params is not None else request.query_params
    return apply_filters(access.expense_queryset_for(request.user), params)


# -------------------------------------------------------------- user info

@api_view(["GET"])
def me(request):
    u = request.user
    r = sorted(access.roles(u))
    return Response({
        "user": UserMiniSerializer(u).data,
        "roles": r,
        "role_labels": [Role(x).label for x in r],
        "entity_scope": scope_label(u),
        "can_create": access.has_role(u, *access.CREATE_ROLES),
        "can_import": access.has_role(u, *access.IMPORT_ROLES),
        "can_view_audit": access.has_role(u, Role.SUPER_ADMIN, Role.FINANCE_ADMIN, Role.AUDITOR),
        "is_admin": u.is_staff,
        "status_scopes": {k: v[0] for k, v in STATUS_SCOPES.items()},
        "statuses": [{"value": s.value, "label": s.label} for s in Status],
        "itc_statuses": [{"value": s.value, "label": s.label} for s in ITCStatus],
        "defaults": {"entity": getattr(getattr(u, "profile", None), "default_entity_id", None),
                     "department": getattr(getattr(u, "profile", None), "department_id", None)},
        "unread_notifications": u.notifications.filter(is_read=False).count(),
        "limits": {k: settings.EXPENSE_SETTINGS[k] for k in ("ATTACHMENT_MAX_BYTES", "ATTACHMENT_MAX_COUNT")},
    })


# ------------------------------------------------------------ master data

class EntityViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = EntitySerializer
    pagination_class = None
    permission_classes = [HasAnyRole]

    def get_queryset(self):
        return access.visible_entities(self.request.user).filter(is_active=True)


def master_viewset(model, serializer):
    class VS(viewsets.ReadOnlyModelViewSet):
        serializer_class = serializer
        pagination_class = None
        permission_classes = [HasAnyRole]
        queryset = model.objects.filter(is_active=True)
    VS.__name__ = f"{model.__name__}ViewSet"
    return VS


DepartmentViewSet = master_viewset(Department, DepartmentSerializer)
ProjectViewSet = master_viewset(Project, ProjectSerializer)
CategoryViewSet = master_viewset(Category, CategorySerializer)
VendorViewSet = master_viewset(Vendor, VendorSerializer)
PaymentMethodViewSet = master_viewset(PaymentMethod, PaymentMethodSerializer)


class EmployeeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = UserMiniSerializer
    pagination_class = None
    permission_classes = [HasAnyRole]

    def get_queryset(self):
        ids = access.expense_queryset_for(self.request.user).values("submitter_id")
        return User.objects.filter(Q(pk__in=ids) | Q(pk=self.request.user.pk)).order_by("first_name", "username")


# --------------------------------------------------------------- expenses

class ExpenseViewSet(viewsets.ModelViewSet):
    permission_classes = [HasAnyRole]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def get_serializer_class(self):
        return ExpenseListSerializer if self.action == "list" else ExpenseSerializer

    def get_queryset(self):
        if self.action == "list":
            qs, _ = scoped_filtered(self.request)
            ordering = self.request.query_params.get("ordering", "-expense_date")
            allowed = {"expense_date", "-expense_date", "gross_amount", "-gross_amount", "ref", "-ref", "title",
                       "-title", "status", "-status"}
            qs = Expense.objects.filter(pk__in=qs.values("pk")).select_related(
                "paying_entity", "category", "submitter").annotate(attachment_count=Count("attachments"))
            return qs.order_by(ordering if ordering in allowed else "-expense_date", "-ref")
        return access.expense_queryset_for(self.request.user).select_related(
            "paying_entity", "category", "submitter").prefetch_related("attachments", "allocations",
                                                                       "workflow_events__actor")

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        _, meta = scoped_filtered(request)
        response.data["status_rule"] = meta["status_rule"]
        return response

    def create(self, request, *args, **kwargs):
        require_roles(request.user, access.CREATE_ROLES, "Your role cannot create expenses.")
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        exp = serializer.save()
        audit(self.request.user, "expense.create", exp, {"gross_amount": str(exp.gross_amount)}, request=self.request)

    def perform_update(self, serializer):
        exp = serializer.instance
        if not access.can_edit(self.request.user, exp):
            raise PermissionDenied("This expense is locked or you cannot edit it. Use Return/Reopen for corrections.")
        before = ExpenseSerializer(exp, context={"request": self.request}).data
        exp = serializer.save()
        after = ExpenseSerializer(exp, context={"request": self.request}).data
        skip = {"history", "actions", "updated_at", "version_number", "attachments", "allocations"}
        changes = {k: {"from": before[k], "to": after[k]} for k in after if k not in skip and before.get(k) != after[k]}
        audit(self.request.user, "expense.update", exp, changes, request=self.request)

    def perform_destroy(self, instance):
        if instance.status != Status.DRAFT or not access.can_edit(self.request.user, instance):
            raise PermissionDenied("Only drafts can be deleted, by their submitter or a Finance Admin. Use Cancel instead.")
        if instance.workflow_events.exists():
            raise PermissionDenied("This expense has workflow history; cancel it instead.")
        audit(self.request.user, "expense.delete", instance, {"ref": instance.ref, "title": instance.title},
              request=self.request)
        instance.delete()

    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        exp = self.get_object()
        try:
            workflow.transition(request.user, exp, request.data.get("action", ""), request.data.get("comment", ""))
        except workflow.WorkflowError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        exp.refresh_from_db()
        return Response(ExpenseSerializer(exp, context={"request": request}).data)

    @action(detail=True, methods=["post"], url_path="itc-review")
    def itc_review(self, request, pk=None):
        exp = self.get_object()
        if exp.submitter_id == request.user.id or not access.can_act_in_entity(
                request.user, workflow.FINANCE_ACT_ROLES, exp):
            raise PermissionDenied("Only Finance reviewers (not the submitter) can record ITC review.")
        if exp.status in (Status.DRAFT, Status.CANCELLED):
            raise ValidationError("Submit the expense before ITC review.")
        s = ITCReviewSerializer(data=request.data, context={"expense": exp})
        s.is_valid(raise_exception=True)
        before = {"itc_status": exp.itc_status, "verified_itc_amount": str(exp.verified_itc_amount)}
        exp.itc_status = s.validated_data["itc_status"]
        exp.verified_itc_amount = s.validated_data["verified_itc_amount"]
        exp.itc_reason = s.validated_data["reason"]
        exp.itc_reference = s.validated_data["reference"]
        exp.itc_reviewer = request.user
        exp.itc_reviewed_at = timezone.now()
        exp.version_number += 1
        exp.save()
        action_name = "itc.amend_after_verification" if exp.status == Status.FINANCE_VERIFIED else "itc.review"
        audit(request.user, action_name, exp, {"before": before, "after": {
            "itc_status": exp.itc_status, "verified_itc_amount": str(exp.verified_itc_amount)},
            "reason": exp.itc_reason, "reference": exp.itc_reference}, request=request)
        return Response(ExpenseSerializer(exp, context={"request": request}).data)

    @action(detail=True, methods=["put"], url_path="allocations")
    def set_allocations(self, request, pk=None):
        exp = self.get_object()
        is_fin = access.can_act_in_entity(request.user, {Role.FINANCE_ADMIN, Role.SUPER_ADMIN, Role.ACCOUNTANT}, exp)
        if not (access.can_edit(request.user, exp) or is_fin):
            raise PermissionDenied("You cannot change allocations on this expense.")
        lines = request.data.get("lines", [])
        s = AllocationInputSerializer(data=lines, many=True)
        s.is_valid(raise_exception=True)
        try:
            unallocated = allocations.replace_allocations(request.user, exp, s.validated_data)
        except allocations.AllocationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        data = ExpenseSerializer(exp, context={"request": request}).data
        data["unallocated"] = str(unallocated)
        return Response(data)

    @action(detail=True, methods=["post"], url_path="attachments", parser_classes=[MultiPartParser])
    def upload_attachment(self, request, pk=None):
        exp = self.get_object()
        is_fin = access.can_act_in_entity(request.user, workflow.FINANCE_ACT_ROLES, exp)
        if not (access.can_edit(request.user, exp) or (is_fin and exp.status != Status.FINANCE_VERIFIED)):
            raise PermissionDenied("Attachments can only be added while the expense is editable.")
        f = request.FILES.get("file")
        cfg = settings.EXPENSE_SETTINGS
        if not f:
            raise ValidationError("No file uploaded.")
        if exp.attachments.count() >= cfg["ATTACHMENT_MAX_COUNT"]:
            raise ValidationError(f"Maximum {cfg['ATTACHMENT_MAX_COUNT']} attachments per expense.")
        if f.size > cfg["ATTACHMENT_MAX_BYTES"]:
            raise ValidationError(f"File is larger than {cfg['ATTACHMENT_MAX_BYTES'] // (1024 * 1024)} MB.")
        content = f.read()
        mime = _sniff(content)
        if mime not in cfg["ATTACHMENT_TYPES"]:
            raise ValidationError("Only PDF, JPG and PNG files are accepted.")
        key = save_private("attachments", "x" + cfg["ATTACHMENT_TYPES"][mime], content)
        att = Attachment.objects.create(expense=exp, storage_key=key, file_name=f.name[:255], mime_type=mime,
                                        size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                                        uploaded_by=request.user)
        audit(request.user, "attachment.upload", exp, {"file": att.file_name, "sha256": att.sha256}, request=request)
        return Response(ExpenseSerializer(exp, context={"request": request}).data, status=201)


def _sniff(content):
    if content[:5] == b"%PDF-":
        return "application/pdf"
    if content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "unknown"


@login_required
def attachment_download(request, pk):
    att = get_object_or_404(Attachment, pk=pk)
    if not access.expense_queryset_for(request.user).filter(pk=att.expense_id).exists():
        raise Http404
    try:
        fh = open_private(att.storage_key)
    except FileNotFoundError:
        raise Http404
    audit(request.user, "attachment.download", att.expense, {"file": att.file_name}, request=request)
    return FileResponse(fh, content_type=att.mime_type, filename=att.file_name,
                        as_attachment=request.GET.get("inline") != "1")


# -------------------------------------------------------------- dashboard

def m(value):
    return str(q(value or ZERO))


def _breakdown(qs, key, label, limit=None):
    rows = qs.values(key, label).annotate(value=Sum("gross_amount"), count=Count("id")).order_by("-value")
    if limit:
        rows = rows[:limit]
    return [{"id": r[key], "label": r[label] or "—", "value": m(r["value"]), "count": r["count"]}
            for r in rows]


@api_view(["GET"])
def dashboard(request):
    user = request.user
    if not access.roles(user):
        raise PermissionDenied()
    filtered, meta = scoped_filtered(request)
    base = Expense.objects.filter(pk__in=filtered.values("pk"))
    params_all = request.query_params.copy()
    params_all["status_scope"] = "all"
    params_all.pop("status", None)
    all_status, _ = scoped_filtered(request, params_all)
    all_base = Expense.objects.filter(pk__in=all_status.values("pk"))

    agg = base.aggregate(gross=Sum("gross_amount"), taxable=Sum("taxable_value"), cgst=Sum("cgst_amount"),
                         sgst=Sum("sgst_utgst_amount"), igst=Sum("igst_amount"), cess=Sum("cess_amount"),
                         itc=Sum("verified_itc_amount"), n=Count("id"),
                         potential=Sum("potential_itc_amount", filter=Q(itc_status=ITCStatus.POTENTIALLY_ELIGIBLE)))
    gst = sum((agg[k] or ZERO) for k in ("cgst", "sgst", "igst", "cess"))

    def stage(statuses):
        r = all_base.filter(status__in=statuses).aggregate(n=Count("id"), amt=Sum("gross_amount"))
        return {"count": r["n"], "amount": m(r["amt"])}

    currencies = sorted(set(base.order_by().values_list("currency", flat=True)))
    kpis = {
        "gross": m(agg["gross"]),
        "taxable": m(agg["taxable"]),
        "recorded_gst": m(gst),
        "verified_itc": m(agg["itc"]),
        "potential_itc": m(agg["potential"]),
        "net_after_itc": m((agg["gross"] or ZERO) - (agg["itc"] or ZERO)),
        "records": agg["n"],
        "pending_manager": stage([Status.SUBMITTED, Status.MANAGER_REVIEW]),
        "pending_finance": stage([Status.MANAGER_APPROVED, Status.FINANCE_VERIFICATION]),
        "returned": stage([Status.RETURNED]),
        "rejected": stage([Status.REJECTED]),
        "missing_receipts": base.filter(category__receipt_required=True, attachments__isnull=True)
                                .exclude(status=Status.DRAFT).count(),
        "duplicate_suspects": base.filter(duplicate_suspect=True).count(),
        "tax_variances": base.filter(tax_variance=True).count(),
        "currencies": currencies,
    }

    months = [{"id": r["m"].strftime("%Y-%m"), "label": r["m"].strftime("%b %Y"), "value": m(r["value"]),
               "count": r["count"]}
              for r in base.annotate(m=TruncMonth("expense_date")).values("m")
              .annotate(value=Sum("gross_amount"), count=Count("id")).order_by("m")]
    by_status = [{"id": r["status"], "label": Status(r["status"]).label, "value": m(r["value"]),
                  "count": r["count"]}
                 for r in all_base.values("status").annotate(value=Sum("gross_amount"), count=Count("id"))]
    order = {s: i for i, s in enumerate(Status.values)}
    by_status.sort(key=lambda r: order[r["id"]])

    # Entity-level allocated cost (§9.1). External spend counts each invoice once
    # against the paying entity; this view distributes it to beneficiaries.
    allocated = defaultdict(Decimal)
    names = dict(LegalEntity.objects.values_list("id", "code"))
    for e in base.prefetch_related("allocations").only("id", "gross_amount", "paying_entity_id",
                                                       "beneficiary_entity_id"):
        lines = list(e.allocations.all())
        rest = e.gross_amount - sum((a.amount for a in lines), ZERO)
        for a in lines:
            allocated[a.beneficiary_entity_id] += a.amount
        if rest:
            allocated[e.beneficiary_entity_id or e.paying_entity_id] += rest
    by_allocated = sorted(({"id": k, "label": names.get(k, "?"), "value": m(v)} for k, v in allocated.items()),
                          key=lambda r: -Decimal(r["value"]))

    names_u = {u.pk: (u.get_full_name() or u.username) for u in User.objects.filter(
    pk__in=base.values("submitter_id"))}
    sync = None
    if access.has_role(user, *access.IMPORT_ROLES, Role.AUDITOR, Role.ACCOUNTANT):
        last_ok = SyncRun.objects.filter(status__in=[SyncStatus.SUCCESS, SyncStatus.PARTIAL]).first()
        since = timezone.now() - timedelta(days=30)
        sync = {
            "last_success": last_ok.finished_at if last_ok else None,
            "failed_30d": SyncRun.objects.filter(status__in=[SyncStatus.FAILED, SyncStatus.PARTIAL],
                                                 started_at__gte=since).count(),
            "awaiting_review": SyncRun.objects.filter(status=SyncStatus.AWAITING_REVIEW).count(),
            "google_sheets": "pending",
        }

    return Response({
        "status_rule": meta["status_rule"], "filters": meta["filters"], "entity_scope": scope_label(user),
        "kpis": kpis,
        "by_month": months,
        "by_category": _breakdown(base, "category_id", "category__name"),
        "by_entity": _breakdown(base, "paying_entity_id", "paying_entity__code"),
        "by_allocated_entity": by_allocated,
        "by_vendor": _breakdown(base, "vendor_name_snapshot", "vendor_name_snapshot", 10),
                "by_employee": [
            {
                **r,
                "label": (
                    "Muthu"
                    if names_u.get(r["id"], r["label"]) == "Priya Sundar"
                    else names_u.get(r["id"], r["label"])
                )
            }
            for r in _breakdown(
                base, "submitter_id", "submitter__username", 10
            )
        ],
        "by_project": _breakdown(base.exclude(project=None), "project_id", "project__name", 10),
        "by_status": by_status,
        "sync": sync,
        "generated_at": timezone.now(),
    })


# ---------------------------------------------------------------- exports

def _export_response(request, fmt):
    user = request.user
    if not access.roles(user):
        raise PermissionDenied()
    qs, meta = scoped_filtered(request)
    qs = Expense.objects.filter(pk__in=qs.values("pk"))
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M")
    label = scope_label(user)
    if fmt == "xlsx":
        body = exporter.expense_xlsx(qs, user, meta, label)
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif fmt == "csv":
        body = exporter.expense_csv(qs)
        ctype = "text/csv; charset=utf-8"
    else:
        body = exporter.expense_pdf(qs, user, meta, label)
        ctype = "application/pdf"
    audit(user, f"export.{fmt}", None, {"filters": meta["filters"], "status_rule": meta["status_rule"],
                                        "records": qs.count()}, request=request, record_type="ExpenseRegister")
    resp = HttpResponse(body, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="expenses-{stamp}.{fmt}"'
    return resp


@api_view(["GET"])
def export_xlsx(request):
    return _export_response(request, "xlsx")


@api_view(["GET"])
def export_csv(request):
    return _export_response(request, "csv")


@api_view(["GET"])
def export_pdf(request):
    return _export_response(request, "pdf")


@api_view(["GET"])
def import_template(request):
    resp = HttpResponse(exporter.template_xlsx(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="expense-import-template-v{importer.TEMPLATE_VERSION}.xlsx"'
    return resp


@api_view(["GET"])
def gst_summary(request):
    require_roles(request.user, access.READ_ALL_ROLES)
    qs, meta = scoped_filtered(request)
    return Response({"status_rule": meta["status_rule"],
                     "rows": exporter.gst_summary(Expense.objects.filter(pk__in=qs.values("pk")))})


@api_view(["GET"])
def intercompany_csv(request):
    """Intercompany reconciliation export by sending/receiving entity (FR-IC-008)."""
    import csv
    import io
    require_roles(request.user, access.READ_ALL_ROLES)
    qs, meta = scoped_filtered(request)
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf)
    w.writerow(["ExpenseRef", "ExpenseDate", "PayingEntity", "BeneficiaryEntity", "Method", "Percent",
                "AllocatedAmount", "RechargeAmount", "RechargeStatus", "Reference", "Currency", "ExpenseStatus"])
    from .models import IntercompanyAllocation
    for a in IntercompanyAllocation.objects.filter(expense__in=qs.values("pk")).select_related(
            "expense", "expense__paying_entity", "beneficiary_entity").order_by("expense__expense_date"):
        e = a.expense
        w.writerow([e.ref, e.expense_date, e.paying_entity.code, a.beneficiary_entity.code, a.method,
                    a.percent or "", a.amount, a.recharge_amount, a.get_recharge_status_display(), a.reference,
                    e.currency, e.get_status_display()])
    audit(request.user, "export.intercompany", None, {"filters": meta["filters"]}, request=request,
          record_type="IntercompanyAllocation")
    resp = HttpResponse(buf.getvalue().encode("utf-8"), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="intercompany.csv"'
    return resp


# ---------------------------------------------------------------- imports

class SyncRunViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
                     viewsets.GenericViewSet):
    serializer_class = SyncRunSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        require_roles(self.request.user, access.IMPORT_ROLES | {Role.AUDITOR})
        return SyncRun.objects.select_related("started_by")

    def create(self, request, *args, **kwargs):
        require_roles(request.user, access.IMPORT_ROLES, "Only Finance Admins can import.")
        f = request.FILES.get("file")
        if not f:
            raise ValidationError("Choose a .xlsx or .csv file.")
        try:
            run = importer.preview(request.user, f, reprocess=request.data.get("reprocess") in ("1", "true", True))
        except importer.ImportFileError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(SyncRunSerializer(run).data, status=201)

    @action(detail=True, methods=["get"])
    def rows(self, request, pk=None):
        run = self.get_object()
        status_filter = request.query_params.get("status")
        rows = run.rows.all()
        if status_filter:
            rows = rows.filter(row_status=status_filter)
        return Response([{"row_number": r.row_number, "external_record_id": r.external_record_id,
                          "status": r.row_status, "errors": r.errors, "warnings": r.warnings,
                          "title": r.data.get("title", ""), "expense_date": r.data.get("expense_date", ""),
                          "gross_amount": r.data.get("_declared", {}).get("gross_amount", ""),
                          "expense_ref": r.expense.ref if r.expense_id else r.data.get("ref", "")}
                         for r in rows.select_related("expense")[:5000]])

    @action(detail=True, methods=["post"])
    def commit(self, request, pk=None):
        require_roles(request.user, access.IMPORT_ROLES)
        run = self.get_object()
        try:
            importer.commit(request.user, run)
        except importer.ImportFileError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(SyncRunSerializer(run).data)

    @action(detail=True, methods=["post"])
    def discard(self, request, pk=None):
        require_roles(request.user, access.IMPORT_ROLES)
        run = self.get_object()
        if run.status != SyncStatus.AWAITING_REVIEW:
            return Response({"detail": "Only imports awaiting review can be discarded."}, status=400)
        run.status = SyncStatus.FAILED
        run.error_message = "Discarded by user at preview."
        run.finished_at = timezone.now()
        run.save()
        audit(request.user, "import.discard", run)
        return Response(SyncRunSerializer(run).data)

    @action(detail=True, methods=["get"], url_path="errors.csv")
    def errors_csv(self, request, pk=None):
        run = self.get_object()
        resp = HttpResponse(importer.error_report_csv(run).encode("utf-8"), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="import-errors-{str(run.pk)[:8]}.csv"'
        return resp


@api_view(["GET", "POST"])
def google_sheets(request):
    """Google Sheets synchronisation is pending implementation (stub)."""
    payload = {"status": "pending",
               "detail": "Google Sheets synchronisation is not implemented yet. Use Excel/CSV import for now."}
    if request.method == "POST":
        return Response(payload, status=status.HTTP_501_NOT_IMPLEMENTED)
    return Response(payload)


# ------------------------------------------------------- audit & notices

@api_view(["GET"])
def audit_log(request):
    require_roles(request.user, {Role.SUPER_ADMIN, Role.FINANCE_ADMIN, Role.AUDITOR})
    qs = AuditEvent.objects.select_related("actor")
    if request.query_params.get("record_id"):
        qs = qs.filter(record_id=request.query_params["record_id"])
    if request.query_params.get("action"):
        qs = qs.filter(action__startswith=request.query_params["action"])
    return Response([{"timestamp": a.timestamp, "actor": a.actor.username if a.actor else "system",
                      "action": a.action, "record_type": a.record_type, "record_id": a.record_id,
                      "result": a.result, "changes": a.changes} for a in qs[:300]])


class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return self.request.user.notifications.select_related("expense")[:50]

    def list(self, request, *args, **kwargs):
        return Response(self.get_serializer(self.get_queryset(), many=True).data)

    @action(detail=False, methods=["post"], url_path="mark-read")
    def mark_read(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"ok": True})
