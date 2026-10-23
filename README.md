# Company Expense Management Dashboard

A mobile-responsive expense dashboard for a multi-entity Indian company, built from
*Company_Expense_Management_Dashboard_SRS_v1.0*.

- **Frontend:** HTML, CSS and vanilla JavaScript (Chart.js 4.4.1 bundled locally in `static/vendor/`)
- **Backend:** Python, Django 5.2, Django REST Framework
- **Database:** SQLite
- **Files:** Excel import (create + update), Excel export, CSV export, PDF register, Excel import template
- **Google Sheets sync:** pending. There is a stub at `/api/integrations/google-sheets/` and a disabled button on the Import page.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo         # master data, 8 demo users, 80 sample expenses
python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in. All demo users use the password `Demo@1234`:

| Username | Role | Scope |
|---|---|---|
| admin | Super Admin (also Django admin at `/admin/`) | All entities |
| finance | Finance Admin (can import) | All entities |
| accountant | Accountant / Finance reviewer | All entities |
| manager | Manager (approval limit ₹1,00,000 ALPHA, ₹50,000 BETA) + Employee | ALPHA, BETA |
| employee | Employee | ALPHA |
| employee2 | Employee | BETA |
| viewer | Management Viewer (read-only) | All entities |
| auditor | Auditor (read-only + audit log) | All entities |

Use `python manage.py seed_demo --no-expenses` for master data and users only.
Run the tests with `python manage.py test expenses`.

## What is in the app

**Dashboard** – KPIs (gross, taxable value, recorded GST, verified ITC, net cost after verified ITC,
pending manager / finance approvals, returned/rejected, missing receipts, duplicate suspects),
charts by month, category, paying entity, allocated entity, vendor, workflow status, employee and project.
Every KPI and chart bar drills down to the matching records. A "Records included" selector makes the
status rule explicit (all, submitted+, manager approved+, finance verified only).

**Expenses** – filtered, sortable list; detail drawer with invoice/GST breakdown, ITC review, intercompany
allocation editor, receipt upload (PDF/JPG/PNG, 5 MB, 5 files, content-sniffed), history timeline and
workflow buttons. Create/edit form calculates CGST/SGST or IGST from taxable value and rate, or accepts
tax amounts exactly as invoiced and flags any variance.

**Workflow** – Draft → Submitted → Manager Review → Manager Approved → Finance Verification → Finance Verified,
with Return, Reject, Cancel and a controlled Reopen. No self-approval, manager approval limits per entity,
receipts required by category, per-category claim limits, records locked after submission, optimistic
version check on edits, in-app notifications.

**Import** – upload `.xlsx` or `.csv` → preview (new / update / duplicate candidate / unchanged / rejected,
with row-level errors) → commit or discard. Updates match on `ExpenseRef` or `ExternalRecordID`, so an
Excel export can be edited and re-imported. The same file is skipped if already imported. Submitted or
approved records are never overwritten by an import. Heuristic duplicates (entity + vendor + normalised
invoice no. + date + amount) are imported and flagged, never silently dropped. Rejected rows download as
an error CSV. Formulas and macros are never executed.

**Reports** – Excel register (with Report info and By category sheets), PDF register (landscape A4,
repeated headers, page numbers, totals), CSV, intercompany allocation CSV, GST summary by entity,
period, tax type and ITC status. Exports use exactly the same filters and access scope as the screen.

**Audit** – append-only audit events for logins, failed logins, creates, edits (field diffs),
transitions, ITC reviews, allocations, imports, exports and admin changes to users, roles and master data.

**Administration** – Django admin (`/admin/`) for entities, departments, projects, categories,
vendors, payment methods, tax codes, users, profiles and role assignments. Delete is disabled for
master data and users; deactivate instead.

## API (all under `/api/`, session auth + CSRF)

| Endpoint | Purpose |
|---|---|
| `GET /me/` | Current user, roles, permissions |
| `GET/POST /expenses/`, `GET/PATCH/DELETE /expenses/{id}/` | Expenses (filters below) |
| `POST /expenses/{id}/transition/` | `{"action": "submit|approve|verify|return|reject|cancel|reopen|start_review|start_verification", "comment": ""}` |
| `POST /expenses/{id}/itc-review/` | ITC status, eligible amount, reason, reference |
| `PUT /expenses/{id}/allocations/` | Intercompany allocation lines |
| `POST /expenses/{id}/attachments/` | Receipt upload (multipart `file`) |
| `GET /dashboard/` | KPIs and breakdowns |
| `GET /exports/expenses.xlsx` / `.csv` / `.pdf`, `GET /exports/intercompany.csv` | Exports |
| `GET /imports-template.xlsx` | Import template + field guide |
| `GET/POST /imports/`, `GET /imports/{id}/rows/`, `POST /imports/{id}/commit/`, `POST /imports/{id}/discard/`, `GET /imports/{id}/errors.csv/` | Import runs |
| `GET /reports/gst-summary/` | GST summary |
| `GET /audit/` | Audit log |
| `GET /integrations/google-sheets/` | Stub: returns `{"status": "pending"}` |

Filters (list, dashboard, exports): `date_from`, `date_to`, `entity`, `department`, `project`, `category`,
`vendor`, `employee`, `payment_method`, `status` (comma list), `status_scope`
(`active|submitted|manager_approved|finance_verified|all`), `itc_status`, `gst_type`, `search`,
`month` (YYYY-MM), `duplicate=1`, `missing_receipt=1`, `tax_variance=1`, `intercompany=1`.

## Configuration

Environment variables: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG` (`0` in production), `DJANGO_ALLOWED_HOSTS`,
`SESSION_TIMEOUT_SECONDS`. Business limits (attachment size/count, import row limit, tax rounding
tolerance, self-approval) live in `EXPENSE_SETTINGS` in `config/settings.py`.

Receipts and uploaded import files are stored in `private_media/` and are only served through
access-checked views.

## Not yet done / known limits

- Google Sheets synchronisation (pending, stubbed). Scheduled cloud-folder polling (OneDrive/Drive) is not built; imports are manual upload.
- Notifications are in-app only (no email delivery).
- MFA, delegation/escalation of approvers, budgets vs actual, saved report presets and recurring expenses (SRS SHOULD/MAY items) are not built.
- SQLite suits the 11–50 user target on a single server. Before production: set `DJANGO_DEBUG=0`, a real secret key, HTTPS, run `collectstatic`, serve with gunicorn/uwsgi behind a web server, and schedule backups of `db.sqlite3` and `private_media/`.
- GST rules and ITC treatment must be confirmed by the company's tax advisor (SRS FR-GST-012). The app records and calculates; it does not decide eligibility.
