import json

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from catalog.models import Product


@require_http_methods(["GET", "HEAD", "POST"])
def products(request):
    if request.method == "POST":
        return create_product(request)
    queryset = Product.objects.all()
    if request.GET.get("out_of_stock") == "1":
        queryset = queryset.filter(stock=0)
    return JsonResponse(
        {"products": list(queryset.values("sku", "name", "price", "stock"))}
    )


def create_product(request):
    if request.content_type != "application/json":
        return JsonResponse({"error": "expected application/json"}, status=415)
    try:
        data = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    fields = ("sku", "name", "price", "stock")
    if not isinstance(data, dict) or set(data) - set(fields):
        return JsonResponse({"error": "expected a product object"}, status=400)
    if (
        not all(
            isinstance(data.get(key), str) and data[key].strip()
            for key in ("sku", "name")
        )
        or type(data.get("price")) is not int
        or type(data.get("stock", 0)) is not int
    ):
        return JsonResponse({"error": "invalid product fields"}, status=400)

    product = Product(**data)
    try:
        product.full_clean(validate_unique=False)
    except ValidationError as error:
        return JsonResponse({"errors": error.message_dict}, status=400)
    product, created = Product.objects.get_or_create(
        sku=product.sku,
        defaults={"name": product.name, "price": product.price, "stock": product.stock},
    )
    if not created:
        return JsonResponse({"error": "SKU already exists"}, status=400)
    return JsonResponse({key: getattr(product, key) for key in fields}, status=201)
