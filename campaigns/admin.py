from django.contrib import admin

from campaigns.models import Campaign, CampaignRecipient, Recipient


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
    list_display = ("campaign", "recipient", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("campaign__name", "recipient__email")
