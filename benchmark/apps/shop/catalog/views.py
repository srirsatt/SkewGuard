from django.http import JsonResponse

from catalog.models import Product


def products(request):
    return JsonResponse(
        {"products": list(Product.objects.values("sku", "name", "price", "stock"))}
    )
