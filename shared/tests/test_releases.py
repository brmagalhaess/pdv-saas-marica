import copy
import unittest
from shared.licensing import generate_keypair,LicenseError
from shared.releases import sign_release,verify_release

class ReleaseTests(unittest.TestCase):
    def test_manifest_signature_binds_package_and_channel(self):
        private,public=generate_keypair()
        body={'schema':1,'purpose':'pdv2-release','component':'terminal','channel':'pilot','version':'2.0.0',
              'url':'https://downloads.example.test/terminal.zip','sha256':'a'*64,'size_bytes':100,'notes':''}
        signed=sign_release(body,private)
        self.assertEqual(verify_release(signed,public),body)
        changed=copy.deepcopy(signed);changed['payload']['channel']='stable'
        with self.assertRaises(LicenseError):verify_release(changed,public)
        _,wrong=generate_keypair()
        with self.assertRaises(LicenseError):verify_release(signed,wrong)
        for value in ('http://example.test/a','https://user:pass@example.test/a'):
            body['url']=value
            with self.assertRaises(LicenseError):sign_release(body,private)
