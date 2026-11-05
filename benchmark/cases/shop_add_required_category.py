"""A Python default backfills old rows but supplies no default for old-code SQL."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.AddField(
            model_name="product",
            name="category",
            field=models.CharField(max_length=32, default="general"),
        )
    ]
