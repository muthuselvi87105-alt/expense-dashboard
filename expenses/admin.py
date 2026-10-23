from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.forms.models import model_to_dict

from .audit import audit
from .models import (Attachment, AuditEvent, Category, Department, EmployeeProfile, Expense, ImportRow,
                     IntercompanyAllocation, LegalEntity, PaymentMethod, Project, RoleAssignment, SyncRun, TaxCode,
                     Vendor, WorkflowEvent)

User = get_user_model()


def _jsonable(d):
    return {k: (str(v) if v is not None else None) for k, v in d.items()}


class AuditedAdmin(admin.ModelAdmin):
    """Logs every admin create/update to the append-only audit trail (FR-MDM-004, FR-AUTH-004).
    Deletion is disabled: referenced master data is deactivated instead (FR-MDM-005)."""

    def save_model(self, request, obj, form, change):
        before = _jsonable(model_to_dict(type(obj).objects.get(pk=obj.pk))) if change else {}
        super().save_model(request, obj, form, change)
        after = _jsonable(model_to_dict(obj))
        audit(request.user, f"admin.{'update' if change else 'create'}.{type(obj).__name__.lower()}", obj,
              {k: {"from": before.get(k), "to": v} for k, v in after.items() if before.get(k) != v}, request=request)

    def has_delete_permission(self, request, obj=None):
        return False


class MasterAdmin(AuditedAdmin):
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")


@admin.register(LegalEntity)
class LegalEntityAdmin(MasterAdmin):
    list_display = ("code", "name", "gstin", "state_code", "is_active")


@admin.register(Category)
class CategoryAdmin(MasterAdmin):
    list_display = ("code", "name", "parent", "receipt_required", "per_claim_limit", "is_active")


for m in (Department, Project, PaymentMethod):
    admin.site.register(m, MasterAdmin)


@admin.register(Vendor)
class VendorAdmin(MasterAdmin):
    list_display = ("code", "name", "gstin", "state_code", "is_active")


@admin.register(TaxCode)
class TaxCodeAdmin(AuditedAdmin):
    list_display = ("code", "label", "rate", "effective_from", "effective_to", "is_active")


class RoleInline(admin.TabularInline):
    model = RoleAssignment
    extra = 1


class ProfileInline(admin.StackedInline):
    model = EmployeeProfile
    fk_name = "user"
    can_delete = False


admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [ProfileInline, RoleInline]
    list_display = ("username", "first_name", "last_name", "email", "is_active", "roles")

    def roles(self, obj):
        return ", ".join(str(r.get_role_display()) + (f" ({r.entity.code})" if r.entity else "")
                         for r in obj.role_assignments.all())

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        audit(request.user, "admin.user.save", obj, {"is_active": obj.is_active, "is_staff": obj.is_staff},
              request=request)

    def save_formset(self, request, form, formset, change):
        before = list(RoleAssignment.objects.filter(user=form.instance).values_list("role", "entity__code"))
        super().save_formset(request, form, formset, change)
        if formset.model is RoleAssignment:
            after = list(RoleAssignment.objects.filter(user=form.instance).values_list("role", "entity__code"))
            if before != after:
                audit(request.user, "admin.roles.change", form.instance, {"from": before, "to": after},
                      request=request)

    def has_delete_permission(self, request, obj=None):
        return False  # deactivate users instead; history stays intact (FR-AUTH-006)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("timestamp", "actor", "action", "record_type", "record_id", "result")
    list_filter = ("action", "result")
    search_fields = ("record_id", "action")


@admin.register(WorkflowEvent)
class WorkflowEventAdmin(ReadOnlyAdmin):
    list_display = ("event_time", "expense", "actor", "action", "from_state", "to_state")


@admin.register(Expense)
class ExpenseAdmin(ReadOnlyAdmin):
    list_display = ("ref", "title", "expense_date", "paying_entity", "gross_amount", "status", "source_type")
    list_filter = ("status", "paying_entity", "source_type")
    search_fields = ("ref", "title", "invoice_number")


class ImportRowInline(admin.TabularInline):
    model = ImportRow
    extra = 0
    can_delete = False
    readonly_fields = ("row_number", "external_record_id", "row_status", "errors", "warnings", "expense")
    exclude = ("data",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SyncRun)
class SyncRunAdmin(ReadOnlyAdmin):
    list_display = ("started_at", "file_name", "provider", "status", "started_by")
    inlines = [ImportRowInline]


admin.site.register(Attachment, ReadOnlyAdmin)
admin.site.register(IntercompanyAllocation, ReadOnlyAdmin)
