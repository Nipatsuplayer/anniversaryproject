from django.apps import AppConfig


class AnniversaryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "anniversary"
    verbose_name = "Anniversary event"
    # tells BallsDex where the discord.py extension lives
    dpy_package = "anniversary.package"
