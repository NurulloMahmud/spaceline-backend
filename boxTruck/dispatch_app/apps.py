from django.apps import AppConfig


class DispatchAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dispatch_app"
    verbose_name = "BTM Dispatch"

    def ready(self) -> None:
        from . import signals  # noqa: F401
