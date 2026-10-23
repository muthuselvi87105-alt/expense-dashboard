import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers

from . import access, tax, workflow
from .models import (AllocationMethod, Attachment, Category, Department, Expense, IntercompanyAllocation,
                     ITCStatus, LegalEntity, Notification, PaymentMethod, Project, SyncRun, Vendor,
                     WorkflowEvent)

User = get_user_model()
TAX_INPUT_FIELDS = ("cgst_amount", "sgst_utgst_amount", "igst_amount", "cess_amount", "gross_amount")


class EntitySerializer(serializers.ModelSerializer):
    class Meta:
        model = LegalEntity
        fields = ["id", "code", "name", "legal_name", "gstin", "state_code", "default_currency", "is_active"]


def simple(model_cls):
    class S(serializers.ModelSerializer):
        class Meta:
            model = model_cls
            fields = ["id", "code", "name", "is_active"]
    S.__name__ = f"{model_cls.__name__}Serializer"
    return S


DepartmentSerializer = simple(Department)
ProjectSerializer = simple(Project)
PaymentMethodSerializer = simple(PaymentMethod)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "code", "name", "parent", "receipt_required", "per_claim_limit", "is_active"]


class VendorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = ["id", "code", "name", "gstin", "state_code", "is_active"]


class UserMiniSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "name"]

    def get_name(self, obj):
        return obj.get_full_name() or obj.username


class AttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Attachment
        fields = ["id", "file_name", "mime_type", "size", "sha256", "uploaded_at"]


class WorkflowEventSerializer(serializers.ModelSerializer):
    actor = UserMiniSerializer(read_only=True)

    class Meta:
        model = WorkflowEvent
        fields = ["id", "action", "from_state", "to_state", "comment", "actor", "event_time"]


class AllocationSerializer(serializers.ModelSerializer):
    beneficiary_entity_code = serializers.CharField(source="beneficiary_entity.code", read_only=True)

    class Meta:
        model = IntercompanyAllocation
        fields = ["id", "line_no", "beneficiary_entity", "beneficiary_entity_code", "method", "percent", "amount",
                  "recharge_amount", "recharge_status", "reference", "reason", "project"]
        read_only_fields = ["line_no"]


class ExpenseListSerializer(serializers.ModelSerializer):
    paying_entity_code = serializers.CharField(source="paying_entity.code", read_only=True)
    category_name = serializers.CharField(source="category.name", read_only=True)
    submitter_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    total_gst = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    attachment_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Expense
        fields = ["id", "ref", "title", "expense_date", "paying_entity_code", "category_name", "vendor_name_snapshot",
                  "gross_amount", "total_gst", "currency", "status", "status_label", "submitter_name",
                  "duplicate_suspect", "tax_variance", "attachment_count", "source_type", "notes"]

    def get_submitter_name(self, obj):
     return "Muthu"


class ExpenseSerializer(serializers.ModelSerializer):
    submitter = UserMiniSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    itc_status_label = serializers.CharField(source="get_itc_status_display", read_only=True)
    total_gst = serializers.DecimalField(max_digits=18, decimal_places=2, read_only=True)
    attachments = AttachmentSerializer(many=True, read_only=True)
    allocations = AllocationSerializer(many=True, read_only=True)
    history = WorkflowEventSerializer(source="workflow_events", many=True, read_only=True)
    actions = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_review_itc = serializers.SerializerMethodField()
    # Accept tax components as optional "declared" values
    cgst_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)
    sgst_utgst_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)
    igst_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)
    cess_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)
    gross_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True)

    class Meta:
        model = Expense
        fields = [
            "id", "ref", "title", "description", "notes", "expense_date", "paying_entity", "beneficiary_entity",
            "submitter", "department", "project", "category", "payment_method", "currency", "status", "status_label",
            "vendor", "vendor_name_snapshot", "invoice_number", "invoice_date", "supplier_gstin", "recipient_gstin",
            "supplier_state_code", "place_of_supply_state_code", "gst_type", "gst_rate", "taxable_value",
            "cgst_amount", "sgst_utgst_amount", "igst_amount", "cess_amount", "other_charges", "gross_amount",
            "total_gst", "source_tax_values", "calculated_tax_values", "tax_variance",
            "itc_status", "itc_status_label", "potential_itc_amount", "verified_itc_amount", "itc_reviewer",
            "itc_reviewed_at", "itc_reason", "itc_reference", "reconciliation_status",
            "source_type", "external_record_id", "sync_run", "duplicate_suspect", "duplicate_of",
            "version_number", "created_at", "updated_at",
            "attachments", "allocations", "history", "actions", "can_edit", "can_review_itc",
        ]
        read_only_fields = [
            "ref", "status", "source_tax_values", "calculated_tax_values", "tax_variance", "itc_status",
            "potential_itc_amount", "verified_itc_amount", "itc_reviewer", "itc_reviewed_at", "itc_reason",
            "itc_reference", "source_type", "external_record_id", "sync_run", "duplicate_suspect", "duplicate_of",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"version_number": {"required": False}}

    def get_actions(self, obj):
        return workflow.available_actions(self.context["request"].user, obj)

    def get_can_edit(self, obj):
        return access.can_edit(self.context["request"].user, obj)

    def get_can_review_itc(self, obj):
        user = self.context["request"].user
        return obj.submitter_id != user.id and access.can_act_in_entity(user, workflow.FINANCE_ACT_ROLES, obj)

    def validate_supplier_gstin(self, value):
        value = (value or "").strip().upper()
        if value and not re.match(tax.GSTIN_PATTERN, value):
            raise serializers.ValidationError("GSTIN format looks invalid (format check only; not an ITC check).")
        return value

    validate_recipient_gstin = validate_supplier_gstin

    def validate(self, attrs):
        user = self.context["request"].user
        inst = self.instance
        for field in ("paying_entity", "beneficiary_entity"):
            ent = attrs.get(field)
            if ent is not None and not access.visible_entities(user).filter(pk=ent.pk).exists():
                raise serializers.ValidationError({field: "You do not have access to this entity."})
        if inst is not None and "version_number" in attrs and attrs["version_number"] != inst.version_number:
            raise serializers.ValidationError(
                {"version_number": "This expense was changed by someone else. Reload and try again."})
        exp_date = attrs.get("expense_date") or (inst and inst.expense_date)
        if exp_date and exp_date > timezone.localdate():
            raise serializers.ValidationError({"expense_date": "Expense date cannot be in the future."})
        for f in ("taxable_value", "other_charges", "gst_rate") + TAX_INPUT_FIELDS:
            v = attrs.get(f)
            if v is not None and v < 0:
                raise serializers.ValidationError({f: "Must not be negative."})
        currency = attrs.get("currency")
        if currency and not re.match(r"^[A-Z]{3}$", currency):
            raise serializers.ValidationError({"currency": "Use a 3-letter ISO currency code."})
        has_taxable = attrs.get("taxable_value") or (inst and inst.taxable_value)
        if not has_taxable and attrs.get("gross_amount") in (None, Decimal("0")) and not inst:
            raise serializers.ValidationError({"gross_amount": "Enter the amount (gross or taxable value)."})
        return attrs

    def _apply(self, instance, validated):
        declared = {}
        for f in TAX_INPUT_FIELDS:
            if f in validated:
                v = validated.pop(f)
                if v is not None:
                    declared[f] = v
        validated.pop("version_number", None)
        for k, v in validated.items():
            setattr(instance, k, v)
        if instance.vendor and not validated.get("vendor_name_snapshot"):
            instance.vendor_name_snapshot = instance.vendor.name
        if not instance.recipient_gstin and instance.paying_entity_id:
            instance.recipient_gstin = instance.paying_entity.gstin
        tax.apply_tax(instance, declared)
        return instance

    def create(self, validated):
        instance = Expense(submitter=self.context["request"].user)
        self._apply(instance, validated)
        instance.save()
        return instance

    def update(self, instance, validated):
        self._apply(instance, validated)
        instance.version_number += 1
        instance.save()
        return instance


class ITCReviewSerializer(serializers.Serializer):
    itc_status = serializers.ChoiceField(choices=ITCStatus.choices)
    verified_itc_amount = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0"),
                                                   required=False, default=Decimal("0"))
    reason = serializers.CharField()
    reference = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        exp = self.context["expense"]
        if attrs["itc_status"] != ITCStatus.VERIFIED_ELIGIBLE:
            attrs["verified_itc_amount"] = Decimal("0")
        elif attrs["verified_itc_amount"] > exp.total_gst:
            raise serializers.ValidationError({"verified_itc_amount": "Cannot exceed recorded GST."})
        return attrs


class AllocationInputSerializer(serializers.Serializer):
    beneficiary_entity = serializers.PrimaryKeyRelatedField(queryset=LegalEntity.objects.filter(is_active=True))
    method = serializers.ChoiceField(choices=AllocationMethod.choices)
    percent = serializers.DecimalField(max_digits=7, decimal_places=4, required=False, allow_null=True,
                                       min_value=Decimal("0"), max_value=Decimal("100"))
    amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, allow_null=True,
                                      min_value=Decimal("0"))
    recharge_amount = serializers.DecimalField(max_digits=18, decimal_places=2, required=False,
                                               default=Decimal("0"), min_value=Decimal("0"))
    recharge_status = serializers.ChoiceField(choices=IntercompanyAllocation._meta.get_field("recharge_status").choices,
                                              required=False, default="DRAFT")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class SyncRunSerializer(serializers.ModelSerializer):
    started_by = UserMiniSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = SyncRun
        fields = ["id", "provider", "file_name", "checksum", "status", "status_label", "row_counts",
                  "error_message", "started_by", "started_at", "finished_at"]


class NotificationSerializer(serializers.ModelSerializer):
    expense_ref = serializers.CharField(source="expense.ref", read_only=True, default=None)

    class Meta:
        model = Notification
        fields = ["id", "message", "expense", "expense_ref", "is_read", "created_at"]
