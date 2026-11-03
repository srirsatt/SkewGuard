"""Head migration for the shop's breaking price-to-amount rename case."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.RenameField(
            model_name="product", old_name="price", new_name="amount"
        ),
        migrations.AlterModelOptions(
            name="product", options={"ordering": ["amount", "pk"]}
        ),
    ]
