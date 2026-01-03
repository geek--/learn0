from django.urls import path

from campaigns import views

app_name = "campaigns"

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboardv2/", views.dashboard_v2, name="dashboard-v2"),
    path("t/open/<uuid:token>/", views.track_open, name="track-open"),
    path("t/click/<uuid:token>/", views.track_click, name="track-click"),
    path("t/cta/<uuid:token>/", views.track_cta, name="track-cta"),
    path("t/submit/<uuid:token>/", views.track_submit_attempt, name="track-submit"),
    path("t/report/<uuid:token>/", views.track_report, name="track-report"),
    path("t/landing/<uuid:token>/", views.track_landing_view, name="track-landing"),
    path("l/<slug:landing_slug>/", views.landing, name="landing"),
]
