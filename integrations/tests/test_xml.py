import unittest
from unittest.mock import Mock
from integrations.fiscal import FocusNFe
from integrations.base import ProviderError

class FiscalXMLTests(unittest.TestCase):
    def test_xml_origin_key_and_protocol_are_checked_before_archiving(self):
        key='1'*44;protocol='123456789'
        xml=(f'<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{key}"/>'
             f'</NFe><protNFe><infProt><chNFe>{key}</chNFe><nProt>{protocol}</nProt><cStat>100</cStat>'
             '</infProt></protNFe></nfeProc>').encode()
        session=Mock();response=Mock(status_code=200);response.iter_content.return_value=[xml]
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        session.get.return_value=context
        provider=FocusNFe('test-token',session=session)
        self.assertEqual(provider.download_xml('/arquivos/nfce.xml',key,protocol),xml)
        with self.assertRaises(ProviderError):provider.download_xml('/arquivos/nfce.xml','2'*44,protocol)
        before=session.get.call_count
        for path in ('https://attacker.test/a','//attacker.test/a','http://homologacao.focusnfe.com.br/a'):
            with self.assertRaises(ProviderError):provider.download_xml(path,key,protocol)
        self.assertEqual(session.get.call_count,before)
        response.iter_content.return_value=[b'<!DOCTYPE root [<!ENTITY x "y">]><root/>']
        with self.assertRaises(ProviderError):provider.download_xml('/a',key,protocol)
