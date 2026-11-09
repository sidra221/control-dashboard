from django.urls import path

from . import views

app_name = "apitokens"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("set-language/", views.set_language, name="set-language"),
    path("<int:pk>/toggle/", views.toggle_active, name="toggle"),
    path("<int:pk>/portal-link/", views.portal_link, name="portal-link"),
    path("<int:pk>/catalog/", views.catalog, name="catalog"),
    path("<int:pk>/shopify/", views.shopify_connect, name="shopify-connect"),
    path("<int:pk>/discounts/", views.discount_codes, name="discount-codes"),
    path("<int:pk>/conversations/", views.conversations, name="conversations"),
    path("<int:pk>/tryon-stats/", views.tryon_stats, name="tryon-stats"),
    path("salla/", views.salla_integration, name="salla"),
    path("zid/", views.zid_integration, name="zid"),
]
