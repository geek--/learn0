from django.contrib import admin

from campaigns.models import Campaign, CampaignRecipient, EmailEvent, Recipient


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "start_at", "end_at", "throttle_per_minute", "created_by")
    search_fields = ("name",)
    list_filter = ("start_at", "end_at")


@admin.register(Recipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "department", "created_at")
    search_fields = ("email", "full_name", "department")


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = (
        "campaign",
        "recipient",
        "status",
        "sent_at",
        "opened_at",
        "clicked_at",
        "click_count",
        "landing_viewed_at",
        "landing_view_count",
    )
    list_filter = ("status", "opened_at", "clicked_at")
    search_fields = ("campaign__name", "recipient__email")

    def save_model(self, request, obj, form, change) -> None:
        obj.full_clean()
        super().save_model(request, obj, form, change)


@admin.register(EmailEvent)
class EmailEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "recipient",
        "device_type",
        "os_family",
        "browser_family",
        "email_client_hint",
        "created_at",
    )
    list_filter = ("event_type", "created_at")
    search_fields = ("recipient__recipient__email", "recipient__campaign__name")
