from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0003_purchase_inventory_p_date_62d889_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchase",
            name="deleted_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Удалена",
            ),
        ),
    ]
