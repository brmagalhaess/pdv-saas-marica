"""Tiny XML tree for documents we generate ourselves, serialized in canonical form.

The NF-e signature uses Canonical XML 1.0 (http://www.w3.org/TR/2001/REC-xml-c14n-20010315).
Every document built here only uses default namespaces (no prefixes), no comments,
no processing instructions and no mixed content, so its canonical form is simple:

- the element's namespace declaration (``xmlns="..."``) comes first, then the attributes
  sorted by name;
- empty elements are written as ``<a></a>``;
- text escapes ``& < >`` and carriage return; attributes escape ``& < "`` and tab/newline/CR;
- no XML declaration and no whitespace between tags.

The serialization of the whole document IS its canonical form, so the bytes we send
are the bytes we digest. ``canonical(element, inherited_ns)`` renders a subtree as
an XPath node-set would be canonicalized (the in-scope default namespace is declared
on the subtree root), which is what the ``Reference URI="#Id"`` of XMLDSig needs.

Parsing (``parse``) refuses DTD/entity declarations before handing the bytes to
ElementTree, and ``from_etree`` converts back to this tree so a document received
from a terminal can be signed byte for byte.
"""
import re
import xml.etree.ElementTree as ET

NFE_NS = "http://www.portalfiscal.inf.br/nfe"
DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"\s+")
_DECLARATION = re.compile(r"""\s*<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


class XMLError(ValueError):
    pass


class E:
    """Element: tag (local name), ns (None = inherit), attributes, children or text."""

    __slots__ = ("tag", "ns", "attrs", "children", "text")

    def __init__(self, tag, *children, ns=None, text=None, **attrs):
        self.tag = tag
        self.ns = ns
        self.attrs = {key: str(value) for key, value in attrs.items() if value is not None}
        self.children = []
        self.text = None if text is None else str(text)
        for child in children:
            self.add(child)

    def add(self, child):
        if child is None:
            return self
        if isinstance(child, (list, tuple)):
            for item in child:
                self.add(item)
            return self
        if not isinstance(child, E):
            raise TypeError("child must be an element")
        if self.text is not None:
            raise XMLError("Elemento com texto não aceita filhos")
        self.children.append(child)
        return self

    def find(self, path):
        """Child by a slash path of local names ("ide/nNF")."""
        node = self
        for part in path.split("/"):
            node = next((child for child in node.children if child.tag == part), None)
            if node is None:
                return None
        return node

    def findall(self, tag):
        return [child for child in self.children if child.tag == tag]

    def findtext(self, path, default=""):
        node = self.find(path)
        return node.text if node is not None and node.text is not None else default

    def iter(self, tag=None):
        if tag is None or self.tag == tag:
            yield self
        for child in self.children:
            yield from child.iter(tag)


def leaf(tag, value):
    """Element with text, or None (skipped) when the value is empty."""
    if value is None or value == "":
        return None
    return E(tag, text=value)


def clean_text(value, limit=None):
    """Text fit for the NF-e schema: no control characters, single spaces, trimmed."""
    text = _SPACES.sub(" ", _CONTROL.sub("", str(value or ""))).strip()
    if limit:
        text = text[:limit].rstrip()
    return text


def _text_escape(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#xD;")


def _attr_escape(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
            .replace("\t", "&#x9;").replace("\n", "&#xA;").replace("\r", "&#xD;"))


def _render(node, parent_ns, out):
    own = node.ns if node.ns is not None else parent_ns
    out.append("<" + node.tag)
    if own and own != parent_ns:
        out.append(' xmlns="' + _attr_escape(own) + '"')
    for key in sorted(node.attrs):
        out.append(" " + key + '="' + _attr_escape(node.attrs[key]) + '"')
    out.append(">")
    if node.text is not None:
        out.append(_text_escape(node.text))
    for child in node.children:
        _render(child, own, out)
    out.append("</" + node.tag + ">")


def canonical(node, inherited_ns=None):
    """Canonical XML 1.0 of the subtree (str). ``inherited_ns`` is the default
    namespace in scope at the subtree root in the full document."""
    out = []
    # The root declares the namespace in scope even when it only inherits it.
    own = node.ns if node.ns is not None else inherited_ns
    copy = E.__new__(E)
    copy.tag, copy.ns, copy.attrs, copy.children, copy.text = node.tag, own, node.attrs, node.children, node.text
    _render(copy, None, out)
    return "".join(out)


def tostring(node, declaration=False):
    body = canonical(node)
    return ('<?xml version="1.0" encoding="UTF-8"?>' + body) if declaration else body


def tobytes(node, declaration=False):
    return tostring(node, declaration).encode("utf-8")


# ------------------------------------------------------------------ parsing

MAX_XML_BYTES = 5 * 1024 * 1024


def parse(data):
    """ElementTree root of untrusted XML; DTD/ENTITY declarations are refused."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise XMLError("XML vazio")
    if len(data) > MAX_XML_BYTES:
        raise XMLError("XML excede o limite de tamanho")
    data = bytes(data)
    # Only plain UTF-8: a BOM or another encoding (UTF-16/32, declared latin-1...) could hide a
    # DOCTYPE/ENTITY from the byte check below, since the parser decodes before reading markup.
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\x00")) or b"\x00" in data:
        raise XMLError("XML deve estar em UTF-8 sem BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise XMLError("XML deve estar em UTF-8") from exc
    declaration = _DECLARATION.match(text)
    if declaration and declaration.group(1).lower().replace("_", "-") not in ("utf-8", "utf8"):
        raise XMLError("XML deve estar em UTF-8")
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise XMLError("Declaração XML não permitida")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise XMLError("XML inválido") from exc


def split_tag(tag):
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def from_etree(node, parent_ns=None):
    """Convert an ElementTree element into E (default namespaces only)."""
    ns, local = split_tag(node.tag)
    for key in node.attrib:
        if key.startswith("{"):
            raise XMLError("Atributo com namespace não suportado")
    children = list(node)
    text = node.text if node.text is not None and node.text.strip() != "" else None
    if children and text is not None:
        raise XMLError("Conteúdo misto não suportado")
    for child in children:
        if child.tail is not None and child.tail.strip():
            raise XMLError("Conteúdo misto não suportado")
    element = E(local, ns=ns if ns != (parent_ns or "") else None, **dict(node.attrib))
    if not children:
        element.text = node.text if node.text is not None else None
        if element.text is not None and element.text == "":
            element.text = None
    for child in children:
        element.children.append(from_etree(child, ns))
    return element


def fromstring(data):
    return from_etree(parse(data))


def etree_text(node, path, ns=NFE_NS, default=""):
    """findtext on an ElementTree node with every path part in the NF-e namespace."""
    if node is None:
        return default
    found = node.find("/".join("{%s}%s" % (ns, part) if part not in (".", "..") and not part.startswith(".")
                               else part for part in path.split("/")))
    return (found.text or "").strip() if found is not None and found.text is not None else default
