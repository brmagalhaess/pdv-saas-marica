"""Asaas subscriptions. Payment is confirmed only from canonical provider state."""
import hmac
import re
from datetime import date
from decimal import Decimal
from urllib.parse import quote
from .base import ProviderClient

URLS={"sandbox":"https://api-sandbox.asaas.com/v3", "production":"https://api.asaas.com/v3"}

def verify_webhook_token(provided, expected):
    return (isinstance(provided,str) and isinstance(expected,str) and 32 <= len(expected) <= 255
            and hmac.compare_digest(provided,expected))

class Asaas(ProviderClient):
    def __init__(self,token,environment="sandbox",session=None):
        if environment not in URLS or not isinstance(token,str) or not token.strip():
            raise ValueError("Configure a chave Asaas e o ambiente")
        super().__init__(URLS[environment],session)
        self.headers={"access_token":token,"User-Agent":"PDV2/2.0","Accept":"application/json"}

    def _id(self,value):
        if not isinstance(value,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}",value):
            raise ValueError("Identificador do provedor inválido")
        return quote(value,safe="")

    def create_customer(self,name,tax_id,email,external_reference):
        tax_id=re.sub(r"\D","",str(tax_id))
        if not name or len(tax_id) not in (11,14) or not external_reference:
            raise ValueError("Nome, CPF/CNPJ e referência são obrigatórios")
        return self.request("POST","/customers",headers=self.headers,json={"name":name,"cpfCnpj":tax_id,
                            "email":email,"externalReference":str(external_reference)})

    def find_customers(self,external_reference):
        return self.request("GET","/customers",headers=self.headers,params={"externalReference":str(external_reference),"limit":100})

    def create_subscription(self,customer,price_cents,cycle,next_due_date,reference,billing_type="PIX"):
        self._id(customer)
        if type(price_cents) is not int or not 0 < price_cents < 10**9:
            raise ValueError("Preço inválido")
        if cycle not in ("MONTHLY","YEARLY") or billing_type not in ("PIX","BOLETO","UNDEFINED"):
            raise ValueError("Ciclo ou forma de pagamento inválidos")
        date.fromisoformat(str(next_due_date))
        return self.request("POST","/subscriptions",headers=self.headers,json={"customer":customer,
            "billingType":billing_type,"value":float(Decimal(price_cents)/100),"nextDueDate":str(next_due_date),
            "cycle":cycle,"externalReference":str(reference),"description":"Assinatura PDV2"})

    def get_payment(self,payment_id):
        return self.request("GET","/payments/"+self._id(payment_id),headers=self.headers)

    def get_subscription(self,subscription_id):
        return self.request("GET","/subscriptions/"+self._id(subscription_id),headers=self.headers)

    def list_subscription_payments(self,subscription_id):
        return self.request("GET","/subscriptions/"+self._id(subscription_id)+"/payments",headers=self.headers,params={"limit":100})

    @staticmethod
    def payment_state(payment):
        return {"RECEIVED":"paid","CONFIRMED":"paid","RECEIVED_IN_CASH":"paid",
                "OVERDUE":"overdue","REFUNDED":"refunded","CHARGEBACK_REQUESTED":"disputed",
                "CHARGEBACK_DISPUTE":"disputed","AWAITING_CHARGEBACK_REVERSAL":"disputed",
                "DELETED":"cancelled"}.get(payment.get("status"),"pending")
