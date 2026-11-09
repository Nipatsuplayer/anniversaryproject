from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("anniversary", "0002_task_min_rarity_alter_reward_reward_type_and_more")]

    operations = [
        migrations.RenameField(model_name="task", old_name="min_rarity", new_name="max_rarity"),
        migrations.AlterField(
            model_name="task",
            name="max_rarity",
            field=models.FloatField(
                blank=True,
                help_text=(
                    "Only count catches where the collectible's rarity weight is at most this value (lower is rarer)."
                ),
                null=True,
            ),
        ),
    ]
