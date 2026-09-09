"""reports URLs — the §4n paths verbatim, mounted at api/v1/ (M7-15)."""

from django.urls import path

from apps.reports.api import report_edit, report_vet_review, saloon_reports

app_name = "reports"

urlpatterns = [
    path("saloons/<int:saloon_id>/reports/", saloon_reports, name="saloon-reports"),
    path("reports/<int:report_id>/", report_edit, name="report-edit"),
    path("reports/<int:report_id>/vet-review/", report_vet_review, name="report-vet-review"),
]
