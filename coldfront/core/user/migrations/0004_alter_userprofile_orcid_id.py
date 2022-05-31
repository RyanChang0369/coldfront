# Generated by Django 3.2.13 on 2022-05-31 21:21

import coldfront.core.user.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('user', '0003_alter_userprofile_orcid_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='orcid_id',
            field=models.CharField(max_length=19, null=True, validators=[coldfront.core.user.models.validate_orcid]),
        ),
    ]
