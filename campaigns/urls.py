from django.urls import path

from campaigns import views

app_name = "campaigns"

urlpatterns = [
    path("t/<uuid:token>/open/", views.track_open, name="track-open"),
    path("t/<uuid:token>/click/", views.track_click, name="track-click"),
    path("t/<uuid:token>/cta/", views.track_cta, name="track-cta"),
    path("t/<uuid:token>/submit/", views.track_submit_attempt, name="track-submit"),
    path("t/<uuid:token>/report/", views.track_report, name="track-report"),
    path("l/<slug:landing_slug>/", views.landing, name="landing"),
]
