from django.contrib import admin

from .models import (
    Client,
    DocumentSequence,
    InventoryProduct,
    Order,
    OrderWorkflowEvent,
    Quotation,
    QuotationItem,
)


class QuotationItemInline(admin.TabularInline):
    model = QuotationItem
    extra = 0


class OrderWorkflowEventInline(admin.TabularInline):
    model = OrderWorkflowEvent
    extra = 0
    readonly_fields = ("category", "from_status", "to_status", "comment", "changed_by", "created_at")
    can_delete = False


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("code", "client_name", "phone_number", "email")
    search_fields = ("code", "client_name", "phone_number", "email")


@admin.register(InventoryProduct)
class InventoryProductAdmin(admin.ModelAdmin):
    list_display = ("name", "quantity", "unit_price", "updated_at")
    search_fields = ("name",)


@admin.register(Quotation)
class QuotationAdmin(admin.ModelAdmin):
    list_display = ("quotation_id", "client_name", "status", "event_date", "total_estimated")
    list_filter = ("status", "event_date")
    search_fields = ("quotation_id", "client_name", "phone_number")
    inlines = [QuotationItemInline]


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("order_id", "status", "operational_status", "billing_status", "confirmed_at")
    list_filter = ("status", "operational_status", "billing_status", "confirmed_at")
    search_fields = ("order_id", "quotation__client_name", "quotation__phone_number")
    inlines = [OrderWorkflowEventInline]


@admin.register(DocumentSequence)
class DocumentSequenceAdmin(admin.ModelAdmin):
    list_display = ("key", "year_suffix", "last_value")
    list_filter = ("key", "year_suffix")
    search_fields = ("key", "year_suffix")
