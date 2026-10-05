from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Control — kie.ai Tokens"
admin.site.site_title = "Control"
admin.site.index_title = "لوحة التحكم"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apitokens.api_urls")),
    path("", include("apitokens.urls")),
]
