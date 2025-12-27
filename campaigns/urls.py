from django.urls import path

from campaigns import views

app_name = "campaigns"

urlpatterns = [
    path("t/<uuid:token>/open/", views.track_open, name="track-open"),
    path("t/<uuid:token>/click/", views.track_click, name="track-click"),
    path("l/<slug:landing_slug>/", views.landing, name="landing"),
]
