from django.urls import path

from .api_views import ConsumeTokenView

urlpatterns = [
    path("tokens/consume/", ConsumeTokenView.as_view(), name="tokens-consume"),
]
