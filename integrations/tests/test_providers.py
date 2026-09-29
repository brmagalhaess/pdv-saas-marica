import unittest
from unittest.mock import Mock
import requests
from integrations.base import ProviderError
from integrations.fiscal import FocusNFe
from integrations.billing import Asaas,verify_webhook_token

class ProviderTests(unittest.TestCase):
    def response(self,body,status=200):
        r=Mock();r.status_code=status;r.json.return_value=body;return r

    def test_fiscal_reference_reused_and_no_false_authorization(self):
        session=Mock();session.request.return_value=self.response({"status":"processando_autorizacao"})
        provider=FocusNFe("test-token",session=session)
        result=provider.submit("sale-1",{"items":[{"codigo_produto":"1"}]})
        self.assertEqual(provider.normalized_status(result),"processing")
        self.assertEqual(session.request.call_args.kwargs["params"],{"ref":"sale-1"})
        self.assertEqual(session.request.call_args.kwargs["auth"],("test-token",""))
        session.request.side_effect=requests.Timeout()
        with self.assertRaises(ProviderError) as failure:provider.submit("sale-1",{"items":[{}]})
        self.assertTrue(failure.exception.outcome_unknown)
        self.assertEqual(session.request.call_count,2)

    def test_webhook_auth_and_provider_status(self):
        token="a"*40
        self.assertTrue(verify_webhook_token(token,token))
        self.assertFalse(verify_webhook_token("forged",token))
        self.assertFalse(verify_webhook_token("", ""))
        self.assertEqual(Asaas.payment_state({"status":"CONFIRMED"}),"paid")
        self.assertEqual(Asaas.payment_state({"status":"PENDING"}),"pending")

    def test_subscription_does_not_imply_payment_and_validates_input(self):
        session=Mock();session.request.return_value=self.response({"id":"sub_1","status":"ACTIVE"})
        provider=Asaas("test-token",session=session)
        result=provider.create_subscription("cus_1",14900,"MONTHLY","2026-10-15","tenant_1")
        self.assertEqual(result["id"],"sub_1")
        self.assertEqual(Asaas.payment_state(result),"pending")
        self.assertEqual(session.request.call_args.kwargs["json"]["value"],149)
        with self.assertRaises(ValueError):provider.get_payment("../../private")
