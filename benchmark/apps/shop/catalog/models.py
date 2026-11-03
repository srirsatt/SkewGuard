from django.db import models


class Product(models.Model):
    sku = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    price = models.IntegerField()
    stock = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["price", "pk"]
