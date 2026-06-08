from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from api.models import Client


DEFAULT_CLIENTS = [
    {
        "client_name": "Grupo Marea Eventos",
        "contact_person": "Fernanda Solis",
        "phone_number": "+52 55 1345 8890",
        "email": "fernanda@mareaeventos.mx",
        "address": "Av. Insurgentes Sur 845, Ciudad de Mexico",
    },
    {
        "client_name": "Constructora Del Valle",
        "contact_person": "Ramon Cardenas",
        "phone_number": "+52 81 2784 4300",
        "email": "ramon@cdelvalle.com",
        "address": "Blvd. Diaz Ordaz 1640, Monterrey",
    },
    {
        "client_name": "Hotel Costa Azul",
        "contact_person": "Elena Prieto",
        "phone_number": "+52 624 105 4471",
        "email": "elena@costaazulhotel.com",
        "address": "Paseo del Mar 120, Los Cabos",
    },
    {
        "client_name": "Producciones Nova",
        "contact_person": "Javier Ruiz",
        "phone_number": "+52 33 2190 7744",
        "email": "javier@produccionesnova.mx",
        "address": "Av. Vallarta 2890, Guadalajara",
    },
    {
        "client_name": "Expo Modular MX",
        "contact_person": "Lucia Herrera",
        "phone_number": "+52 55 2266 1180",
        "email": "lucia@expomodular.mx",
        "address": "Calle 5 de Febrero 340, Queretaro",
    },
    {
        "client_name": "Stellar Corporate",
        "contact_person": "Mauricio Vega",
        "phone_number": "+52 55 4477 2299",
        "email": "mauricio@stellarcorp.io",
        "address": "Av. Santa Fe 495, Ciudad de Mexico",
    },
]


class Command(BaseCommand):
    help = "Crea un usuario administrador de desarrollo y clientes demo."

    def handle(self, *args, **options):
        user_model = get_user_model()
        admin_user, created = user_model.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@orderflow.com",
                "first_name": "Administrador",
                "last_name": "OrderFlow",
                "is_staff": True,
                "is_superuser": True,
            },
        )

        if created:
            admin_user.set_password("OrderFlow123")
            admin_user.save()
            self.stdout.write(self.style.SUCCESS("Usuario admin creado."))
        else:
            self.stdout.write("El usuario admin ya existia.")

        created_clients = 0

        for client_data in DEFAULT_CLIENTS:
            _, was_created = Client.objects.get_or_create(
                name_key=client_data["client_name"].strip().lower(),
                defaults=client_data,
            )

            if was_created:
                created_clients += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Clientes demo creados en esta corrida: {created_clients}.",
            )
        )
