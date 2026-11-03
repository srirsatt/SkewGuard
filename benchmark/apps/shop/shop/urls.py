from catalog.views import products
from django.urls import path

urlpatterns = [path("products/", products, name="products")]
