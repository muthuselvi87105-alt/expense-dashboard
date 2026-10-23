"""Load master data, demo users (one per role) and sample expenses.

    python manage.py seed_demo            # master data + users + ~80 sample expenses
    python manage.py seed_demo --no-expenses
"""
import hashlib
import io
import random
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas

from expenses import tax
from expenses.allocations import replace_allocations
from expenses.storage import save_private
from expenses.models import (Attachment, Category, Department, EmployeeProfile, Expense, GSTType, ITCStatus, LegalEntity,
                             PaymentMethod, Project, Role, RoleAssignment, Status, TaxCode, Vendor, WorkflowEvent)

User = get_user_model()
PASSWORD = "Demo@1234"

ENTITIES = [
    ("ALPHA", "Alpha Tech", "Alpha Tech Private Limited", "33AAACA1234A1Z5", "33", "Chennai, Tamil Nadu"),
    ("BETA", "Beta Services", "Beta Services Private Limited", "29AAACB2345B1Z3", "29", "Bengaluru, Karnataka"),
    ("GAMMA", "Gamma Logistics", "Gamma Logistics LLP", "27AAACG3456C1Z1", "27", "Mumbai, Maharashtra"),
]
DEPARTMENTS = [("SALES", "Sales"), ("OPS", "Operations"), ("FIN", "Finance"), ("IT", "IT"), ("HR", "HR")]
PROJECTS = [("PRJ-ERP", "ERP rollout"), ("PRJ-EXP", "South expansion"), ("CC-HO", "Head office")]
CATEGORIES = [
    ("TRAVEL", "Travel", True, None), ("MEALS", "Meals & entertainment", True, Decimal("5000")),
    ("LODGING", "Lodging", True, None), ("OFFICE", "Office supplies", True, None),
    ("SOFTWARE", "Software & subscriptions", True, None), ("FUEL", "Fuel & conveyance", False, Decimal("3000")),
    ("TELECOM", "Phone & internet", True, None), ("TRAINING", "Training", True, None),
]
VENDORS = [
    ("V001", "Indigo Airlines", "07AAACI1234D1Z9", "07"), ("V002", "Taj Hotels", "33AAACT1234E1Z2", "33"),
    ("V003", "Amazon Business", "29AAACA9876F1Z4", "29"), ("V004", "Airtel", "33AAACB4567G1Z6", "33"),
    ("V005", "Zoho Corp", "33AAACZ7654H1Z8", "33"), ("V006", "Uber India", "27AAACU3456J1Z0", "27"),
    ("V007", "Saravana Bhavan", "33AAACS2345K1Z2", "33"), ("V008", "Udemy", "", ""),
]
PAYMENTS = [("CASH", "Cash"), ("CARD", "Corporate card"), ("UPI", "UPI"), ("BANK", "Bank transfer"),
            ("PERSONAL", "Personal (reimbursable)")]
USERS = [
    # username, first, last, emp code, dept, entity, roles [(role, entity_code|None, limit)]
    ("admin", "Asha", "Admin", "E001", "IT", "ALPHA", [(Role.SUPER_ADMIN, None, None)]),
    ("finance", "Farid", "Khan", "E002", "FIN", "ALPHA", [(Role.FINANCE_ADMIN, None, None)]),
    ("accountant", "Anitha", "Raj", "E003", "FIN", "ALPHA", [(Role.ACCOUNTANT, None, None)]),
    ("manager", "Mohan", "Kumar", "E004", "SALES", "ALPHA",
     [(Role.MANAGER, "ALPHA", Decimal("100000")), (Role.MANAGER, "BETA", Decimal("50000")),
      (Role.EMPLOYEE, "ALPHA", None)]),
    ("employee", "Priya", "Sundar", "E005", "SALES", "ALPHA", [(Role.EMPLOYEE, "ALPHA", None)]),
    ("employee2", "Ravi", "Shankar", "E006", "OPS", "BETA", [(Role.EMPLOYEE, "BETA", None)]),
    ("viewer", "Vani", "Iyer", "E007", "FIN", "ALPHA", [(Role.VIEWER, None, None)]),
    ("auditor", "Arun", "Das", "E008", "FIN", "ALPHA", [(Role.AUDITOR, None, None)]),
]
TITLES = {
    "TRAVEL": ["Flight to Delhi", "Flight to Mumbai", "Train to Coimbatore", "Client visit airfare"],
    "MEALS": ["Client lunch", "Team dinner", "Working lunch"],
    "LODGING": ["Hotel stay – client visit", "Hotel – conference"],
    "OFFICE": ["Printer cartridges", "Stationery", "Office chairs"],
    "SOFTWARE": ["Zoho One subscription", "Design tool licence", "Cloud hosting"],
    "FUEL": ["Cab to airport", "Fuel reimbursement", "Local conveyance"],
    "TELECOM": ["Broadband – October", "Mobile bill"],
    "TRAINING": ["Online course", "Workshop fees"],
}
CAT_VENDOR = {"TRAVEL": "V001", "MEALS": "V007", "LODGING": "V002", "OFFICE": "V003", "SOFTWARE": "V005",
              "FUEL": "V006", "TELECOM": "V004", "TRAINING": "V008"}


class Command(BaseCommand):
    help = "Seed master data, demo users and sample expenses."

    def add_arguments(self, parser):
        parser.add_argument("--no-expenses", action="store_true")
        parser.add_argument("--count", type=int, default=80)

    @transaction.atomic
    def handle(self, *args, **opts):
        ents = {}
        for code, name, legal, gstin, st, addr in ENTITIES:
            ents[code], _ = LegalEntity.objects.update_or_create(code=code, defaults=dict(
                name=name, legal_name=legal, gstin=gstin, state_code=st, address=addr))
        depts = {c: Department.objects.update_or_create(code=c, defaults={"name": n})[0] for c, n in DEPARTMENTS}
        projects = [Project.objects.update_or_create(code=c, defaults={"name": n})[0] for c, n in PROJECTS]
        cats = {}
        for c, n, receipt, limit in CATEGORIES:
            cats[c], _ = Category.objects.update_or_create(code=c, defaults=dict(name=n, receipt_required=receipt,
                                                                                 per_claim_limit=limit))
        vendors = {}
        for c, n, g, st in VENDORS:
            vendors[c], _ = Vendor.objects.update_or_create(code=c, defaults=dict(name=n, gstin=g, state_code=st))
        pays = [PaymentMethod.objects.update_or_create(code=c, defaults={"name": n})[0] for c, n in PAYMENTS]
        for rate in ("0", "5", "12", "18", "28"):
            TaxCode.objects.get_or_create(code=f"GST{rate}", effective_from=date(2017, 7, 1),
                                          defaults=dict(label=f"GST {rate}%", rate=Decimal(rate)))

        users = {}
        for username, first, last, emp, dept, ent, roles in USERS:
            u, created = User.objects.get_or_create(username=username, defaults=dict(
                first_name=first, last_name=last, email=f"{username}@example.com"))
            if created:
                u.set_password(PASSWORD)
            u.is_staff = username == "admin"
            u.is_superuser = username == "admin"
            u.save()
            EmployeeProfile.objects.update_or_create(user=u, defaults=dict(
                employee_code=emp, department=depts[dept], default_entity=ents[ent]))
            for role, ecode, limit in roles:
                RoleAssignment.objects.update_or_create(user=u, role=role, entity=ents.get(ecode) if ecode else None,
                                                        defaults={"approval_limit": limit})
            users[username] = u
        for name in ("employee", "employee2"):
            EmployeeProfile.objects.filter(user=users[name]).update(manager=users["manager"])
        self.stdout.write(self.style.SUCCESS(f"Master data and {len(users)} users ready (password: {PASSWORD})"))

        if opts["no_expenses"] or Expense.objects.exists():
            if Expense.objects.exists():
                self.stdout.write("Expenses already exist; sample expenses not added.")
            return
        self._expenses(opts["count"], ents, cats, vendors, pays, projects, users)

    def _expenses(self, count, ents, cats, vendors, pays, projects, users):
        rnd = random.Random(42)
        today = timezone.localdate()
        submitters = [users["employee"], users["employee2"], users["manager"]]
        flow = [Status.DRAFT, Status.SUBMITTED, Status.MANAGER_APPROVED, Status.FINANCE_VERIFIED,
                Status.FINANCE_VERIFIED, Status.FINANCE_VERIFIED, Status.RETURNED, Status.REJECTED]
        for i in range(count):
            sub = rnd.choice(submitters)
            prof = sub.profile
            ent = prof.default_entity
            ccode = rnd.choice(list(cats))
            cat = cats[ccode]
            vendor = vendors[CAT_VENDOR[ccode]]
            d = today - timedelta(days=rnd.randint(0, 330))
            taxable = Decimal(rnd.choice([450, 900, 1200, 2500, 4200, 7800, 12500, 18000, 26000, 41000]))
            if ccode == "MEALS":
                taxable = min(taxable, Decimal("4000"))
            if ccode == "FUEL":
                taxable = min(taxable, Decimal("2500"))
            rate = {"TRAVEL": 5, "MEALS": 5, "LODGING": 12, "FUEL": 5, "TRAINING": 18}.get(ccode, 18)
            exp = Expense(
                title=rnd.choice(TITLES[ccode]), description=f"{cat.name} for {prof.department.name}",
                expense_date=d, paying_entity=ent, submitter=sub, department=prof.department,
                project=rnd.choice(projects + [None, None]), category=cat, payment_method=rnd.choice(pays),
                vendor=vendor, vendor_name_snapshot=vendor.name, invoice_number=f"INV/{d:%y%m}/{1000 + i}",
                invoice_date=d, supplier_gstin=vendor.gstin, recipient_gstin=ent.gstin,
                supplier_state_code=vendor.state_code, place_of_supply_state_code=ent.state_code,
                gst_type="" if not vendor.gstin else "", gst_rate=Decimal(rate) if vendor.gstin else None,
                taxable_value=taxable, notes="Sample data",
            )
            if not vendor.gstin:
                exp.gst_type = GSTType.NON_GST
            exp.created_at = timezone.now()
            tax.apply_tax(exp, {})
            exp.save()
            if rnd.random() > 0.15:
                self._receipt(exp, sub)
            target = rnd.choice(flow) if d < today - timedelta(days=20) else rnd.choice(flow[:4])
            self._walk(exp, target, users)
            if exp.status == Status.FINANCE_VERIFIED and exp.total_gst:
                eligible = rnd.random() > 0.25
                exp.itc_status = ITCStatus.VERIFIED_ELIGIBLE if eligible else ITCStatus.INELIGIBLE
                exp.verified_itc_amount = exp.total_gst if eligible else Decimal("0")
                exp.itc_reviewer = users["accountant"]
                exp.itc_reviewed_at = timezone.now()
                exp.itc_reason = "Valid tax invoice on file" if eligible else "Blocked credit (sample)"
                exp.save()
            elif exp.status in (Status.MANAGER_APPROVED,) and exp.total_gst:
                exp.itc_status = ITCStatus.POTENTIALLY_ELIGIBLE
                exp.save()
            if i % 9 == 0 and ent.code == "ALPHA":
                other = ents["BETA"]
                replace_allocations(users["finance"], exp, [
                    {"beneficiary_entity": ent, "method": "PERCENT", "percent": Decimal("60")},
                    {"beneficiary_entity": other, "method": "PERCENT", "percent": Decimal("40"),
                     "recharge_amount": tax.q(exp.gross_amount * Decimal("0.4")), "recharge_status": "APPROVED",
                     "reference": f"IC-{exp.ref}"}])
        self.stdout.write(self.style.SUCCESS(f"{count} sample expenses created"))

    def _receipt(self, exp, user):
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A5)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, 540, f"SAMPLE RECEIPT – {exp.vendor_name_snapshot}")
        c.setFont("Helvetica", 10)
        for n, line in enumerate([f"Invoice: {exp.invoice_number}", f"Date: {exp.invoice_date}",
                                  f"Taxable: {exp.taxable_value}", f"GST: {exp.total_gst}",
                                  f"Total: {exp.currency} {exp.gross_amount}", "Demo data – not a real invoice"]):
            c.drawString(40, 510 - 16 * n, line)
        c.save()
        content = buf.getvalue()
        Attachment.objects.create(expense=exp, storage_key=save_private("attachments", "r.pdf", content),
                                  file_name=f"receipt-{exp.ref}.pdf", mime_type="application/pdf",
                                  size=len(content), sha256=hashlib.sha256(content).hexdigest(), uploaded_by=user)

    def _walk(self, exp, target, users):
        steps = {
            Status.DRAFT: [],
            Status.SUBMITTED: [("submit", Status.SUBMITTED, exp.submitter)],
            Status.MANAGER_APPROVED: [("submit", Status.SUBMITTED, exp.submitter),
                                      ("approve", Status.MANAGER_APPROVED, users["manager"])],
            Status.FINANCE_VERIFIED: [("submit", Status.SUBMITTED, exp.submitter),
                                      ("approve", Status.MANAGER_APPROVED, users["manager"]),
                                      ("verify", Status.FINANCE_VERIFIED, users["accountant"])],
            Status.RETURNED: [("submit", Status.SUBMITTED, exp.submitter),
                              ("return", Status.RETURNED, users["manager"])],
            Status.REJECTED: [("submit", Status.SUBMITTED, exp.submitter),
                              ("reject", Status.REJECTED, users["manager"])],
        }[target]
        prev = Status.DRAFT
        for action, to, actor in steps:
            if actor == exp.submitter and action != "submit":
                actor = users["admin"]
            WorkflowEvent.objects.create(expense=exp, actor=actor, action=action, from_state=prev, to_state=to,
                                         comment="Missing details" if action in ("return", "reject") else "")
            prev = to
        exp.status = target
        exp.save(update_fields=["status"])
