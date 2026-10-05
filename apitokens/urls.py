from django.urls import path

from . import views

app_name = "apitokens"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("<int:pk>/toggle/", views.toggle_active, name="toggle"),
]
