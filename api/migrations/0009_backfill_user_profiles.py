from django.db import migrations


def create_missing_profiles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("api", "UserProfile")

    for user in User.objects.all():
        role = "admin" if user.is_staff else "ventas"
        UserProfile.objects.get_or_create(user=user, defaults={"role": role})


def remove_profiles(apps, schema_editor):
    UserProfile = apps.get_model("api", "UserProfile")
    UserProfile.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0008_userprofile"),
    ]

    operations = [
        migrations.RunPython(create_missing_profiles, remove_profiles),
    ]
