from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0006_backfill_sale_item_batches"),
    ]

    operations = [
        migrations.AddField(
            model_name="sale",
            name="deleted_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="Удалена",
            ),
        ),
    ]
