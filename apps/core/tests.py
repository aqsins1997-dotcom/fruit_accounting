from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Store


class CoreNoAdminViewsTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="core-user", password="secret123")
        self.client = Client()
        self.client.force_login(self.user)

    def test_store_directory_page_renders(self):
        response = self.client.get(reverse("core:stores_page"))
        self.assertEqual(response.status_code, 200)

    def test_store_can_be_created_without_admin(self):
        response = self.client.post(
            reverse("core:stores_page"),
            {"name": "Новый магазин", "is_active": "on"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Store.objects.filter(name="Новый магазин").exists())
