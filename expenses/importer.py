"""Excel / CSV import with preview, idempotent upsert and row-level errors (SRS §4.4, §6)."""
import csv
import hashlib
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from openpyxl import load_workbook

from . import access, tax
from .audit import audit
from .models import (Category, Department, EmployeeProfile, Expense, GSTType, ImportRow, LegalEntity,
                     PaymentMethod, Project, RowStatus, SourceType, SyncRun, SyncStatus, Vendor,
                     normalize_invoice)
from .storage import save_private

TEMPLATE_VERSION = "1.0"

# (header, model attribute, kind). Kind drives parsing and comparison.
COLUMNS = [
    ("ExpenseRef", "ref", "key"),
    ("ExternalRecordID", "external_record_id", "key"),
    ("ExpenseName", "title", "text"),
    ("ExpenseDate", "expense_date", "date"),
    ("PayingEntityCode", "paying_entity_id", ("fk", LegalEntity)),
    ("BeneficiaryEntityCode", "beneficiary_entity_id", ("fk", LegalEntity)),
    ("EmployeeCode", "submitter_id", "employee"),
    ("CategoryCode", "category_id", ("fk", Category)),
    ("DepartmentCode", "department_id", ("fk", Department)),
    ("ProjectCode", "project_id", ("fk", Project)),
    ("Description", "description", "text"),
    ("VendorName", "vendor_name_snapshot", "text"),
    ("SupplierGSTIN", "supplier_gstin", "gstin"),
    ("SupplierStateCode", "supplier_state_code", "state"),
    ("PlaceOfSupplyStateCode", "place_of_supply_state_code", "state"),
    ("InvoiceNumber", "invoice_number", "text"),
    ("InvoiceDate", "invoice_date", "date"),
    ("Currency", "currency", "currency"),
    ("TaxableValue", "taxable_value", "money"),
    ("GSTType", "gst_type", "gst_type"),
    ("GSTRate", "gst_rate", "rate"),
    ("CGSTAmount", "cgst_amount", "tax"),
    ("SGSTAmount", "sgst_utgst_amount", "tax"),
    ("IGSTAmount", "igst_amount", "tax"),
    ("CessAmount", "cess_amount", "tax"),
    ("OtherCharges", "other_charges", "money"),
    ("GrossAmount", "gross_amount", "tax"),
    ("PaymentMethod", "payment_method_id", ("fk", PaymentMethod)),
    ("Notes", "notes", "text"),
    ("SourceNotes", None, "ignore"),
]
HEADERS = [c[0] for c in COLUMNS]
REQUIRED = {"ExpenseName", "ExpenseDate", "PayingEntityCode", "CategoryCode"}
MONEY_FIELDS = {"taxable_value", "other_charges", "cgst_amount", "sgst_utgst_amount", "igst_amount",
                "cess_amount", "gross_amount", "gst_rate"}
DATE_FIELDS = {"expense_date", "invoice_date"}
TAX_FIELDS = {"cgst_amount", "sgst_utgst_amount", "igst_amount", "cess_amount", "gross_amount"}
# Extra columns that appear in our own exports; ignored on import.
EXPORT_ONLY = ["Status", "TotalGST", "ITCStatus", "VerifiedITC", "Submitter", "Source", "DuplicateSuspect"]


class ImportFileError(Exception):
    pass


# ---------------------------------------------------------------- parsing

def _cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def read_rows(file_name, content):
    """Yield (row_number, {header: str}). Formulas/macros are never executed:
    openpyxl reads cached values only."""
    name = file_name.lower()
    if name.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("cp1252")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        offset = 1
    elif name.endswith(".xlsx"):
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # corrupt / not a workbook
            raise ImportFileError(f"Could not read workbook: {exc}") from exc
        ws = wb["Expenses"] if "Expenses" in wb.sheetnames else wb.worksheets[0]
        rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
        offset = 1
    else:
        raise ImportFileError("Only .xlsx and .csv files are supported.")

    header_idx = next((i for i, r in enumerate(rows[:20]) if "ExpenseDate" in [c.strip() for c in r]), None)
    if header_idx is None:
        raise ImportFileError("Header row not found. Use the template: the header must include ExpenseDate.")
    headers = [c.strip() for c in rows[header_idx]]
    missing = REQUIRED - set(headers)
    if missing:
        raise ImportFileError(f"Missing required columns: {', '.join(sorted(missing))}")
    unknown = [h for h in headers if h and h not in HEADERS and h not in EXPORT_ONLY]
    out = []
    for i, r in enumerate(rows[header_idx + 1:], start=header_idx + 1 + offset):
        if not any(str(c).strip() for c in r):
            continue
        out.append((i, {h: (str(r[j]).strip() if j < len(r) else "") for j, h in enumerate(headers) if h}))
    if len(out) > settings.EXPENSE_SETTINGS["IMPORT_MAX_ROWS"]:
        raise ImportFileError(f"File has {len(out)} rows; the limit is {settings.EXPENSE_SETTINGS['IMPORT_MAX_ROWS']}.")
    return out, unknown


# ------------------------------------------------------------- validation

class Lookups:
    def __init__(self, user):
        self.user = user
        self.fk = {}
        for model in (LegalEntity, Category, Department, Project, PaymentMethod):
            self.fk[model] = {o.code.upper(): o for o in model.objects.filter(is_active=True)}
        self.employees = {p.employee_code.upper(): p for p in EmployeeProfile.objects.select_related("user")}
        self.allowed_entities = set(access.visible_entities(user).values_list("id", flat=True))
        self.vendors = {v.name.lower(): v for v in Vendor.objects.filter(is_active=True)}


def _err(errors, field, code, message):
    errors.append({"field": field, "code": code, "message": message})


def parse_date(value):
    value = value.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return date.fromisoformat(value)
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", value)
    if m:
        d, mth, y = (int(x) for x in m.groups())
        if d > 12 or mth > 12 or d == mth:
            # dd/mm/yyyy is the Indian convention; only accept when it can't be mm/dd.
            if mth > 12:
                raise ValueError("ambiguous")
            return date(y, mth, d)
        raise ValueError("ambiguous")
    raise ValueError("format")


def validate_row(raw, lk):
    clean, declared, errors, warnings = {}, {}, [], []
    for header, attr, kind in COLUMNS:
        value = (raw.get(header) or "").strip()
        if header in REQUIRED and not value:
            _err(errors, header, "REQUIRED", f"{header} is required.")
            continue
        if not value or attr is None or kind == "ignore":
            continue
        if kind in ("key", "text"):
            clean[attr] = value[:200] if attr == "title" else value
        elif kind == "date":
            try:
                clean[attr] = parse_date(value)
            except ValueError as e:
                msg = "ambiguous day/month; use yyyy-mm-dd" if str(e) == "ambiguous" else "use yyyy-mm-dd"
                _err(errors, header, "BAD_DATE", f"{header} '{value}' is not a valid date ({msg}).")
        elif kind in ("money", "tax", "rate"):
            try:
                num = Decimal(value.replace(",", ""))
                if num < 0:
                    raise InvalidOperation
                if num.as_tuple().exponent < -2 and kind != "rate":
                    _err(errors, header, "PRECISION", f"{header} has more than 2 decimal places.")
                    continue
            except InvalidOperation:
                _err(errors, header, "BAD_NUMBER", f"{header} '{value}' must be a non-negative number.")
                continue
            if kind == "tax":
                declared[attr] = num
            else:
                clean[attr] = num
        elif isinstance(kind, tuple):
            obj = lk.fk[kind[1]].get(value.upper())
            if not obj:
                _err(errors, header, "UNKNOWN_CODE", f"{header} '{value}' does not match an active record.")
            elif kind[1] is LegalEntity and obj.id not in lk.allowed_entities:
                _err(errors, header, "NO_ACCESS", f"You do not have access to entity {value}.")
            else:
                clean[attr] = obj.id
        elif kind == "employee":
            prof = lk.employees.get(value.upper())
            if not prof:
                _err(errors, header, "UNKNOWN_EMPLOYEE", f"EmployeeCode '{value}' is not a known employee.")
            else:
                clean[attr] = prof.user_id
        elif kind == "gstin":
            v = value.upper()
            if not re.match(tax.GSTIN_PATTERN, v):
                _err(errors, header, "BAD_GSTIN", f"SupplierGSTIN '{value}' format is invalid.")
            else:
                clean[attr] = v
        elif kind == "state":
            if not re.match(r"^\d{2}$", value.zfill(2)):
                _err(errors, header, "BAD_STATE", f"{header} must be a 2-digit GST state code.")
            else:
                clean[attr] = value.zfill(2)
        elif kind == "currency":
            if not re.match(r"^[A-Za-z]{3}$", value):
                _err(errors, header, "BAD_CURRENCY", "Currency must be a 3-letter code.")
            else:
                clean[attr] = value.upper()
        elif kind == "gst_type":
            v = value.upper()
            if v not in GSTType.values:
                _err(errors, header, "BAD_GST_TYPE", f"GSTType must be one of {', '.join(GSTType.values)}.")
            else:
                clean[attr] = v
    if not errors:
        if "gross_amount" not in declared and not clean.get("taxable_value"):
            _err(errors, "GrossAmount", "REQUIRED", "GrossAmount (or TaxableValue) is required.")
        if clean.get("expense_date") and clean["expense_date"] > timezone.localdate():
            _err(errors, "ExpenseDate", "FUTURE_DATE", "ExpenseDate is in the future.")
        if clean.get("invoice_number") and not clean.get("invoice_date"):
            warnings.append("InvoiceDate missing for an invoice-based expense.")
        if clean.get("gst_type") in (GSTType.CGST_SGST, GSTType.IGST, GSTType.RCM) and "gst_rate" not in clean:
            _err(errors, "GSTRate", "REQUIRED", "GSTRate is required for this GSTType.")
    return clean, declared, errors, warnings


def _to_json(clean, declared):
    out = {k: (v.isoformat() if isinstance(v, date) else str(v) if isinstance(v, Decimal) else v)
           for k, v in clean.items()}
    out["_declared"] = {k: str(v) for k, v in declared.items()}
    return out


def _from_json(data):
    data = dict(data)
    declared = {k: Decimal(v) for k, v in data.pop("_declared", {}).items()}
    clean = {}
    for k, v in data.items():
        if k in DATE_FIELDS:
            clean[k] = date.fromisoformat(v)
        elif k in MONEY_FIELDS:
            clean[k] = Decimal(v)
        else:
            clean[k] = v
    return clean, declared


def _differs(expense, clean, declared):
    for k, v in clean.items():
        if k in ("ref", "external_record_id"):
            continue
        if getattr(expense, k) != v:
            return True
    for k, v in declared.items():
        if getattr(expense, k) != tax.q(v):
            return True
    return False


def _find_duplicate(clean, declared):
    inv = normalize_invoice(clean.get("invoice_number"))
    if not inv:
        return None
    qs = Expense.objects.filter(paying_entity_id=clean.get("paying_entity_id"), invoice_number_normalized=inv)
    if clean.get("vendor_name_snapshot"):
        qs = qs.filter(vendor_name_snapshot__iexact=clean["vendor_name_snapshot"])
    if clean.get("invoice_date"):
        qs = qs.filter(invoice_date=clean["invoice_date"])
    if "gross_amount" in declared:
        qs = qs.filter(gross_amount=tax.q(declared["gross_amount"]))
    return qs.first()


# ---------------------------------------------------------------- preview

def preview(user, uploaded, reprocess=False):
    content = uploaded.read()
    if len(content) > settings.EXPENSE_SETTINGS["IMPORT_MAX_BYTES"]:
        raise ImportFileError("File is too large.")
    checksum = hashlib.sha256(content).hexdigest()
    run = SyncRun(provider="MANUAL_UPLOAD", file_name=uploaded.name[:255], checksum=checksum, started_by=user)
    prior = SyncRun.objects.filter(checksum=checksum, status__in=[SyncStatus.SUCCESS, SyncStatus.PARTIAL]).first()
    if prior and not reprocess:
        run.status = SyncStatus.SKIPPED
        run.error_message = (f"This exact file was already imported on {timezone.localtime(prior.started_at):%d %b %Y %H:%M}. "
                             "Nothing to do. Tick 'Reprocess' to check it again.")
        run.finished_at = timezone.now()
        run.save()
        audit(user, "import.skipped", run)
        return run
    run.storage_key = save_private("imports", uploaded.name, content)
    try:
        rows, unknown = read_rows(uploaded.name, content)
    except ImportFileError as exc:
        run.status = SyncStatus.FAILED
        run.error_message = str(exc)
        run.finished_at = timezone.now()
        run.save()
        audit(user, "import.failed", run, {"error": str(exc)}, result="FAILED")
        return run
    run.save()
    lk = Lookups(user)
    scope = access.expense_queryset_for(user)
    seen_ext = set()
    counts = {s: 0 for s in ("NEW", "UPDATE", "DUPLICATE", "UNCHANGED", "REJECTED")}
    objs = []
    for row_no, raw in rows:
        clean, declared, errors, warnings = validate_row(raw, lk)
        target = None
        ext = clean.get("external_record_id", "")
        if ext:
            if ext in seen_ext:
                _err(errors, "ExternalRecordID", "DUPLICATE_IN_FILE", f"ExternalRecordID '{ext}' appears twice in the file.")
            seen_ext.add(ext)
        if clean.get("ref"):
            target = scope.filter(ref=clean["ref"]).first()
            if not target:
                _err(errors, "ExpenseRef", "NOT_FOUND", f"ExpenseRef {clean['ref']} not found or not accessible.")
        elif ext:
            target = Expense.objects.filter(external_record_id=ext).first()
            if target and not scope.filter(pk=target.pk).exists():
                _err(errors, "ExternalRecordID", "NO_ACCESS", "Matching expense is outside your access.")
        status = RowStatus.REJECTED
        if not errors:
            if target:
                if target.is_locked:
                    _err(errors, "ExpenseRef", "LOCKED",
                         f"{target.ref} is {target.get_status_display()}; approved or submitted records are not "
                         "overwritten by import. Use a controlled amendment (reopen) instead.")
                elif _differs(target, clean, declared):
                    status = RowStatus.UPDATE
                else:
                    status = RowStatus.UNCHANGED
            else:
                dup = _find_duplicate(clean, declared)
                status = RowStatus.DUPLICATE if dup else RowStatus.NEW
                if dup:
                    warnings.append(f"Possible duplicate of {dup.ref} (same entity, vendor, invoice no., date, amount). "
                                    "It will be imported and flagged for review, not discarded.")
                    clean["_duplicate_of"] = str(dup.pk)
        if errors:
            status = RowStatus.REJECTED
        counts[status] += 1
        objs.append(ImportRow(sync_run=run, row_number=row_no, external_record_id=ext, row_status=status,
                              errors=errors, warnings=warnings, data=_to_json(clean, declared),
                              expense=target))
    ImportRow.objects.bulk_create(objs)
    run.row_counts = {**counts, "total": len(rows), "unknown_columns": unknown}
    run.save(update_fields=["row_counts"])
    audit(user, "import.preview", run, {"file": run.file_name, "counts": counts})
    return run


# ----------------------------------------------------------------- commit

def _apply(expense, clean, declared):
    for k, v in clean.items():
        if k in ("ref", "_duplicate_of"):
            continue
        setattr(expense, k, v)
    if not expense.vendor_id and expense.vendor_name_snapshot:
        expense.vendor = Vendor.objects.filter(name__iexact=expense.vendor_name_snapshot).first()
    if not expense.recipient_gstin:
        expense.recipient_gstin = LegalEntity.objects.get(pk=expense.paying_entity_id).gstin
    tax.apply_tax(expense, declared)


def commit(user, run):
    if run.status != SyncStatus.AWAITING_REVIEW:
        raise ImportFileError("This import has already been processed.")
    source = SourceType.CSV if run.file_name.lower().endswith(".csv") else SourceType.EXCEL
    created = updated = skipped = rejected = 0
    for row in run.rows.all():
        if row.row_status == RowStatus.REJECTED:
            rejected += 1
            continue
        if row.row_status == RowStatus.UNCHANGED:
            row.row_status = RowStatus.SKIPPED
            row.save(update_fields=["row_status"])
            skipped += 1
            continue
        clean, declared = _from_json(row.data)
        submitter_id = clean.pop("submitter_id", user.id)
        try:
            with transaction.atomic():
                if row.row_status == RowStatus.UPDATE:
                    exp = Expense.objects.select_for_update().get(pk=row.expense_id)
                    if exp.is_locked:
                        raise ImportFileError(f"{exp.ref} became locked ({exp.get_status_display()}) after preview.")
                    _apply(exp, clean, declared)
                    exp.version_number += 1
                    exp.sync_run = run
                    exp.save()
                    row.row_status = RowStatus.UPDATED
                    updated += 1
                else:
                    if clean.get("external_record_id") and Expense.objects.filter(
                            external_record_id=clean["external_record_id"]).exists():
                        raise ImportFileError("A record with this ExternalRecordID was created after preview.")
                    exp = Expense(submitter_id=submitter_id, source_type=source, sync_run=run)
                    _apply(exp, clean, declared)
                    if clean.get("_duplicate_of"):
                        exp.duplicate_suspect = True
                        exp.duplicate_of_id = clean["_duplicate_of"]
                    exp.save()
                    row.row_status = RowStatus.CREATED
                    created += 1
                row.expense = exp
                row.save(update_fields=["row_status", "expense"])
        except Exception as exc:  # one bad row never corrupts the others
            row.row_status = RowStatus.REJECTED
            row.errors = row.errors + [{"field": "", "code": "COMMIT_FAILED", "message": str(exc)}]
            row.save(update_fields=["row_status", "errors"])
            rejected += 1
    committed = created + updated
    if rejected and committed:
        run.status = SyncStatus.PARTIAL
    elif rejected and not committed and not skipped:
        run.status = SyncStatus.FAILED
    else:
        run.status = SyncStatus.SUCCESS
    run.row_counts = {**run.row_counts, "created": created, "updated": updated, "skipped": skipped,
                      "rejected": rejected}
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "row_counts", "finished_at"])
    audit(user, "import.commit", run, {"status": run.status, **run.row_counts})
    return run


def error_report_csv(run):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["RowNumber", "ExternalRecordID", "Field", "ErrorCode", "Message"])
    for row in run.rows.filter(row_status=RowStatus.REJECTED):
        for e in row.errors:
            w.writerow([row.row_number, row.external_record_id, e.get("field"), e.get("code"), e.get("message")])
    return buf.getvalue()
