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

    def _provider(self, xml):
        session=Mock();response=Mock(status_code=200);response.iter_content.return_value=[xml]
        context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
        session.get.return_value=context
        return FocusNFe('test-token',session=session)

    def test_cancel_event_and_inutilization_xml_are_checked(self):
        key='3'*44
        event=(f'<procEventoNFe xmlns="http://www.portalfiscal.inf.br/nfe"><evento><infEvento><chNFe>{key}</chNFe>'
               '<tpEvento>110111</tpEvento></infEvento></evento><retEvento><infEvento><cStat>135</cStat>'
               '</infEvento></retEvento></procEventoNFe>').encode()
        self.assertEqual(self._provider(event).download_event_xml('/a.xml',key),event)
        with self.assertRaises(ProviderError):self._provider(event).download_event_xml('/a.xml','4'*44)
        with self.assertRaises(ProviderError):self._provider(event.replace(b'135',b'573')).download_event_xml('/a.xml',key)
        inut=(b'<procInutNFe xmlns="http://www.portalfiscal.inf.br/nfe"><retInutNFe><infInut><cStat>102</cStat>'
              b'</infInut></retInutNFe></procInutNFe>')
        self.assertEqual(self._provider(inut).download_inutilization_xml('/i.xml'),inut)
        with self.assertRaises(ProviderError):self._provider(inut.replace(b'102',b'241')).download_inutilization_xml('/i.xml')

    def test_production_mutations_need_the_platform_flag(self):
        import os
        from unittest.mock import patch
        session=Mock()
        provider=FocusNFe('test-token','production',session=session)
        with patch.dict(os.environ,{'PDV_ALLOW_LIVE_FISCAL':'0'}):
            with self.assertRaises(ProviderError):provider.inutilize('11222333000181',1,1,2,'Falha no sistema de emissao')
            with self.assertRaises(ProviderError):provider.submit('ref-1',{'items':[{}]})
        session.request.assert_not_called()
