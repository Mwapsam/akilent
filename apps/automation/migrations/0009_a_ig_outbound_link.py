import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("automation", "0008_a0_workflow_interaction"),
        ("instagram", "0013_a_outbound_quick_replies"),
    ]

    operations = [
        migrations.AddField(
            model_name="workflowsteprun",
            name="ig_outbound_message",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="workflow_step_runs",
                to="instagram.outboundmessage",
            ),
        ),
    ]
