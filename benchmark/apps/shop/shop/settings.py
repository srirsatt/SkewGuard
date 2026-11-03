"""Local-only shop fixture settings; SkewGuard injects its sandbox connection."""

import os

SECRET_KEY = "skewguard-shop-fixture-only"
INSTALLED_APPS = ["catalog"]
ROOT_URLCONF = "shop.urls"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("PGDATABASE", "skewguard_shop"),
        "USER": os.environ.get("PGUSER", "skewguard"),
        "PASSWORD": os.environ.get("PGPASSWORD", ""),
        "HOST": os.environ.get("PGHOST", "127.0.0.1"),
        "PORT": os.environ.get("PGPORT", "5432"),
    }
}
