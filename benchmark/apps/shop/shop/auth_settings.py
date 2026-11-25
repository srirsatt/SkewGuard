"""Shop settings for endpoints that use Django's session authentication."""

from .settings import *  # noqa: F403
from .settings import INSTALLED_APPS as SHOP_APPS

INSTALLED_APPS = [
    *SHOP_APPS,
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]
ROOT_URLCONF = "shop.auth_urls"
