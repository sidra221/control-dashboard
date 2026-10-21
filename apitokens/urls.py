from django.urls import path

from . import views

app_name = "apitokens"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("set-language/", views.set_language, name="set-language"),
    path("<int:pk>/toggle/", views.toggle_active, name="toggle"),
    path("<int:pk>/catalog/", views.catalog, name="catalog"),
    path("<int:pk>/shopify/", views.shopify_connect, name="shopify-connect"),
]
