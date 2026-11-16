import copy
import unittest
from shared.licensing import generate_keypair, sign_lease, verify_lease, LicenseError
from shared.security import hash_pin, verify_pin

class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.private, self.public = generate_keypair()
        self.claims = {"schema":1,"tenant_id":"tenant","shop_id":"shop","terminal_id":"pos","device_id":"device",
                       "issued_at":1700000000,"expires_at":1700604800,"features":["pos"],"status":"active"}
        self.lease = sign_lease(self.claims, self.private)

    def test_device_bound_and_tamper_resistant(self):
        self.assertEqual(verify_lease(self.lease,self.public,{"device_id":"device"},now=1700000010),self.claims)
        with self.assertRaises(LicenseError):
            verify_lease(self.lease,self.public,{"device_id":"other"},now=1700000010)
        tampered = copy.deepcopy(self.lease)
        tampered["payload"]["expires_at"] += 1
        with self.assertRaises(LicenseError):
            verify_lease(tampered,self.public,now=1700000010)
        _, other = generate_keypair()
        with self.assertRaises(LicenseError):
            verify_lease(self.lease,other,now=1700000010)

    def test_expiration_clock_rollback_and_max_offline_window(self):
        with self.assertRaises(LicenseError):
            verify_lease(self.lease,self.public,now=self.claims["expires_at"])
        with self.assertRaises(LicenseError):
            verify_lease(self.lease,self.public,now=1700000100,last_seen=1700001000)
        bad = dict(self.claims, expires_at=1700000000+45*86400+1)
        with self.assertRaises(LicenseError): sign_lease(bad,self.private)

    def test_pin_hash_is_salted_and_verifies(self):
        a,b=hash_pin("123987"),hash_pin("123987")
        self.assertNotEqual(a,b)
        self.assertTrue(verify_pin("123987",a))
        self.assertFalse(verify_pin("999999",a))
        self.assertFalse(verify_pin("123987","invalid"))
        with self.assertRaises(ValueError): hash_pin("123")
