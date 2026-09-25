from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ai_assistant", "0016_ai_instruction_profile_skill_snapshot")]

    operations = [
        migrations.RemoveConstraint(
            model_name="aiinstructionprofile",
            name="unique_ai_instruction_profile_per_owner",
        ),
        migrations.AddConstraint(
            model_name="aiinstructionprofile",
            constraint=models.UniqueConstraint(
                fields=("owner", "product"),
                name="unique_ai_instruction_profile_per_project",
            ),
        ),
    ]
