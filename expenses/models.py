import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

ZERO = Decimal("0.00")


def money(**kwargs):
    return models.DecimalField(max_digits=18, decimal_places=2, default=ZERO, **kwargs)


# --------------------------------------------------------------------------
# Master data. Referenced rows are PROTECTed: deactivate instead of delete.
# --------------------------------------------------------------------------

class MasterData(models.Model):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} – {self.name}"


class LegalEntity(MasterData):
    legal_name = models.CharField(max_length=255, blank=True)
    gstin = models.CharField(max_length=15, blank=True)
    state_code = models.CharField(max_length=2, blank=True, help_text="GST state code, e.g. 33 for Tamil Nadu")
    address = models.TextField(blank=True)
    default_currency = models.CharField(max_length=3, default="INR")

    class Meta(MasterData.Meta):
        verbose_name_plural = "legal entities"


class Department(MasterData):
    pass


class Project(MasterData):
    """Project or cost centre."""


class Category(MasterData):
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    receipt_required = models.BooleanField(default=True)
    per_claim_limit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    class Meta(MasterData.Meta):
        verbose_name_plural = "categories"


class Vendor(MasterData):
    gstin = models.CharField(max_length=15, blank=True)
    state_code = models.CharField(max_length=2, blank=True)


class PaymentMethod(MasterData):
    pass


class TaxCode(models.Model):
    """GST rates are configuration, never hard-coded (FR-MDM-004, §8.1)."""
    code = models.CharField(max_length=32)
    label = models.CharField(max_length=100)
    rate = models.DecimalField(max_digits=6, decimal_places=3)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code", "-effective_from"]
        unique_together = [("code", "effective_from")]

    def __str__(self):
        return f"{self.label} ({self.rate}%)"


# --------------------------------------------------------------------------
# Users, roles and entity scope
# --------------------------------------------------------------------------

class Role(models.TextChoices):
    SUPER_ADMIN = "SUPER_ADMIN", "Super Admin"
    FINANCE_ADMIN = "FINANCE_ADMIN", "Finance Admin"
    ACCOUNTANT = "ACCOUNTANT", "Accountant / Finance Reviewer"
    MANAGER = "MANAGER", "Manager / Approver"
    EMPLOYEE = "EMPLOYEE", "Employee / Submitter"
    VIEWER = "VIEWER", "Management Viewer"
    AUDITOR = "AUDITOR", "Auditor"


class EmployeeProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="profile")
    employee_code = models.CharField(max_length=32, unique=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    default_entity = models.ForeignKey(LegalEntity, null=True, blank=True, on_delete=models.PROTECT)
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                related_name="reports")

    def __str__(self):
        return f"{self.employee_code} – {self.user.get_full_name() or self.user.username}"


class RoleAssignment(models.Model):
    """A role scoped to one entity, or to all entities when entity is empty."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.CharField(max_length=20, choices=Role.choices)
    entity = models.ForeignKey(LegalEntity, null=True, blank=True, on_delete=models.PROTECT,
                               help_text="Leave empty for all entities")
    approval_limit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True,
                                         help_text="Managers only. Empty means no limit.")

    class Meta:
        unique_together = [("user", "role", "entity")]

    def __str__(self):
        return f"{self.user} · {self.get_role_display()} · {self.entity.code if self.entity else 'All entities'}"


# --------------------------------------------------------------------------
# Expenses
# --------------------------------------------------------------------------

class Status(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    MANAGER_REVIEW = "MANAGER_REVIEW", "Manager Review"
    MANAGER_APPROVED = "MANAGER_APPROVED", "Manager Approved"
    FINANCE_VERIFICATION = "FINANCE_VERIFICATION", "Finance Verification"
    FINANCE_VERIFIED = "FINANCE_VERIFIED", "Finance Verified"
    RETURNED = "RETURNED", "Returned"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


EDITABLE_STATUSES = {Status.DRAFT, Status.RETURNED}


class GSTType(models.TextChoices):
    CGST_SGST = "CGST_SGST", "CGST + SGST/UTGST (intra-state)"
    IGST = "IGST", "IGST (inter-state)"
    CESS = "CESS", "Cess only"
    EXEMPT = "EXEMPT", "Exempt"
    NIL_RATED = "NIL_RATED", "Nil rated"
    ZERO_RATED = "ZERO_RATED", "Zero rated"
    NON_GST = "NON_GST", "Non-GST"
    RCM = "RCM", "Reverse charge"


class ITCStatus(models.TextChoices):
    NOT_REVIEWED = "NOT_REVIEWED", "Not reviewed"
    PENDING_EVIDENCE = "PENDING_EVIDENCE", "Pending evidence"
    POTENTIALLY_ELIGIBLE = "POTENTIALLY_ELIGIBLE", "Potentially eligible"
    VERIFIED_ELIGIBLE = "VERIFIED_ELIGIBLE", "Verified eligible"
    INELIGIBLE = "INELIGIBLE", "Ineligible"
    REVERSED = "REVERSED", "Reversed / adjusted"


class SourceType(models.TextChoices):
    MANUAL = "MANUAL", "Manual / mobile"
    EXCEL = "EXCEL", "Excel import"
    CSV = "CSV", "CSV import"
    API = "API", "API"


class Expense(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ref = models.CharField(max_length=20, unique=True, editable=False, help_text="Human-readable immutable ID")

    # Core
    title = models.CharField("expense name", max_length=200)
    description = models.TextField(blank=True, help_text="Business purpose")
    notes = models.TextField(blank=True)
    expense_date = models.DateField()
    paying_entity = models.ForeignKey(LegalEntity, on_delete=models.PROTECT, related_name="paid_expenses")
    beneficiary_entity = models.ForeignKey(LegalEntity, null=True, blank=True, on_delete=models.PROTECT,
                                           related_name="beneficiary_expenses")
    submitter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="expenses")
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    payment_method = models.ForeignKey(PaymentMethod, null=True, blank=True, on_delete=models.PROTECT)
    currency = models.CharField(max_length=3, default="INR")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT, db_index=True)

    # Invoice and GST
    vendor = models.ForeignKey(Vendor, null=True, blank=True, on_delete=models.PROTECT)
    vendor_name_snapshot = models.CharField(max_length=255, blank=True)
    invoice_number = models.CharField(max_length=64, blank=True)
    invoice_number_normalized = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    invoice_date = models.DateField(null=True, blank=True)
    supplier_gstin = models.CharField(max_length=15, blank=True)
    recipient_gstin = models.CharField(max_length=15, blank=True)
    supplier_state_code = models.CharField(max_length=2, blank=True)
    place_of_supply_state_code = models.CharField(max_length=2, blank=True)
    gst_type = models.CharField(max_length=12, choices=GSTType.choices, blank=True)
    gst_rate = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    taxable_value = money()
    cgst_amount = money()
    sgst_utgst_amount = money()
    igst_amount = money()
    cess_amount = money()
    other_charges = money()
    gross_amount = money()
    source_tax_values = models.JSONField(default=dict, blank=True)
    calculated_tax_values = models.JSONField(default=dict, blank=True)
    tax_variance = models.BooleanField(default=False)

    # ITC review (separate from GST amount; default zero until verified)
    itc_status = models.CharField(max_length=24, choices=ITCStatus.choices, default=ITCStatus.NOT_REVIEWED)
    potential_itc_amount = money()
    verified_itc_amount = money()
    itc_reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="itc_reviews")
    itc_reviewed_at = models.DateTimeField(null=True, blank=True)
    itc_reason = models.TextField(blank=True)
    itc_reference = models.CharField(max_length=200, blank=True)

    # Reconciliation (manual; GSTR-2B integration out of scope)
    reconciliation_status = models.CharField(max_length=20, default="UNRECONCILED",
                                             choices=[("UNRECONCILED", "Unreconciled"), ("MATCHED", "Matched"),
                                                      ("MISMATCH", "Mismatch"), ("NOT_APPLICABLE", "Not applicable")])

    # Provenance and duplicates
    source_type = models.CharField(max_length=10, choices=SourceType.choices, default=SourceType.MANUAL)
    external_record_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    sync_run = models.ForeignKey("SyncRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses")
    duplicate_suspect = models.BooleanField(default=False)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    version_number = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-expense_date", "-created_at"]
        indexes = [models.Index(fields=["paying_entity", "expense_date"])]

    def __str__(self):
        return f"{self.ref} {self.title}"

    @property
    def total_gst(self):
        return self.cgst_amount + self.sgst_utgst_amount + self.igst_amount + self.cess_amount

    @property
    def is_locked(self):
        return self.status not in EDITABLE_STATUSES

    def save(self, *args, **kwargs):
        if not self.ref:
            last = Expense.objects.order_by("-ref").values_list("ref", flat=True).first()
            n = int(last.split("-")[1]) + 1 if last else 1
            self.ref = f"EXP-{n:06d}"
        self.invoice_number_normalized = normalize_invoice(self.invoice_number)
        if self.vendor and not self.vendor_name_snapshot:
            self.vendor_name_snapshot = self.vendor.name
        super().save(*args, **kwargs)


def normalize_invoice(value):
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


class Attachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="attachments")
    storage_key = models.CharField(max_length=255)
    file_name = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class WorkflowEvent(models.Model):
    """Append-only record of every status transition."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(Expense, on_delete=models.PROTECT, related_name="workflow_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=30)
    from_state = models.CharField(max_length=24, blank=True)
    to_state = models.CharField(max_length=24)
    comment = models.TextField(blank=True)
    snapshot = models.JSONField(default=dict)
    event_time = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["event_time"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Workflow events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow events are append-only.")


class AllocationMethod(models.TextChoices):
    PERCENT = "PERCENT", "Percentage"
    FIXED = "FIXED", "Fixed amount"


class RechargeStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_REVIEW = "PENDING_REVIEW", "Pending review"
    APPROVED = "APPROVED", "Approved for recharge"
    INVOICED = "INVOICED", "Recharge invoiced"
    SETTLED = "SETTLED", "Settled"
    CANCELLED = "CANCELLED", "Cancelled"


class IntercompanyAllocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    expense = models.ForeignKey(Expense, on_delete=models.CASCADE, related_name="allocations")
    beneficiary_entity = models.ForeignKey(LegalEntity, on_delete=models.PROTECT)
    method = models.CharField(max_length=10, choices=AllocationMethod.choices)
    percent = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    amount = money()
    recharge_amount = money(help_text="May differ from the allocated cost; tax treatment set by Finance")
    recharge_status = models.CharField(max_length=20, choices=RechargeStatus.choices, default=RechargeStatus.DRAFT)
    reference = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=255, blank=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.PROTECT)
    line_no = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["line_no"]


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    expense = models.ForeignKey(Expense, null=True, on_delete=models.CASCADE)
    message = models.CharField(max_length=300)
    is_read = models.BooleanField(default=False)
    delivery_status = models.CharField(max_length=10, default="IN_APP")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# --------------------------------------------------------------------------
# Imports / sync
# --------------------------------------------------------------------------

class SyncStatus(models.TextChoices):
    AWAITING_REVIEW = "AWAITING_REVIEW", "Awaiting review"
    SUCCESS = "SUCCESS", "Success"
    PARTIAL = "PARTIAL", "Partial success"
    FAILED = "FAILED", "Failed"
    SKIPPED = "SKIPPED", "Skipped (unchanged file)"


class SyncRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=30, default="MANUAL_UPLOAD")
    source_file_id = models.CharField(max_length=255, blank=True)
    file_name = models.CharField(max_length=255)
    storage_key = models.CharField(max_length=255, blank=True)
    checksum = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=20, choices=SyncStatus.choices, default=SyncStatus.AWAITING_REVIEW)
    row_counts = models.JSONField(default=dict)
    error_message = models.TextField(blank=True)
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class RowStatus(models.TextChoices):
    NEW = "NEW", "New"
    UPDATE = "UPDATE", "Update"
    DUPLICATE = "DUPLICATE", "Duplicate candidate"
    UNCHANGED = "UNCHANGED", "Unchanged"
    REJECTED = "REJECTED", "Rejected"
    CREATED = "CREATED", "Created"
    UPDATED = "UPDATED", "Updated"
    SKIPPED = "SKIPPED", "Skipped"


class ImportRow(models.Model):
    sync_run = models.ForeignKey(SyncRun, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    external_record_id = models.CharField(max_length=100, blank=True)
    row_status = models.CharField(max_length=12, choices=RowStatus.choices)
    errors = models.JSONField(default=list)
    warnings = models.JSONField(default=list)
    data = models.JSONField(default=dict)
    expense = models.ForeignKey(Expense, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["row_number"]


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------

class AuditEvent(models.Model):
    """Append-only audit trail (FR-AUD-001..003)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=50, db_index=True)
    record_type = models.CharField(max_length=50, blank=True)
    record_id = models.CharField(max_length=64, blank=True)
    result = models.CharField(max_length=20, default="OK")
    changes = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Audit events are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events are append-only.")
