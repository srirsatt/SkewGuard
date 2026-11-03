from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(primary_key=True)),
                ("sku", models.CharField(max_length=32, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("price", models.IntegerField()),
                ("stock", models.PositiveIntegerField(default=0)),
            ],
            options={"ordering": ["price", "pk"]},
        )
    ]
