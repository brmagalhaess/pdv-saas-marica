"""Release manifests: dedicated signing key with fallback, optional kind/min_version and ?current filter."""
import json
import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase

from shared.licensing import LicenseError, generate_keypair
from shared.releases import verify_release
from .models import ProductRelease

CURRENT = '/api/v1/ops/releases/current'


class ReleaseKeyTests(TestCase):
    def setUp(self):
        self.license_private, self.license_public = generate_keypair()
        self.release_private, self.release_public = generate_keypair()
        env = patch.dict(os.environ, {'PDV_LICENSE_PRIVATE_KEY': self.license_private, 'PDV_LICENSE_PUBLIC_KEY': self.license_public,
                                      'PDV_LICENSE_PRIVATE_KEY_FILE': '', 'PDV_LICENSE_PUBLIC_KEY_FILE': '',
                                      'PDV_RELEASE_PRIVATE_KEY': self.release_private, 'PDV_RELEASE_PUBLIC_KEY': self.release_public,
                                      'PDV_RELEASE_PRIVATE_KEY_FILE': '', 'PDV_RELEASE_PUBLIC_KEY_FILE': ''})
        env.start()
        self.addCleanup(env.stop)
        user = get_user_model().objects.create_user(username='ops@example.test', email='ops@example.test',
                                                    password='Test-safe72!', is_staff=True)
        user.groups.add(Group.objects.get(name='platform_ops'))
        self.client.force_login(user)
        self.body = {'component': 'terminal', 'channel': 'stable', 'version': '2.1.0',
                     'url': 'https://updates.example.test/pdv2-2.1.0.zip', 'sha256': 'a' * 64, 'size_bytes': 1000}

    def publish(self, **changes):
        return self.client.post('/api/v1/ops/releases', json.dumps({**self.body, **changes}), content_type='application/json')

    def release(self, version, **fields):
        return ProductRelease.objects.create(component='terminal', channel='stable', version=version,
                                             url='https://updates.example.test/t.zip', sha256='b' * 64, size_bytes=10, **fields)

    def test_current_is_signed_with_the_release_key(self):
        self.release('2.0.1')
        data = self.client.get(CURRENT).json()
        self.assertEqual(data['public_key'], self.release_public)
        self.assertEqual(verify_release(data['release'], self.release_public)['version'], '2.0.1')
        with self.assertRaises(LicenseError):
            verify_release(data['release'], self.license_public)

    def test_falls_back_to_license_key_when_release_key_is_not_configured(self):
        self.release('2.0.1')
        with patch.dict(os.environ, {'PDV_RELEASE_PRIVATE_KEY': '', 'PDV_RELEASE_PUBLIC_KEY': ''}):
            data = self.client.get(CURRENT).json()
        self.assertEqual(data['public_key'], self.license_public)
        verify_release(data['release'], self.license_public)

    def test_public_key_is_derived_when_only_the_private_release_key_is_set(self):
        self.release('2.0.1')
        with patch.dict(os.environ, {'PDV_RELEASE_PUBLIC_KEY': ''}):
            data = self.client.get(CURRENT).json()
        verify_release(data['release'], self.release_public)
        self.assertEqual(data['public_key'].strip(), self.release_public.strip())

    def test_broken_release_key_configuration_fails_closed(self):
        self.release('2.0.1')
        for broken in ({'PDV_RELEASE_PUBLIC_KEY': self.license_public},
                       {'PDV_RELEASE_PRIVATE_KEY': 'not a key'},
                       {'PDV_RELEASE_PRIVATE_KEY': ''},
                       {'PDV_RELEASE_PRIVATE_KEY_FILE': '/nonexistent/release-private.pem'}):
            with patch.dict(os.environ, broken):
                self.assertEqual(self.client.get(CURRENT).status_code, 503, broken)

    def test_publish_accepts_kind_and_min_version(self):
        response = self.publish(kind='bundle', min_version='2.0.0')
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response.json()['kind'], response.json()['min_version']), ('bundle', '2.0.0'))
        payload = verify_release(self.client.get(CURRENT).json()['release'], self.release_public)
        self.assertEqual((payload['kind'], payload['min_version']), ('bundle', '2.0.0'))
        listed = self.client.get('/api/v1/ops/releases').json()['results'][0]
        self.assertEqual((listed['kind'], listed['min_version']), ('bundle', '2.0.0'))

    def test_publish_without_optional_fields_keeps_the_old_manifest(self):
        response = self.publish(version='2.2.0', kind='', min_version='')
        self.assertEqual(response.status_code, 201, response.content)
        payload = self.client.get(CURRENT).json()['release']['payload']
        self.assertNotIn('kind', payload)
        self.assertNotIn('min_version', payload)

    def test_publish_rejects_invalid_optional_fields(self):
        for changes in ({'kind': 'msi'}, {'kind': 1}, {'min_version': '2.0'}, {'min_version': 'abc'},
                        {'min_version': 2}, {'min_version': '2.1.1'}):
            response = self.publish(**changes)
            self.assertEqual(response.status_code, 400, (changes, response.content))
        self.assertEqual(ProductRelease.objects.count(), 0)

    def test_current_parameter_hides_versions_the_terminal_cannot_install(self):
        self.release('2.1.0')
        self.release('2.2.0', kind='bundle', min_version='2.1.0')
        self.release('3.0.0', kind='installer', min_version='2.2.0')
        version = lambda query: (self.client.get(CURRENT + query).json()['release'] or {}).get('payload', {}).get('version')
        self.assertEqual(version(''), '3.0.0')
        self.assertEqual(version('?current=2.0.0'), '2.1.0')
        self.assertEqual(version('?current=2.1.0'), '2.2.0')
        self.assertEqual(version('?current=2.10.0'), '3.0.0')
        self.assertEqual(version('?component=terminal&channel=stable&current=2.2.0'), '3.0.0')
        ProductRelease.objects.filter(version='2.1.0').update(active=False)
        self.assertIsNone(self.client.get(CURRENT + '?current=2.0.0').json()['release'])
        for invalid in ('?current=2.0', '?current=', '?current=a.b.c'):
            self.assertEqual(self.client.get(CURRENT + invalid).status_code, 400, invalid)
