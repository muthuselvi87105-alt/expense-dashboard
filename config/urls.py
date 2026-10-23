from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = "Expense Management – Administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="expenses/login.html", redirect_authenticated_user=True,
                                                extra_context={"show_demo": settings.DEBUG}),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("expenses.urls")),
]
