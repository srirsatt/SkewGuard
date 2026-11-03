"""Head migration for the shop's safe nullable-column addition case."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="product", name="notes", field=models.TextField(null=True)
        )
    ]
