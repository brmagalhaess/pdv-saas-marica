import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from shared.licensing import MAX_LEASE_SECONDS,generate_keypair,sign_lease,verify_lease,LicenseError
from shared.releases import (sign_release,verify_release,validate_release,version_tuple,
                             load_release_private_key,load_release_public_key)

def body(**extra):
    return {'schema':1,'purpose':'pdv2-release','component':'terminal','channel':'stable','version':'2.1.0',
            'url':'https://downloads.example.test/terminal.zip','sha256':'a'*64,'size_bytes':100,**extra}

class OptionalManifestFieldTests(unittest.TestCase):
    def test_kind_and_min_version_are_optional_and_signed(self):
        private,public=generate_keypair()
        self.assertEqual(verify_release(sign_release(body(),private),public),body())
        for kind in ('bundle','installer'):
            full=body(kind=kind,min_version='2.0.0')
            signed=sign_release(full,private)
            self.assertEqual(verify_release(signed,public),full)
            signed['payload']['min_version']='1.0.0'
            with self.assertRaises(LicenseError):verify_release(signed,public)
        validate_release(body(min_version='2.1.0'))

    def test_invalid_optional_fields_are_rejected_when_present(self):
        for extra in ({'kind':'msi'},{'kind':''},{'kind':None},{'min_version':'2.0'},{'min_version':''},
                      {'min_version':None},{'min_version':200},{'min_version':'2.1.1'},{'min_version':'10.0.0'}):
            with self.assertRaises(LicenseError,msg=extra):validate_release(body(**extra))

    def test_version_tuple_orders_numerically(self):
        self.assertLess(version_tuple('2.9.0'),version_tuple('2.10.0'))
        for invalid in ('2.0','v2.0.0','2.0.0 ',None,2):
            with self.assertRaises(LicenseError):version_tuple(invalid)

class ReleaseKeyLoadingTests(unittest.TestCase):
    def setUp(self):
        self.license_private,self.license_public=generate_keypair()
        self.release_private,self.release_public=generate_keypair()
        self.clean={'PDV_LICENSE_PRIVATE_KEY':self.license_private,'PDV_LICENSE_PUBLIC_KEY':self.license_public,
                    'PDV_LICENSE_PRIVATE_KEY_FILE':'','PDV_LICENSE_PUBLIC_KEY_FILE':'',
                    'PDV_RELEASE_PRIVATE_KEY':'','PDV_RELEASE_PUBLIC_KEY':'',
                    'PDV_RELEASE_PRIVATE_KEY_FILE':'','PDV_RELEASE_PUBLIC_KEY_FILE':''}

    def env(self,**values):
        return patch.dict(os.environ,{**self.clean,**values})

    def test_fallback_to_license_keys(self):
        with self.env():
            self.assertEqual(load_release_private_key(),self.license_private)
            self.assertEqual(load_release_public_key(),self.license_public)

    def test_dedicated_keys_from_files(self):
        with tempfile.TemporaryDirectory() as directory:
            private=Path(directory,'release-private.pem');private.write_text(self.release_private)
            public=Path(directory,'release-public.pem');public.write_text(self.release_public)
            with self.env(PDV_RELEASE_PRIVATE_KEY_FILE=str(private),PDV_RELEASE_PUBLIC_KEY_FILE=str(public)):
                signed=sign_release(body(),load_release_private_key())
                self.assertEqual(verify_release(signed,load_release_public_key()),body())
                with self.assertRaises(LicenseError):verify_release(signed,self.license_public)
            with self.env(PDV_RELEASE_PRIVATE_KEY_FILE=str(private)):
                verify_release(sign_release(body(),load_release_private_key()),load_release_public_key())

    def test_inconsistent_configuration_is_an_error(self):
        for values in ({'PDV_RELEASE_PRIVATE_KEY':self.release_private,'PDV_RELEASE_PUBLIC_KEY':self.license_public},
                       {'PDV_RELEASE_PUBLIC_KEY':self.release_public},
                       {'PDV_RELEASE_PRIVATE_KEY':'garbage'},
                       {'PDV_RELEASE_PRIVATE_KEY_FILE':'/nonexistent/key.pem'}):
            with self.env(**values),self.assertRaises(LicenseError,msg=list(values)):load_release_public_key()

class LongLeaseCeilingTests(unittest.TestCase):
    def claims(self,seconds):
        return {'schema':1,'tenant_id':'t','shop_id':'s','terminal_id':'x','device_id':'d','issued_at':1000,
                'expires_at':1000+seconds,'features':[],'status':'active'}

    def test_ceiling_is_forty_five_days(self):
        private,public=generate_keypair()
        self.assertEqual(MAX_LEASE_SECONDS,45*86400)
        signed=sign_lease(self.claims(45*86400),private)
        self.assertEqual(verify_lease(signed,public,now=2000)['expires_at'],1000+45*86400)
        with self.assertRaises(LicenseError):sign_lease(self.claims(45*86400+1),private)
