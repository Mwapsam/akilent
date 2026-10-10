"""G — BM25 knowledge retrieval: add knowledge_version and bm25_enabled to AISettings."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0009_knowledge_review"),
    ]

    operations = [
        migrations.AddField(
            model_name="aisettings",
            name="knowledge_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="aisettings",
            name="bm25_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
