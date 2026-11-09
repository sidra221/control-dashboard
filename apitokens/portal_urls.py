from django.urls import path

from . import portal_auth, portal_views

app_name = "portal"

urlpatterns = [
    path("enter/<str:key>/", portal_auth.enter, name="enter"),
    path("expired/", portal_auth.expired, name="expired"),
    path("logout/", portal_auth.logout, name="logout"),
    path("", portal_views.home, name="home"),
]
