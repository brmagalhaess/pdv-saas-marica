"""CrownTech brand, landing, login, offline page and PWA manifest regressions."""
import html
import json
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings
from django.test import SimpleTestCase, TestCase

WEB = Path(settings.WEB_DIR)
ASSETS = WEB / "assets"
VENDOR_URL = "https://crowntech.com.br"
SITE_URL = "https://pdv.crowntech.com.br/"
HEDGES = ("acesso antecipado", "nome provisório", "de referência", "validação técnica")
# Contact and registration data that nobody supplied must never be made up in the pages.
INVENTED_CONTACT = re.compile(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|wa\.me|tel:|mailto:|\(\d{2}\)\s?\d{4}", re.I)


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def visible_text(markup):
    markup = re.sub(r"<(script|style)\b.*?</\1>", " ", markup, flags=re.S)
    markup = re.sub(r"</?(?:a|b|em|i|small|span|strong)\b[^>]*>", "", markup)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup))).strip()


def meta(markup, key):
    match = re.search(r'<meta (?:name|property)="%s" content="([^"]*)"' % re.escape(key), markup)
    return html.unescape(match.group(1)) if match else None


def links(markup):
    return [(html.unescape(href), visible_text(label))
            for href, label in re.findall(r'<a\b[^>]*\bhref="([^"]*)"[^>]*>(.*?)</a>', markup, flags=re.S)]


class BrandAssetTests(SimpleTestCase):
    lockups = ("pdv2-por-crowntech.svg", "pdv2-por-crowntech-negativo.svg",
               "pdv2-por-crowntech-empilhado.svg")

    def test_lockups_are_self_contained_svg_with_the_endorsement(self):
        for name in self.lockups:
            source = (ASSETS / name).read_text(encoding="utf-8")
            root = ElementTree.fromstring(source)
            self.assertTrue(root.tag.endswith("svg"), name)
            text = " ".join("".join(node.itertext()) for node in root.iter() if node.tag.endswith("text"))
            self.assertIn("PDV", text, name)
            self.assertIn("por", text, name)
            self.assertIn("CrownTech", text, name)
            for external in ("@font-face", "@import", "href=", "url(http", "<image"):
                self.assertNotIn(external, source, name)

    def test_horizontal_and_stacked_lockups_have_different_proportions(self):
        def ratio(name):
            box = ElementTree.fromstring((ASSETS / name).read_text(encoding="utf-8")).get("viewBox").split()
            return float(box[2]) / float(box[3])
        self.assertGreater(ratio("pdv2-por-crowntech.svg"), 3)
        self.assertLess(ratio("pdv2-por-crowntech-empilhado.svg"), 1)

    def test_share_image_is_a_self_hosted_1200x630_png(self):
        data = (ASSETS / "og-pdv2.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1200, 630))
        self.assertLess(len(data), 200_000)

    def test_logo_uses_the_brand_palette(self):
        logo = (WEB.parent / "logo-pdv2.svg").read_text(encoding="utf-8").lower()
        self.assertIn("#148765", logo)
        self.assertIn("#102630", logo)
        self.assertNotIn("#347d55", logo)


class LoginBrandTests(SimpleTestCase):
    def setUp(self):
        self.markup = read("login.html")
        self.text = visible_text(self.markup)

    def test_login_no_longer_calls_the_name_provisional(self):
        lowered = self.text.lower()
        for hedge in HEDGES:
            self.assertNotIn(hedge, lowered)
        self.assertIn("PDV2 é um produto CrownTech © 2026", self.text)
        self.assertIn((VENDOR_URL, "CrownTech"), links(self.markup))

    def test_login_shows_the_lockup_and_stays_out_of_search_results(self):
        self.assertIn('src="/assets/pdv2-por-crowntech-negativo.svg"', self.markup)
        self.assertEqual(meta(self.markup, "robots"), "noindex")

    def test_login_stays_honest_about_fiscal_homologation(self):
        self.assertIn("homologação fiscal", self.text)
        self.assertIn("14 dias de teste no plano Essencial", self.text)

    def test_auth_script_copy_has_no_hedging(self):
        script = (ASSETS / "auth.js").read_text(encoding="utf-8").lower()
        for hedge in HEDGES + ("conta de teste para conhecer",):
            self.assertNotIn(hedge, script)


class LandingBrandTests(SimpleTestCase):
    def setUp(self):
        self.markup = read("landing.html")
        self.text = visible_text(self.markup)

    def test_hero_states_the_product_the_price_and_both_calls_to_action(self):
        heading = visible_text(re.search(r"<h1>(.*?)</h1>", self.markup, flags=re.S).group(1))
        self.assertEqual(heading, "Sistema de caixa (PDV) que vende mesmo sem internet.")
        hero = visible_text(re.search(r'<section class="hero">(.*?)</section>', self.markup, flags=re.S).group(1))
        self.assertIn("a partir de R$ 149/mês", hero)
        hero_links = links(re.search(r'<div class="hero-actions">(.*?)</div>', self.markup, flags=re.S).group(1))
        self.assertEqual(hero_links, [("/login/?mode=register", "Testar grátis ↗"),
                                      (VENDOR_URL, "Falar com a CrownTech ↗")])

    def test_trust_band_maker_section_and_footer_carry_the_brand(self):
        self.assertIn("Desenvolvido e suportado pela CrownTech", self.text)
        maker = re.search(r'<section class="section maker-section"[^>]*>(.*?)</section>', self.markup, flags=re.S)
        self.assertIsNotNone(maker)
        self.assertIn("Quem faz o PDV2", visible_text(maker.group(1)))
        self.assertIn(VENDOR_URL, maker.group(1))
        footer = re.search(r'<footer class="site-footer">(.*?)</footer>', self.markup, flags=re.S).group(1)
        self.assertEqual(re.findall(r"<h2[^>]*>(.*?)</h2>", footer), ["Produto", "CrownTech", "Legal"])
        self.assertIn("PDV2 · um produto CrownTech · crowntech.com.br", visible_text(footer))

    def test_no_hedging_and_no_invented_company_data(self):
        lowered = self.text.lower()
        for hedge in HEDGES:
            self.assertNotIn(hedge, lowered)
        self.assertIsNone(INVENTED_CONTACT.search(self.markup))

    def test_every_register_link_uses_one_label_and_vendor_links_another(self):
        register = {label.rstrip(" ↗") for href, label in links(self.markup)
                    if href.startswith("/login/?mode=register")}
        self.assertEqual(register, {"Testar grátis"})
        contact = [href for href, label in links(self.markup) if label.startswith("Falar com a CrownTech")]
        self.assertGreaterEqual(len(contact), 3)
        self.assertEqual(set(contact), {VENDOR_URL})

    def test_plan_buttons_carry_the_chosen_plan(self):
        for slug in ("essential", "management", "advanced"):
            card = re.search(r'<article class="plan-card[^"]*" data-plan="%s">(.*?)</article>' % slug,
                             self.markup, flags=re.S).group(1)
            self.assertIn(("/login/?mode=register&plan=" + slug, "Testar grátis"), links(card))

    def test_app_section_does_not_claim_a_published_app(self):
        section = visible_text(re.search(r'<section class="section owner-app-section".*?</section>\s*<section',
                                         self.markup, flags=re.S).group(0))
        self.assertIn("app de gestão gratuito para iPhone e Android, em breve nas lojas", section)
        self.assertIn("Em breve na App Store", section)
        self.assertIn("Em breve no Google Play", section)
        self.assertNotIn("Aplicativo para iPhone", self.text)
        self.assertNotIn("PDV2 Monitor", self.text)

    def test_a_feature_card_presents_the_new_register_and_the_f_keys(self):
        cards = re.findall(r'<article class="feature[^"]*">(.*?)</article>', self.markup, flags=re.S)
        card = next(visible_text(item) for item in cards if "novo caixa" in item)
        for expected in ("F1 a F12", "F2", "F10"):
            self.assertIn(expected, card)

    def test_search_and_sharing_metadata(self):
        title = "PDV2 — Sistema PDV e frente de caixa offline | CrownTech"
        self.assertIn("<title>%s</title>" % title, self.markup)
        self.assertIn('<link rel="canonical" href="%s">' % SITE_URL, self.markup)
        self.assertEqual(meta(self.markup, "author"), "CrownTech")
        self.assertIn("CrownTech", meta(self.markup, "description"))
        self.assertEqual(meta(self.markup, "og:title"), title)
        self.assertEqual(meta(self.markup, "og:site_name"), "PDV2 · CrownTech")
        self.assertTrue(meta(self.markup, "og:description"))
        self.assertEqual(meta(self.markup, "og:url"), SITE_URL)
        image = meta(self.markup, "og:image")
        self.assertTrue(image.startswith(SITE_URL + "assets/"))
        self.assertTrue((ASSETS / image.rsplit("/", 1)[1]).is_file())

    def test_structured_data_matches_the_plans_on_the_page(self):
        block = re.search(r'<script type="application/ld\+json">(.*?)</script>', self.markup, flags=re.S)
        data = json.loads(block.group(1))
        self.assertEqual(data["@type"], "SoftwareApplication")
        self.assertEqual(data["publisher"], {"@type": "Organization", "name": "CrownTech", "url": VENDOR_URL})
        offers = {offer["name"]: offer for offer in data["offers"]}
        starter = re.findall(r'slug:"([a-z]+)", name:"([^"]+)", price_month_cents:(\d+)',
                             (ASSETS / "landing.js").read_text(encoding="utf-8"))
        self.assertEqual(len(starter), 3)
        self.assertEqual(len(offers), 3)
        for slug, name, cents in starter:
            card = re.search(r'data-plan="%s">.*?<strong data-price>R\$ (\d+)</strong>' % slug,
                             self.markup, flags=re.S)
            self.assertEqual(int(card.group(1)) * 100, int(cents), slug)
            self.assertEqual(offers[name]["price"], "%d.00" % (int(cents) // 100), slug)
            self.assertEqual(offers[name]["priceCurrency"], "BRL")

    def test_stylesheet_has_no_rules_for_removed_sections(self):
        styles = (ASSETS / "landing.css").read_text(encoding="utf-8")
        for selector in ("honesty-", "setup-grid", "footer-inner", "app-platform-ios"):
            self.assertNotIn(selector, styles)
        for selector in (".maker-grid", ".footer-grid", ".trust-list", ".brand-lockup"):
            self.assertIn(selector, styles)


class LandingScriptTests(SimpleTestCase):
    def setUp(self):
        self.source = (ASSETS / "landing.js").read_text(encoding="utf-8")

    def test_api_failures_are_never_written_into_the_sales_page(self):
        self.assertNotIn("errorText", self.source)
        self.assertNotIn("#price-source", self.source)
        for jargon in ("carregados da plataforma", "indisponíveis no servidor", "Não foi possível atualizar"):
            self.assertNotIn(jargon, self.source)
        self.assertIn("console.warn", self.source)

    @unittest.skipUnless(shutil.which("node"), "node is required to run the landing script")
    def test_cards_follow_the_plans_on_sale(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "core.mjs").write_text((ASSETS / "core.js").read_text(encoding="utf-8"), encoding="utf-8")
            (folder / "landing.mjs").write_text(self.source.replace('"./core.js"', '"./core.mjs"'),
                                                encoding="utf-8")
            (folder / "run.mjs").write_text(
                'import { planLayout, registerHref, lowestMonthlyCents, shortCurrency } from "./landing.mjs";\n'
                'const plans = [{slug:"essential", price_month_cents:15900}, {slug:"pro", price_month_cents:44900}];\n'
                'console.log(JSON.stringify({\n'
                '  layout: planLayout(plans, ["essential", "management", "advanced"]),\n'
                '  href: registerHref("pro"), plain: registerHref(""),\n'
                '  lowest: lowestMonthlyCents(plans), none: lowestMonthlyCents([{price_month_cents:0}]),\n'
                '  whole: shortCurrency(15900).replace(/\\s/g, " "), cents: shortCurrency(15990).replace(/\\s/g, " ")\n'
                '}));\n', encoding="utf-8")
            result = subprocess.run(["node", str(folder / "run.mjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        # A retired plan loses its card and a new plan gets one, instead of selling a stale price.
        self.assertEqual(output["layout"], {"show": ["essential", "pro"], "create": ["pro"],
                                            "hide": ["management", "advanced"]})
        self.assertEqual(output["href"], "/login/?mode=register&plan=pro")
        self.assertEqual(output["plain"], "/login/?mode=register")
        self.assertEqual(output["lowest"], 15900)
        self.assertIsNone(output["none"])
        self.assertEqual(output["whole"], "R$ 159")
        self.assertEqual(output["cents"], "R$ 159,90")


class ManifestAndOfflineTests(TestCase):
    def test_manifest_names_the_vendor_and_splits_icon_purposes(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        manifest = json.loads(b"".join(response.streaming_content))
        self.assertEqual(manifest["name"], "PDV2 · CrownTech")
        self.assertIn("CrownTech", manifest["description"])
        self.assertNotIn("mercearias", manifest["description"])
        purposes = sorted(icon["purpose"] for icon in manifest["icons"])
        self.assertEqual(purposes, ["any", "any", "maskable", "maskable"])
        for icon in manifest["icons"]:
            data = (ASSETS / icon["src"].rsplit("/", 1)[1]).read_bytes()
            size = int(icon["sizes"].split("x")[0])
            self.assertEqual(struct.unpack(">II", data[16:24]), (size, size), icon["src"])

    def test_offline_page_is_branded_and_works_under_the_content_security_policy(self):
        response = self.client.get("/offline/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("script-src 'self'", response["Content-Security-Policy"])
        markup = b"".join(response.streaming_content).decode("utf-8")
        # Inline handlers are blocked by the policy above, so retry has to be a plain link.
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", markup))
        self.assertNotIn("<script", markup)
        self.assertIn(("", "Tentar novamente"), links(markup))
        text = visible_text(markup)
        self.assertIn("por CrownTech", text)
        self.assertIn("PDV2 · um produto CrownTech · crowntech.com.br", text)
        self.assertIn("PDV2 · CrownTech", re.search(r"<title>(.*?)</title>", markup).group(1))
        # The local terminal only exists on the store computer, never on a phone.
        self.assertIn(".offline-terminal{display:none}", markup)
        self.assertIn("@media(hover:hover) and (pointer:fine){.offline-terminal{display:inline-flex}}", markup)

    def test_public_pages_are_served_with_the_brand(self):
        for path in ("/", "/login/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn("CrownTech", b"".join(response.streaming_content).decode("utf-8"), path)
