from django.http import JsonResponse
from django.urls import path
from django.views.decorators.http import require_http_methods

from .urls import urlpatterns as shop_urls


@require_http_methods(["GET", "POST"])
def account(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "sign in required"}, status=403)
    return JsonResponse({"username": request.user.get_username()})


urlpatterns = [*shop_urls, path("account/", account, name="account")]
