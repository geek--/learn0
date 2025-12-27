from django.contrib import admin

from auditing.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("action", "actor__username")
