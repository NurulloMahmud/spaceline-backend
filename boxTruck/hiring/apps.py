from django.apps import AppConfig


class HiringConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'hiring'

    def ready(self) -> None:
        # The live hiring board: driver changes are announced to open boards.
        from . import signals

        signals.connect_load_signals()
