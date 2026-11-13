from django.urls import path

from . import portal_auth, portal_views

app_name = "portal"

urlpatterns = [
    path("enter/<str:key>/", portal_auth.enter, name="enter"),
    path("login/", portal_auth.login, name="login"),
    path("register/", portal_auth.register, name="register"),
    path("pending/", portal_views.pending, name="pending"),
    path("logout/", portal_auth.logout, name="logout"),
    path("accept/", portal_views.accept, name="accept"),
    path("", portal_views.home, name="home"),
]
