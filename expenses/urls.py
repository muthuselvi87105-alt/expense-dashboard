from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register("expenses", views.ExpenseViewSet, basename="expense")
router.register("entities", views.EntityViewSet, basename="entity")
router.register("departments", views.DepartmentViewSet, basename="department")
router.register("projects", views.ProjectViewSet, basename="project")
router.register("categories", views.CategoryViewSet, basename="category")
router.register("vendors", views.VendorViewSet, basename="vendor")
router.register("payment-methods", views.PaymentMethodViewSet, basename="payment-method")
router.register("employees", views.EmployeeViewSet, basename="employee")
router.register("imports", views.SyncRunViewSet, basename="import")
router.register("notifications", views.NotificationViewSet, basename="notification")

api = [
    path("me/", views.me),
    path("dashboard/", views.dashboard),
    path("exports/expenses.xlsx", views.export_xlsx),
    path("exports/expenses.csv", views.export_csv),
    path("exports/expenses.pdf", views.export_pdf),
    path("exports/intercompany.csv", views.intercompany_csv),
    path("imports-template.xlsx", views.import_template),
    path("reports/gst-summary/", views.gst_summary),
    path("integrations/google-sheets/", views.google_sheets),
    path("audit/", views.audit_log),
    path("", include(router.urls)),
]

urlpatterns = [
    path("", views.app_page, name="app"),
    path("attachments/<uuid:pk>/", views.attachment_download, name="attachment-download"),
    path("api/", include(api)),
]
